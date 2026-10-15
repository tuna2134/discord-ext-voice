"""Audio sources and the playback thread.

:class:`~discord.player.AudioSource`, :class:`~discord.player.PCMAudio`,
:class:`~discord.player.PCMVolumeTransformer` and :class:`~discord.player.AudioPlayer` are
discord.py's own classes, re-exported here for convenience.

:class:`FFmpegPCMAudio` and :class:`FFmpegOpusAudio` subclass discord.py's equivalents and add
an in-process decode path: when the input is a local file (or a file object) with no extra
``ffmpeg`` arguments, libav through ``ac-ffmpeg`` decodes and resamples it to the 48kHz stereo
``s16`` frames Discord expects, and no subprocess is started. Anything else — URLs,
``pipe=True``, custom ``executable``/``stderr``, ``before_options``/``options``, or a source
libav cannot open — falls back to discord.py's ``ffmpeg`` subprocess implementation untouched.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from typing import IO, Any, Optional

from discord.player import (  # noqa: F401  (re-exported)
    AudioPlayer,
    AudioSource,
    FFmpegAudio,
    PCMAudio,
    PCMVolumeTransformer,
)
from discord.player import FFmpegOpusAudio as _DiscordFFmpegOpusAudio
from discord.player import FFmpegPCMAudio as _DiscordFFmpegPCMAudio
from discord.utils import MISSING

from . import _native
from .errors import FFmpegSourceError

__all__ = (
    'AudioSource',
    'AudioPlayer',
    'PCMAudio',
    'PCMVolumeTransformer',
    'FFmpegAudio',
    'FFmpegPCMAudio',
    'FFmpegOpusAudio',
)


def _local_path(source: Any) -> Optional[str]:
    """The filesystem path ``source`` names, or ``None`` for file objects and URLs."""
    if isinstance(source, (str, os.PathLike)):
        text = os.fspath(source)
        if '://' not in text:
            return text
    return None


def _requests_cli(
    *,
    force_cli: bool,
    executable: str,
    pipe: bool,
    stderr: Any,
    before_options: Optional[str],
    options: Optional[str],
) -> bool:
    """Whether discord.py's ``ffmpeg`` subprocess is needed instead of the in-process decoder.

    ``before_options``/``options`` are left to the binary on purpose: it interprets the whole
    FFmpeg CLI grammar, which libav's option table cannot stand in for.
    """
    return bool(
        force_cli
        or pipe
        or executable != 'ffmpeg'
        or stderr is not None
        or before_options
        or options
    )


class _InProcessFirst:
    """Mixin giving a ``discord.player.FFmpegAudio`` subclass an in-process fast path."""

    _native = None

    def _init_native_slots(self) -> None:
        # The attribute contract `discord.player.FFmpegAudio.cleanup` relies on, so that
        # `cleanup()` and `AudioSource.__del__` stay safe when no subprocess is started.
        self._process = MISSING
        self._stdout = MISSING
        self._stdin = None
        self._stderr = None
        self._pipe_writer_thread = None
        self._pipe_reader_thread = None
        self._current_error = None
        self._stopped = False

    def read(self) -> bytes:
        if self._native is not None:
            return self._native.read()
        return super().read()

    def cleanup(self) -> None:
        native, self._native = self._native, None
        if native is not None:
            native.cleanup()
        super().cleanup()

    def _check_process_returncode(self) -> None:
        """Reap the child before asking discord.py to inspect it.

        ``discord.player.FFmpegAudio._check_process_returncode`` reads ``Popen.poll()``, which
        can still be ``None`` at the moment the pipe reports EOF, so an `ffmpeg` failure would
        be reported late (or not at all). Waiting for the exit makes it deterministic.
        """
        process: Optional[subprocess.Popen] = getattr(self, '_process', None)
        if process is not None and process is not MISSING and process.returncode is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        super()._check_process_returncode()


class FFmpegPCMAudio(_InProcessFirst, _DiscordFFmpegPCMAudio):
    """A PCM audio source, decoded in process with libav when possible.

    The constructor arguments are discord.py's, plus ``force_cli`` to always use the binary.
    """

    def __init__(
        self,
        source: Any,
        *,
        executable: str = 'ffmpeg',
        pipe: bool = False,
        stderr: Any = None,
        before_options: Optional[str] = None,
        options: Optional[str] = None,
        force_cli: bool = False,
    ) -> None:
        self._native = None
        self._init_native_slots()

        path = _local_path(source)
        if path is not None and not os.path.exists(path):
            # discord.py only surfaces this once the subprocess exits; fail immediately.
            raise FFmpegSourceError(f'unable to open {path!r}: no such file')

        use_native = not _requests_cli(
            force_cli=force_cli,
            executable=executable,
            pipe=pipe,
            stderr=stderr,
            before_options=before_options,
            options=options,
        ) and (path is not None or hasattr(source, 'read'))

        if use_native:
            try:
                self._native = _native.DecodeSource(source)
            except _native.FFmpegError:
                # Unusual container or codec: let the ffmpeg binary have a go.
                self._native = None

        if self._native is None:
            super().__init__(
                source,
                executable=executable,
                pipe=pipe,
                stderr=stderr,
                before_options=before_options,
                options=options,
            )


class FFmpegOpusAudio(_InProcessFirst, _DiscordFFmpegOpusAudio):
    """An Opus audio source.

    When the input already carries Opus packets (``.ogg``/``.webm``/``.mka``) they are passed
    through untouched. Otherwise the input is decoded in process and encoded with libopus,
    which is what ``discord.FFmpegOpusAudio`` achieves by asking FFmpeg to transcode. Sources
    that need the binary take discord.py's path unchanged.
    """

    def __init__(
        self,
        source: Any,
        *,
        executable: str = 'ffmpeg',
        pipe: bool = False,
        stderr: Any = None,
        before_options: Optional[str] = None,
        options: Optional[str] = None,
        codec: Optional[str] = None,
        bitrate: Optional[int] = None,
    ) -> None:
        self._native = None
        self._init_native_slots()

        path = _local_path(source)
        if path is not None and not os.path.exists(path):
            raise FFmpegSourceError(f'unable to open {path!r}: no such file')

        use_native = not _requests_cli(
            force_cli=False,
            executable=executable,
            pipe=pipe,
            stderr=stderr,
            before_options=before_options,
            options=options,
        ) and (path is not None or hasattr(source, 'read'))

        if use_native:
            try:
                if path is not None and self.probe_codec(path) == 'opus':
                    self._native = _native.OpusSource(source)
                else:
                    # Probing consumes a file object, so those always take the PCM path.
                    self._native = _native.DecodeSource(source)
            except _native.FFmpegError:
                self._native = None

        if self._native is None:
            super().__init__(
                source,
                executable=executable,
                pipe=pipe,
                stderr=stderr,
                before_options=before_options,
                options=options,
                codec=codec,
                bitrate=bitrate,
            )

    def is_opus(self) -> bool:
        if self._native is not None:
            return bool(self._native.is_opus())
        return super().is_opus()

    @staticmethod
    def probe_codec(source: Any) -> Optional[str]:
        """The codec name libav reports for ``source``."""
        try:
            probed = _native.probe(source)
        except _native.FFmpegError:
            return None
        return probed[0] if probed else None

    @classmethod
    async def probe(
        cls,
        source: Any,
        *,
        method: Any = None,
        executable: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[int]]:
        """Probe ``source`` for its codec and bitrate.

        Same contract as :meth:`discord.FFmpegOpusAudio.probe`, but uses libav instead of
        spawning ``ffprobe``/``ffmpeg``. ``method`` and ``executable`` are accepted and
        ignored.
        """
        loop = asyncio.get_running_loop()
        codec = await loop.run_in_executor(None, cls.probe_codec, source)
        return codec, None
