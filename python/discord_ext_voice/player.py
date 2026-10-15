"""Audio sources and the playback thread, mirroring :mod:`discord.player`.

Where ``discord.py`` shells out to the ``ffmpeg`` binary and uses the removed ``audioop``
module, this implementation decodes and resamples in-process with ``ac-ffmpeg`` (libav*) and
scales volume in Rust. The Python API is unchanged, and the ``ffmpeg`` CLI is still used as a
fallback for inputs libav cannot open directly (URLs, pipes, exotic options).
"""

from __future__ import annotations

import io
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from typing import IO, Any, Callable, Generic, Optional, TypeVar, Union

from . import _native
from .errors import ClientException, FFmpegError, FFmpegProcessError
from .opus import OPUS_SILENCE, Encoder as OpusEncoder
from .opus import FRAME_SIZE

__all__ = (
    'AudioSource',
    'PCMAudio',
    'FFmpegPCMAudio',
    'FFmpegOpusAudio',
    'PCMVolumeTransformer',
    'AudioPlayer',
)

_log = logging.getLogger(__name__)

AT = TypeVar('AT', bound='AudioSource')

#: URL schemes that only the ``ffmpeg`` binary (via libavformat protocols) can open here.
_URL_SCHEMES = (
    'http://',
    'https://',
    'rtmp://',
    'rtmps://',
    'rtsp://',
    'mms://',
    'ftp://',
    'hls+http://',
)


def _looks_like_url(source: Any) -> bool:
    return isinstance(source, str) and source.lower().startswith(_URL_SCHEMES)


def _as_path(source: Any) -> Optional[str]:
    """Return a filesystem path for `source`, or None when it is not path-like."""
    if isinstance(source, (str, os.PathLike)):
        return os.fspath(source)
    return None


def _missing_local_file(source: Any) -> bool:
    """Whether `source` names a local file that does not exist."""
    path = _as_path(source)
    if path is None or _looks_like_url(source):
        return False
    return not os.path.exists(path)


def _parse_extra_options(before_options: Optional[str], options: Optional[str]) -> dict[str, str]:
    """Extract the libav options we can honour in-process.

    Only ``-ss``/``-t``/``-to``/``-f`` are interpreted by the caller; anything else is
    returned in the dict so the caller can decide whether to fall back to the CLI.
    """
    tokens: list[str] = []
    for blob in (before_options, options):
        if blob:
            tokens.extend(shlex.split(blob))

    parsed: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith('-'):
            name = token.lstrip('-')
            if index + 1 < len(tokens) and not tokens[index + 1].startswith('-'):
                parsed[name] = tokens[index + 1]
                index += 2
                continue
            parsed[name] = ''
        index += 1
    return parsed


def _seconds(value: Optional[str]) -> Optional[float]:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except ValueError:
        match = re.match(r'^(\d+):(\d+):(\d+(?:\.\d+)?)$', value)
        if not match:
            return None
        hours, minutes, secs = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(secs)


class AudioSource:
    """Represents an audio stream.

    The interface is identical to :class:`discord.AudioSource`.
    """

    def read(self) -> bytes:
        """Reads 20ms worth of audio.

        Subclasses must implement this. Returning an empty bytestring signals the end of the
        stream.
        """
        raise NotImplementedError

    def is_opus(self) -> bool:
        """Whether the data is already Opus encoded."""
        return False

    def cleanup(self) -> None:
        """Called when the source is done being used."""

    def __del__(self) -> None:
        try:
            self.cleanup()
        except Exception:
            pass


class PCMAudio(AudioSource):
    """Raw 16-bit 48KHz stereo PCM read from a file-like object.

    Attributes
    ----------
    stream: :term:`py:file object`
        A file-like object that reads byte data representing raw PCM.
    """

    def __init__(self, stream: io.BufferedIOBase) -> None:
        self.stream: io.BufferedIOBase = stream

    def read(self) -> bytes:
        data = self.stream.read(FRAME_SIZE)
        if len(data) != FRAME_SIZE:
            return b''
        return data


class FFmpegAudio(AudioSource):
    """Base class for sources that decode through libav.

    Unlike ``discord.FFmpegAudio`` this does not unconditionally spawn a process; the
    subprocess machinery below is only used as a fallback.
    """

    def __init__(self, source: Union[str, io.BufferedIOBase]) -> None:
        self.source: Union[str, io.BufferedIOBase] = source

    @property
    def original(self) -> Any:  # pragma: no cover - debugging helper
        return self.source


