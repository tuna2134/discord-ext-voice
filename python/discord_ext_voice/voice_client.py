"""A drop-in replacement for :class:`discord.voice_client.VoiceClient`.

Use it exactly like the built-in client::

    import discord_ext_voice as dev

    voice = await channel.connect(cls=dev.VoiceClient)
    voice.play(dev.FFmpegPCMAudio('song.mp3'))

:class:`VoiceClient` *is* a :class:`discord.voice_client.VoiceClient`, so every property and
method (``guild``, ``latency``, ``move_to``, ``is_playing``, ``source``, ...) is inherited
unchanged. Only two things differ, and both exist because the work happens in Rust:

* :meth:`create_connection_state` builds :class:`~discord_ext_voice.VoiceConnectionState`,
  which drives the native websocket/UDP/DAVE session;
* :meth:`play` installs the native Opus encoder and :meth:`send_audio_packet` encodes,
  encrypts (including DAVE) and transmits entirely in Rust.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from discord.player import AudioPlayer, AudioSource
from discord.voice_client import VoiceClient as _DiscordVoiceClient
from discord.voice_client import VoiceProtocol  # noqa: F401  (re-exported)

from . import opus
from .errors import ClientException, NotConnected
from .voice_state import VoiceConnectionState

__all__ = ('VoiceClient', 'VoiceProtocol', 'has_nacl', 'has_dave')

#: Whether RTP encryption is available. Always ``True``: it is implemented natively with
#: RustCrypto instead of PyNaCl.
has_nacl: bool = True

#: Whether DAVE end-to-end encryption is available. Always ``True``: the MLS session is
#: implemented natively with the ``davey`` crate.
has_dave: bool = True


class VoiceClient(_DiscordVoiceClient):
    """Represents a Discord voice connection backed by the native transport."""

    #: Both capabilities come from the native core, so discord.py's PyNaCl/davey warnings do
    #: not apply to this subclass.
    warn_nacl: bool = False
    warn_dave: bool = False

    # ------------------------------------------------------------------ construction

    def create_connection_state(self) -> VoiceConnectionState:
        """Build the connection state that drives the native transport."""
        return VoiceConnectionState(self)

    @property
    def native(self) -> Any:
        """The underlying :class:`discord_ext_voice._native.VoiceConnection`."""
        return self._connection.native

    # ------------------------------------------------------------------ audio

    def play(
        self,
        source: AudioSource,
        *,
        after: Optional[Callable[[Optional[Exception]], Any]] = None,
        application: str = 'audio',
        bitrate: int = 128,
        fec: bool = True,
        expected_packet_loss: float = 0.15,
        bandwidth: str = 'full',
        signal_type: str = 'auto',
    ) -> None:
        """Plays an :class:`AudioSource`.

        Identical to :meth:`discord.VoiceClient.play`, except that the encoder is the native
        one, so no ``discord.opus.load_opus`` call is required. The finalizer ``after`` is
        called after the source has been exhausted or an error occurred.
        """
        if not self.is_connected():
            raise ClientException('Not connected to voice.')

        if self.is_playing():
            raise ClientException('Already playing audio.')

        if not isinstance(source, AudioSource):
            raise TypeError(f'source must be an AudioSource not {source.__class__.__name__}')

        if not source.is_opus():
            # Validates the parameters and raises ValueError, exactly like discord.opus.Encoder.
            self.encoder = opus.Encoder(
                application=application,
                bitrate=bitrate,
                fec=fec,
                expected_packet_loss=expected_packet_loss,
                bandwidth=bandwidth,
                signal_type=signal_type,
            )

        self._player = AudioPlayer(source, self, after=after)
        self._player.start()

    def send_audio_packet(self, data: bytes, *, encode: bool = True) -> None:
        """Sends an audio packet composed of the data.

        The packet is Opus-encoded (when ``encode`` is set), DAVE-encrypted when the session
        is ready, RTP-encrypted and transmitted by the native transport.

        Parameters
        ----------
        data: :class:`bytes`
            The :term:`py:bytes-like object` denoting PCM or Opus voice data.
        encode: :class:`bool`
            Indicates if ``data`` should be encoded into Opus.
        """
        encoder = self.encoder if isinstance(self.encoder, opus.Encoder) else None
        try:
            self._connection.native.send_audio_packet(data, encode=encode, encoder=encoder)
        except NotConnected as exc:
            raise ClientException(str(exc)) from exc
