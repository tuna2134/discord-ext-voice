"""The playback clock, implemented by the native core.

:class:`AudioPlayer` is a thin wrapper over :class:`discord_ext_voice._native.AudioPlayer`: the
Rust side owns the thread, the 20ms pacing, the ``SPEAKING`` announcements, the silence frames
and the reconnection wait, and calls back into Python only for the audio source and the ``after``
callback. Nothing from :mod:`discord.player` is used, so ``discord.py``'s ``audioop`` based
player (and its ``ffmpeg`` subprocess handling) stays out of the picture.

The public surface matches :class:`discord.player.AudioPlayer` — ``DELAY``, ``source``,
``start``, ``stop``, ``pause``, ``resume``, ``is_playing``, ``is_paused``, ``set_source`` — so a
player can be constructed the same way as discord.py's.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, TypeVar

from . import _native
from ._native import is_audio_source

__all__ = ('AudioPlayer',)

AT = TypeVar('AT')


class AudioPlayer:
    """Plays an :class:`~discord_ext_voice.AudioSource` on a :class:`VoiceClient`.

    Parameters
    ----------
    source: :class:`~discord_ext_voice.AudioSource`
        The source to play: anything providing ``read`` and ``is_opus``.
    client: :class:`discord_ext_voice.VoiceClient`
        The voice client whose connection the frames are sent on.
    after: Optional[Callable[[Optional[:class:`Exception`]], Any]]
        Called once when playback finishes, with the error if it failed and ``None`` otherwise.

    Raises
    ------
    TypeError
        The source is not playable, or ``after`` is not callable.
    """

    #: The number of seconds between two frames: 20ms, ``discord.py``'s value as well.
    DELAY: float = _native.AudioPlayer.DELAY

    def __init__(
        self,
        source: AT,
        client: Any,
        *,
        after: Optional[Callable[[Optional[Exception]], Any]] = None,
    ) -> None:
        if not is_audio_source(source):
            raise TypeError(f'source must be an AudioSource not {source.__class__.__name__}')
        if after is not None and not callable(after):
            raise TypeError('Expected a callable for the "after" parameter.')

        self.client: Any = client
        self.after: Optional[Callable[[Optional[Exception]], Any]] = after
        self._native = _native.AudioPlayer(
            client.native,
            source,
            after=after,
            timeout=client.timeout,
        )

    @property
    def source(self) -> AT:
        """:class:`~discord_ext_voice.AudioSource`: The source being played.

        Setting it swaps the source of a running player, exactly like
        :meth:`set_source`.
        """
        return self._native.source

    @source.setter
    def source(self, value: AT) -> None:
        self.set_source(value)

    def start(self) -> None:
        """Start playback on the native playback thread."""
        self._native.start()

    def stop(self) -> None:
        """Stop playing and announce that we are no longer speaking."""
        self._native.stop()

    def pause(self, *, update_speaking: bool = True) -> None:
        """Pause playback, keeping the jitter buffer fed with silence."""
        self._native.pause(update_speaking)

    def resume(self, *, update_speaking: bool = True) -> None:
        """Resume a paused player, restarting its clock."""
        self._native.resume(update_speaking)

    def is_playing(self) -> bool:
        """Indicates if we're currently playing audio."""
        return self._native.is_playing()

    def is_paused(self) -> bool:
        """Indicates if we're playing audio, but if we're paused."""
        return self._native.is_paused()

    def is_alive(self) -> bool:
        """Whether the playback thread is still running."""
        return self._native.is_alive()

    def set_source(self, source: AT) -> None:
        """Change the source being played without touching the speaking state."""
        if not is_audio_source(source):
            raise TypeError(f'source must be an AudioSource not {source.__class__.__name__}')
        self._native.set_source(source)

    def join(self, timeout: Optional[float] = None) -> None:
        """Wait for the playback thread to finish, at most ``timeout`` seconds."""
        self._native.join(timeout)

    def __repr__(self) -> str:
        return f'<discord_ext_voice.AudioPlayer playing={self.is_playing()} paused={self.is_paused()}>'