class FFmpegPCMAudio(FFmpegAudio):
    """A PCM audio source decoded with FFmpeg.

    Local files and file-like objects are decoded in-process with ``ac-ffmpeg``, which avoids
    the ``ffmpeg`` subprocess entirely. URLs, ``pipe=True`` and unusual ``-before_options``
    automatically fall back to the ``ffmpeg`` CLI, so the accepted inputs are the same as
    ``discord.FFmpegPCMAudio``.

    Parameters
    ----------
    source: Union[:class:`str`, :term:`py:file object`]
        The source to stream from.
    executable: :class:`str`
        The FFmpeg executable to use for the fallback path. Defaults to ``'ffmpeg'``.
    pipe: :class:`bool`
        If ``True``, a subprocess is always used and ``source`` is written to its stdin.
    stderr: Optional[:term:`py:file object`]
        A file-like object to write the FFmpeg process' stderr to. Forces the CLI path.
    before_options: Optional[:class:`str`]
        Extra CLI options inserted before ``-i``. Only ``-ss``/``-f`` are honoured in-process.
    options: Optional[:class:`str`]
        Extra CLI options inserted after ``-i``. Only ``-t``/``-to`` are honoured in-process.
    force_cli: :class:`bool`
        Always use the ``ffmpeg`` subprocess, even when the in-process decoder could handle
        the input.
    """

    def __init__(
        self,
        source: Union[str, io.BufferedIOBase],
        *,
        executable: str = 'ffmpeg',
        pipe: bool = False,
        stderr: Optional[IO[bytes]] = None,
        before_options: Optional[str] = None,
        options: Optional[str] = None,
        force_cli: bool = False,
    ) -> None:
        if pipe and _as_path(source) is not None:
            raise TypeError(
                "parameter conflict: 'source' cannot be a path when piping to ffmpeg's stdin"
            )

        super().__init__(source)
        self._native: Optional[_native.DecodeSource] = None
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._stdout: Optional[IO[bytes]] = None

        extra = _parse_extra_options(before_options, options)
        seek_start = _seconds(extra.get('ss'))
        seek_end = _seconds(extra.get('t'))
        seek_to = _seconds(extra.get('to'))
        input_format = extra.get('f')

        if _missing_local_file(source):
            raise FFmpegError(f'unable to open {os.fspath(source)!r}: no such file')

        unsupported = set(extra) - {'ss', 't', 'to', 'f'}
        use_cli = (
            force_cli
            or pipe
            or stderr is not None
            or executable != 'ffmpeg'
            or _looks_like_url(source)
            or bool(unsupported)
        )

        if not use_cli:
            try:
                self._native = _native.DecodeSource(
                    source,
                    format=input_format,
                    start_time=seek_start,
                    end_time=seek_end if seek_end is not None else seek_to,
                )
            except FFmpegError as exc:
                _log.debug('in-process decode unavailable (%s); falling back to ffmpeg', exc)
                use_cli = True

        if use_cli:
            args = self._build_arguments(
                source,
                executable=executable,
                pipe=pipe,
                before_options=before_options,
                options=options,
            )
            self._process = self._spawn_process(args, pipe=pipe, stderr=stderr)
            self._stdout = self._process.stdout
            if pipe:
                threading.Thread(
                    target=self._pipe_writer,
                    args=(source,),
                    daemon=True,
                    name=f'ffmpeg-pipe-writer:{id(self):#x}',
                ).start()

    # ------------------------------------------------------------------ construction helpers

    #: Mirrors `discord.player.FFmpegAudio.BLOCKSIZE`.
    BLOCKSIZE: int = io.DEFAULT_BUFFER_SIZE

    @staticmethod
    def _build_arguments(
        source: Union[str, io.BufferedIOBase],
        *,
        executable: str,
        pipe: bool,
        before_options: Optional[str],
        options: Optional[str],
    ) -> list[str]:
        """Build the ``ffmpeg`` command line.

        The option order matches ``discord.FFmpegPCMAudio`` exactly: ``before_options``
        precede ``-i``, and the PCM output options follow it, followed by ``options``.
        """
        args = [executable]

        if before_options is not None:
            args.extend(shlex.split(before_options))

        args.append('-i')
        if pipe:
            args.append('-')
        else:
            path = _as_path(source)
            if path is None:
                raise TypeError('a file-like source requires pipe=True with the ffmpeg fallback')
            args.append(path)

        args.extend(
            (
                '-f',
                's16le',
                '-ar',
                '48000',
                '-ac',
                '2',
                '-loglevel',
                'warning',
                '-blocksize',
                str(io.DEFAULT_BUFFER_SIZE),
            )
        )

        if options is not None:
            args.extend(shlex.split(options))

        args.append('pipe:1')
        return args

    @staticmethod
    def _spawn_process(
        args: list[str], *, pipe: bool = False, stderr: Optional[IO[bytes]] = None
    ) -> subprocess.Popen[bytes]:
        _log.debug('spawning ffmpeg: %s', ' '.join(args))
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE if pipe else subprocess.DEVNULL,
            stderr=stderr,
        )
        if process.stdout is None:
            raise FFmpegProcessError('the ffmpeg process did not expose a stdout pipe')
        return process

    def _pipe_writer(self, source: Any) -> None:
        process = self._process
        if process is None or process.stdin is None:
            return
        try:
            while process.poll() is None:
                data = source.read(self.BLOCKSIZE)  # type: ignore[union-attr]
                if not data:
                    break
                process.stdin.write(data)
        except Exception:
            _log.debug('error while piping audio into ffmpeg', exc_info=True)
        finally:
            try:
                process.stdin.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ AudioSource

    def read(self) -> bytes:
        if self._native is not None:
            return self._native.read()
        if self._stdout is None:
            return b''
        data = self._stdout.read(FRAME_SIZE)
        if len(data) != FRAME_SIZE:
            self._check_process_returncode()
            return b''
        return data

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        if self._native is not None:
            self._native.cleanup()
            self._native = None
        self._kill_process()

    def _check_process_returncode(self) -> None:
        """Reap the process and raise if it failed.

        Unlike ``discord.py`` this waits for the child to exit, which makes the error
        deterministic instead of depending on whether ``Popen`` happened to notice the exit.
        """
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return
        if process.returncode in (None, 0):
            return
        raise FFmpegProcessError(
            f'FFmpeg exited with code {process.returncode}. Verify that the arguments are '
            f'correct and that the input is a valid media file.'
        )

    def _kill_process(self) -> None:
        process = self._process
        if process is None:
            return
        self._process = None
        self._stdout = None
        if process.poll() is not None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class FFmpegOpusAudio(FFmpegAudio):
    """An Opus audio source.

    Two modes are supported, chosen automatically:

    * If the input already carries Opus packets (``.ogg``/``.webm``/``.mka`` Opus), the raw
      packets are passed through untouched, so Discord receives the original stream.
    * Otherwise the input is decoded to PCM in-process and encoded with libopus by
      :class:`discord_ext_voice.opus.Encoder`, which is what ``discord.FFmpegOpusAudio``
      achieves by asking FFmpeg to transcode.

    Parameters
    ----------
    source: Union[:class:`str`, :term:`py:file object`]
        The source to stream from.
    executable: :class:`str`
        Only used by the CLI fallback path.
    pipe: :class:`bool`
        If ``True``, force the CLI fallback and pipe ``source`` to its stdin.
    stderr: Optional[:term:`py:file object`]
        Only used by the CLI fallback path.
    before_options: Optional[:class:`str`]
        Extra options inserted before ``-i``.
    options: Optional[:class:`str`]
        Extra options inserted after ``-i``.
    codec: Optional[:class:`str`]
        Accepted for compatibility; ignored (libopus is always the encoder).
    bitrate: Optional[:class:`int`]
        Accepted for compatibility; ignored.
    """

    def __init__(
        self,
        source: Union[str, io.BufferedIOBase],
        *,
        executable: str = 'ffmpeg',
        pipe: bool = False,
        stderr: Optional[IO[bytes]] = None,
        before_options: Optional[str] = None,
        options: Optional[str] = None,
        codec: Optional[str] = None,
        bitrate: Optional[int] = None,
    ) -> None:
        if pipe and _as_path(source) is not None:
            raise TypeError(
                "parameter conflict: 'source' cannot be a path when piping to ffmpeg's stdin"
            )

        super().__init__(source)
        self.codec = codec
        self.bitrate = bitrate
        self._native: Optional[AudioSource] = None
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._stdout: Optional[IO[bytes]] = None

        if _missing_local_file(source):
            raise FFmpegError(f'unable to open {os.fspath(source)!r}: no such file')

        extra = _parse_extra_options(before_options, options)
        seek_start = _seconds(extra.get('ss'))
        seek_end = _seconds(extra.get('t'))
        seek_to = _seconds(extra.get('to'))
        input_format = extra.get('f')
        unsupported = set(extra) - {'ss', 't', 'to', 'f'}

        use_cli = (
            pipe
            or stderr is not None
            or executable != 'ffmpeg'
            or _looks_like_url(source)
            or bool(unsupported)
        )

        if not use_cli:
            probed = _native.probe(source, format=input_format)
            codec_name = (probed[0] if probed else '') or ''
            if codec_name.startswith('opus'):
                self._native = _native.OpusSource(source, format=input_format)
            else:
                # Transcoding is still required, but libopus does it directly.
                self._native = _native.DecodeSource(
                    source,
                    format=input_format,
                    start_time=seek_start,
                    end_time=seek_end if seek_end is not None else seek_to,
                )

        if use_cli:
            args = [executable, '-hide_banner']
            if before_options is not None:
                args.extend(shlex.split(before_options))
            args.append('-i')
            args.append('-' if pipe else _as_path(source))
            args.extend(
                (
                    '-map_metadata',
                    '-1',
                    '-c:a',
                    'libopus',
                    '-ar',
                    '48000',
                    '-ac',
                    '2',
                    '-b:a',
                    f'{bitrate if bitrate is not None else 128}k',
                    '-loglevel',
                    'warning',
                    '-fec',
                    'true',
                    '-packet_loss',
                    '15',
                    '-f',
                    'opus',
                )
            )
            if options is not None:
                args.extend(shlex.split(options))
            args.append('pipe:1')
            self._process = FFmpegPCMAudio._spawn_process(args, pipe=pipe, stderr=stderr)
            self._stdout = self._process.stdout
            self._ogg: Any = None
            if pipe:
                threading.Thread(
                    target=self._pipe_writer,
                    args=(source,),
                    daemon=True,
                    name=f'ffmpeg-pipe-writer:{id(self):#x}',
                ).start()

    @property
    def _is_native_opus(self) -> bool:
        return isinstance(self._native, _native.OpusSource)

    def read(self) -> bytes:
        if self._native is not None:
            return self._native.read()
        if self._stdout is None:
            return b''
        packet = self._read_ogg_packet()
        if not packet:
            self._check_process_returncode()
        return packet

    def is_opus(self) -> bool:
        if self._native is not None:
            return self._native.is_opus()
        return True

    def cleanup(self) -> None:
        if self._native is not None:
            self._native.cleanup()
            self._native = None
        process = self._process
        if process is not None:
            self._process = None
            self._stdout = None
            if process.poll() is None:
                process.kill()
                process.wait()

    def _check_process_returncode(self) -> None:
        process = self._process
        if process is None:
            return
        if process.wait() != 0:
            raise FFmpegProcessError(f'FFmpeg exited with code {process.returncode}.')

    def _read_ogg_packet(self) -> bytes:
        """Minimal Ogg page reader used by the CLI fallback."""
        from .oggparse import OggStream

        if getattr(self, '_ogg', None) is None:
            self._ogg = OggStream(self._stdout)  # type: ignore[arg-type]
        packet = self._ogg.read_packet()
        return packet or b''

    _pipe_writer = FFmpegPCMAudio._pipe_writer

    @classmethod
    async def from_probe(
        cls,
        source: Union[str, io.BufferedIOBase],
        *,
        method: Optional[str] = None,
        **kwargs: Any,
    ) -> 'FFmpegOpusAudio':
        """Construct the source after probing the input codec."""
        codec, _ = await cls.probe(source, **kwargs)
        return cls(source, codec=codec, **kwargs)

    @staticmethod
    async def probe(
        source: Union[str, io.BufferedIOBase],
        *,
        executable: str = 'ffmpeg',
        **kwargs: Any,
    ) -> tuple[Optional[str], Optional[int]]:
        """Probe ``source`` and return ``(codec, bitrate)``."""
        import asyncio

        def _probe() -> tuple[Optional[str], Optional[int]]:
            try:
                result = _native.probe(source)
            except FFmpegError:
                return None, None
            if result is None:
                return None, None
            codec, _sample_rate, _channels, _duration = result
            return codec, None

        return await asyncio.get_running_loop().run_in_executor(None, _probe)


