"""The audio sources this package plays.

Every source here is implemented in this package and decodes in process:

* ``ac-ffmpeg`` (libavformat, libavcodec, libswresample) for containers and codecs,
* ``libopus`` for the packets :class:`discord_ext_voice.opus.Encoder` produces at play time.

The ``ffmpeg`` binary is never spawned, which is why discord.py's FFmpeg-CLI arguments
(``executable``, ``pipe``, ``stderr``, ``before_options``, ``options``, ``force_cli``) do not
exist here — pass the media itself (a path, a file object or an ``http(s)`` URL, which is
streamed with ``reqwest``), and use ``seek`` where ``-ss`` used to go.

":class:`AudioSource` is this package's own base class; nothing here inherits from discord.py.
A source only has to provide ``read``/``is_opus``/``cleanup``, which is also the contract
:class:`discord.player.AudioPlayer` (reused as the playback clock) relies on — so a user's own
``discord.AudioSource`` subclass is still accepted by ``play()``.
"""

from __future__ import annotations

import asyncio
import io
import math
import os
from typing import Any, Optional, TypeVar

from discord.errors import ClientException

from . import _native, opus
from .errors import FFmpegSourceError

__all__ = (
    'AudioSource',
    'PCMAudio',
    'FFmpegPCMAudio',
    'FFmpegOpusAudio',
    'PCMVolumeTransformer',
    'is_audio_source',
)

#: One 20ms frame of 48kHz stereo signed 16 bit PCM, which is what the player sends.
FRAME_SIZE: int = opus.Encoder.FRAME_SIZE

AT = TypeVar('AT', bound='AudioSource')


def is_audio_source(candidate: Any) -> bool:
    """Whether ``candidate`` can be played.

    Either one of the sources in this module, or anything that provides the same three methods —
    which is how a ``discord.AudioSource`` subclass, or a user's own duck-typed source, stays
    usable.
    """
    if isinstance(candidate, AudioSource):
        return True
    return callable(getattr(candidate, 'read', None)) and callable(
        getattr(candidate, 'is_opus', None)
    )


def _reject_ffmpeg_cli_options(options: dict[str, Any]) -> None:
    """Reject discord.py's FFmpeg-CLI arguments, which have no meaning without a binary."""
    if not options:
        return

    names = ', '.join(sorted(options))
    plural = 's' if len(options) > 1 else ''
    raise TypeError(
        f'unsupported argument{plural} {names}: this package decodes in process with ac-ffmpeg '
        '(libav) and never runs the ffmpeg binary, so the FFmpeg CLI arguments of discord.py '
        '(before_options, options, executable, pipe, stderr, force_cli) do not exist here. '
        'Pass the media directly and use seek= instead of -ss.'
    )


def _validate_seek(seek: Optional[float]) -> None:
    """Reject a seek position that cannot be honoured before any native resource is opened."""
    if seek is None:
        return
    if not isinstance(seek, (int, float)) or isinstance(seek, bool) or not math.isfinite(seek):
        raise ValueError('the seek position must be a finite, non-negative number of seconds')
    if seek < 0:
        raise ValueError('the seek position must be a finite, non-negative number of seconds')


def _is_url(source: Any) -> bool:
    """Whether ``source`` is an ``http(s)`` URL, which the native reader streams itself."""
    return isinstance(source, str) and source.startswith(('http://', 'https://'))


def _is_path(source: Any) -> bool:
    """Whether ``source`` names a file on disk (a URL is not one)."""
    return isinstance(source, (str, os.PathLike)) and not _is_url(source)


def _open_reader(source: Any, seek: Optional[float], *, opus_first: bool) -> Any:
    """Create and (optionally) seek the native reader, reporting failures as our own error."""
    reader: Any = None
    try:
        # An Opus stream is passed through untouched; a URL is worth the extra request, because
        # it is as likely to be an Ogg/WebM as a plain file is.
        if opus_first and (_is_path(source) or _is_url(source)):
            try:
                reader = _native.OpusSource(source)
            except _native.FFmpegError:
                # Not an Opus stream: fall through to decoding, which is what the old
                # `probe_codec(...) == 'opus'` check decided, without opening the input twice
                # for a probe and then again for the decoder.
                reader = None
        if reader is None:
            reader = _native.DecodeSource(source)
        if seek is not None:
            reader.seek(seek)
    except _native.FFmpegError as exc:
        if reader is not None:
            reader.cleanup()
        raise FFmpegSourceError(str(exc)) from exc
    return reader


class AudioSource:
    """Base class of this package's audio sources.

    A source hands the player 20ms of audio per :meth:`read`: 3840 bytes of 16 bit 48kHz stereo
    PCM, or one Opus packet when :meth:`is_opus` is true. Sources backed by the native core keep
    their reader in :attr:`_reader` and inherit the whole contract from here.
    """

    #: The native reader behind this source, or ``None`` for a pure Python source.
    _reader: Any = None

    def read(self) -> bytes:
        """Read the next 20ms of audio, or ``b''`` once the source is exhausted."""
        reader = self._reader
        if reader is None:
            raise NotImplementedError
        return reader.read()

    def is_opus(self) -> bool:
        """Whether :meth:`read` returns Opus packets instead of PCM."""
        reader = self._reader
        return bool(reader.is_opus()) if reader is not None else False

    def cleanup(self) -> None:
        """Release the native decoder, demuxer and their buffers.

        Called by the player when the source is exhausted, and by :meth:`__del__` — so it has to
        survive being called twice, and on an instance whose ``__init__`` raised.
        """
        reader, self._reader = getattr(self, '_reader', None), None
        if reader is not None:
            reader.cleanup()

    def __del__(self) -> None:
        self.cleanup()

    def __repr__(self) -> str:
        return f'<{type(self).__name__}>'