class PCMVolumeTransformer(AudioSource, Generic[AT]):
    """Transforms a previous :class:`AudioSource` to have volume controls.

    This does not work on audio sources that have :meth:`AudioSource.is_opus` set to ``True``.
    """

    def __init__(self, original: AT, volume: float = 1.0):
        if not isinstance(original, AudioSource) and not hasattr(original, 'read'):
            raise TypeError(f'expected AudioSource not {original.__class__.__name__}.')

        if original.is_opus():
            raise ClientException('AudioSource must not be Opus encoded.')

        self.original: AT = original
        self._native = _native.VolumeTransformer(original, volume)

    @property
    def volume(self) -> float:
        """Retrieves or sets the volume as a floating point percentage (e.g. ``1.0`` for 100%)."""
        return self._native.volume

    @volume.setter
    def volume(self, value: float) -> None:
        self._native.volume = max(value, 0.0)

    def cleanup(self) -> None:
        self._native.cleanup()

    def read(self) -> bytes:
        return self._native.read()


class AudioPlayer(threading.Thread):
    """The thread that pulls frames from an :class:`AudioSource` and sends them to Discord.

    This is a faithful port of ``discord.player.AudioPlayer``; only the encoder plumbing
    differs, because encoding happens natively.
    """

    DELAY: float = OpusEncoder.FRAME_LENGTH / 1000.0

    def __init__(
        self,
        source: AudioSource,
        client: Any,
        *,
        after: Optional[Callable[[Optional[Exception]], Any]] = None,
    ) -> None:
        super().__init__(daemon=True, name=f'audio-player:{id(self):#x}')
        self.source: AudioSource = source
        self.client = client
        self.after: Optional[Callable[[Optional[Exception]], Any]] = after

        self._end: threading.Event = threading.Event()
        self._resumed: threading.Event = threading.Event()
        self._resumed.set()  # we are not paused
        self._current_error: Optional[Exception] = None
        self._lock: threading.Lock = threading.Lock()

        if after is not None and not callable(after):
            raise TypeError('Expected a callable for the "after" parameter.')

    def _do_run(self) -> None:
        self.loops = 0
        self._start = time.perf_counter()

        client = self.client
        play_audio = client.send_audio_packet
        self._speak(1)

        while not self._end.is_set():
            if not self._resumed.is_set():
                self.send_silence()
                self._resumed.wait()
                continue

            data = self.source.read()

            if not data:
                if self._current_error is None:
                    source_error = getattr(self.source, '_current_error', None)
                    if source_error:
                        self._current_error = source_error
                self.stop()
                break

            if not client.is_connected():
                _log.debug('Not connected, waiting for %ss...', client.timeout)
                connected = client.wait_until_connected(client.timeout)
                if self._end.is_set() or not connected:
                    _log.debug('Aborting playback')
                    return
                _log.debug('Reconnected, resuming playback')
                self._speak(1)
                self.loops = 0
                self._start = time.perf_counter()

            play_audio(data, encode=not self.source.is_opus())
            self.loops += 1
            next_time = self._start + self.DELAY * self.loops
            delay = max(0, self.DELAY + (next_time - time.perf_counter()))
            time.sleep(delay)

        if client.is_connected():
            self.send_silence()

    def run(self) -> None:
        try:
            self._do_run()
        except Exception as exc:
            self._current_error = exc
            self.stop()
        finally:
            self._call_after()
            self.source.cleanup()

    def _call_after(self) -> None:
        error = self._current_error

        if self.after is not None:
            try:
                self.after(error)
            except Exception as exc:
                exc.__context__ = error
                _log.exception('Calling the after function failed.', exc_info=exc)
        elif error:
            _log.exception('Exception in voice thread %s', self.name, exc_info=error)

    def stop(self) -> None:
        self._end.set()
        self._resumed.set()
        self._speak(0)

    def pause(self, *, update_speaking: bool = True) -> None:
        self._resumed.clear()
        if update_speaking:
            self._speak(0)

    def resume(self, *, update_speaking: bool = True) -> None:
        self.loops = 0
        self._start = time.perf_counter()
        self._resumed.set()
        if update_speaking:
            self._speak(1)

    def is_playing(self) -> bool:
        return self._resumed.is_set() and not self._end.is_set()

    def is_paused(self) -> bool:
        return not self._end.is_set() and not self._resumed.is_set()

    def set_source(self, source: AudioSource) -> None:
        with self._lock:
            self.pause(update_speaking=False)
            self.source = source
            self.resume(update_speaking=False)

    def _speak(self, speaking: int) -> None:
        try:
            import asyncio

            loop = getattr(self.client, 'loop', None) or self.client.client.loop
            asyncio.run_coroutine_threadsafe(self.client.ws.speak(speaking), loop)
        except Exception:
            _log.exception('Speaking call in player failed')

    def send_silence(self, count: int = 5) -> None:
        for _ in range(count):
            try:
                self.client.send_audio_packet(OPUS_SILENCE, encode=False)
            except Exception:
                # Any possible error (probably a socket error) is inconsequential.
                pass