class PCMAudio(AudioSource):
    """Raw 16 bit 48kHz stereo PCM read from a file-like object.

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
            # A partial frame is not sendable, and `discord.py`'s player reads that as the end.
            return b''
        return data


class FFmpegPCMAudio(AudioSource):
    """A PCM source decoded and resampled in process with libav (``ac-ffmpeg``).

    Parameters
    ----------
    source: :class:`str` | :term:`py:file object`
        A path to a media file, an ``http(s)`` URL, or a file-like object. A URL is fetched with
        ``reqwest`` and handed to libav as a stream, so playback starts before the download
        finishes; ranged requests are used for seeking when the server supports them.
    seek: Optional[:class:`float`]
        Start this many seconds into the stream — the native form of ``-ss``. The input has to
        be seekable, otherwise creating the source raises
        :class:`~discord_ext_voice.FFmpegSourceError`.

    Raises
    ------
    FFmpegSourceError
        The input cannot be opened, contains no audio stream, or cannot be decoded, and the
        source could not be seeked.
    """

    def __init__(
        self, source: Any, *, seek: Optional[float] = None, **options: Any
    ) -> None:
        _reject_ffmpeg_cli_options(options)
        _validate_seek(seek)
        self._reader = _open_reader(source, seek, opus_first=False)


class FFmpegOpusAudio(AudioSource):
    """An Opus source.

    When the input already carries Opus packets (``.ogg``, ``.webm``, ``.mka``) they are passed
    through untouched and :meth:`is_opus` is true. Anything else is decoded to PCM exactly like
    :class:`FFmpegPCMAudio`, and the native :class:`~discord_ext_voice.opus.Encoder` produces the
    packets when they are sent — which is what ``discord.FFmpegOpusAudio`` asks the ``ffmpeg``
    binary to do with a second encoding pass.

    Parameters
    ----------
    source: :class:`str` | :term:`py:file object`
        A path to a media file, an ``http(s)`` URL, or a file-like object. (A file object always
        takes the decoding path: probing it for Opus would consume it.)
    seek: Optional[:class:`float`]
        Start this many seconds into the stream, see :class:`FFmpegPCMAudio`.
    """

    def __init__(
        self, source: Any, *, seek: Optional[float] = None, **options: Any
    ) -> None:
        _reject_ffmpeg_cli_options(options)
        _validate_seek(seek)
        self._reader = _open_reader(source, seek, opus_first=True)

    @staticmethod
    def probe_codec(source: Any) -> Optional[str]:
        """The codec name libav reports for ``source``, or ``None`` if it cannot be read."""
        try:
            probed = _native.probe(source)
        except _native.FFmpegError:
            return None
        return probed[0] if probed else None

    @classmethod
    async def probe(cls, source: Any) -> tuple[Optional[str], Optional[int]]:
        """Probe ``source`` for its codec and bitrate without decoding it.

        Returns ``(codec, bitrate)``. The probing is libav's, so no ``ffprobe``/``ffmpeg``
        process is started and the bitrate is always ``None`` — a transport stream does not
        carry one bitrate the way discord.py's CLI probe reports it.
        """
        loop = asyncio.get_running_loop()
        codec = await loop.run_in_executor(None, cls.probe_codec, source)
        return codec, None


class PCMVolumeTransformer(AudioSource):
    """Transforms a previous :class:`AudioSource` to have volume controls.

    This does not work on audio sources that have :meth:`AudioSource.is_opus` set to ``True``.
    The samples are scaled by the native core, so this does not need ``audioop`` (removed from
    the standard library in Python 3.13) and allocates nothing beyond the scaled frame.

    Parameters
    ----------
    original: :class:`AudioSource`
        The original AudioSource to transform.
    volume: :class:`float`
        The initial volume to set it to. See :attr:`volume` for more info.

    Raises
    ------
    TypeError
        Not an audio source.
    ClientException
        The audio source is Opus encoded.
    """

    def __init__(self, original: AT, volume: float = 1.0) -> None:
        if not is_audio_source(original):
            raise TypeError(f'expected AudioSource not {original.__class__.__name__}.')

        # Set before validating so that `cleanup()` (also reached through `AudioSource.__del__`)
        # always has something to hand back to.
        self.original: AT = original

        if original.is_opus():
            raise ClientException('AudioSource must not be Opus encoded.')

        self.volume = volume

    @property
    def volume(self) -> float:
        """Retrieves or sets the volume as a floating point percentage (``1.0`` for 100%)."""
        return self._volume

    @volume.setter
    def volume(self, value: float) -> None:
        self._volume = max(value, 0.0)

    def read(self) -> bytes:
        return _native.scale_pcm(self.original.read(), min(self._volume, 2.0))

    def cleanup(self) -> None:
        original = getattr(self, 'original', None)
        if original is not None:
            original.cleanup()

    def __repr__(self) -> str:
        return f'<{type(self).__name__} volume={self._volume} original={self.original!r}>'
