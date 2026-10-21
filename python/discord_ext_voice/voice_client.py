"""A `discord.py` voice backend implemented with :class:`discord.VoiceProtocol`.

``VoiceProtocol`` is discord.py's documented extension point for third-party voice backends
("This class allows you to implement a protocol to allow for an external method of sending
voice, such as Lavalink or a native library implementation"). This module implements it on top
of the native transport, so nothing from discord.py's own `VoiceClient` — its PyNaCl-based
encryption, its Python `davey` session, its capability guards — is inherited or worked around.

The connection flow is :class:`discord_ext_voice.VoiceConnectionState`, the playback clock is
:class:`discord_ext_voice.AudioPlayer` (a native thread) and the audio sources are
:mod:`discord_ext_voice.sources`; all three are this package's own. discord.py provides the host
framework — the client, the gateway, and the `VoiceProtocol` extension point this class
implements.

Usage is unchanged::

    import discord_ext_voice as dev

    voice = await channel.connect(cls=dev.VoiceClient)
    voice.play(dev.FFmpegPCMAudio('song.mp3'))
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Tuple

from discord.voice_client import VoiceClient as _DiscordVoiceClient
from discord.voice_client import VoiceProtocol

from .errors import ClientException, NotConnected
from .player import AudioPlayer
from .sources import AudioSource, is_audio_source
from .voice_state import VoiceConnectionState

__all__ = ('VoiceClient', 'VoiceProtocol', 'has_nacl', 'has_dave')

_log = logging.getLogger(__name__)

#: Whether RTP encryption is available. Always ``True``: it is implemented natively with
#: RustCrypto instead of PyNaCl.
has_nacl: bool = True

#: Whether DAVE end-to-end encryption is available. Always ``True``: the MLS session is
#: implemented natively with the ``davey`` crate.
has_dave: bool = True


class VoiceClient(VoiceProtocol):
    """Represents a Discord voice connection backed by the native transport.

    Parameters
    ----------
    client: :class:`discord.Client`
        The client that started the connection request.
    channel: :class:`discord.abc.Connectable`
        The voice channel being connected to.
    """

    #: The encryption modes this backend advertises. Read from :class:`discord.VoiceClient` so
    #: the two can never disagree; the test suite also asserts that the native mode list
    #: (:data:`discord_ext_voice.SUPPORTED_MODES`) matches it.
    supported_modes: Tuple[str, ...] = _DiscordVoiceClient.supported_modes

    def __init__(self, client: Any, channel: Any) -> None:
        super().__init__(client, channel)
        state = client._connection
        self.loop = state.loop
        self._state = state
        self._player: Optional[AudioPlayer] = None
        #: The Opus encoder, set by :meth:`play` for PCM sources and handed to the native
        #: transport for every packet.
        self._connection: VoiceConnectionState = self.create_connection_state()

    # ------------------------------------------------------------------ state accessors

    def create_connection_state(self) -> VoiceConnectionState:
        """Build the connection state that drives the native transport."""
        return VoiceConnectionState(self)

    @property
    def native(self) -> Any:
        """The underlying :class:`discord_ext_voice._native.VoiceConnection`."""
        return self._connection.native

    @property
    def guild(self) -> Any:
        """:class:`discord.Guild`: The guild we're connected to."""
        return self.channel.guild

    @property
    def user(self) -> Any:
        """:class:`discord.ClientUser`: The user connected to voice (i.e. ourselves)."""
        return self._state.user

    @property
    def session_id(self) -> Optional[str]:
        """Optional[:class:`str`]: The voice connection session ID."""
        return self._connection.session_id

    @property
    def endpoint(self) -> Optional[str]:
        """Optional[:class:`str`]: The endpoint we are connecting to."""
        return self._connection.endpoint

    @property
    def ssrc(self) -> int:
        """:class:`int`: The RTP synchronisation source Discord assigned to us."""
        return self._connection.ssrc

    @property
    def mode(self) -> Optional[str]:
        """Optional[:class:`str`]: The negotiated RTP encryption mode."""
        return self._connection.mode

    @property
    def ws(self) -> Any:
        """:class:`discord_ext_voice.DiscordVoiceWebSocket`: The voice websocket facade."""
        return self._connection.ws

    @property
    def timeout(self) -> float:
        """:class:`float`: How long to wait for the connection flow, in seconds."""
        return self._connection.timeout

    @property
    def latency(self) -> float:
        """:class:`float`: Latency between a HEARTBEAT and a HEARTBEAT_ACK in seconds."""
        return self._connection.native.latency

    @property
    def average_latency(self) -> float:
        """:class:`float`: Average of the most recent 20 HEARTBEAT latencies in seconds."""
        return self._connection.native.average_latency

    @property
    def voice_privacy_code(self) -> Optional[str]:
        """Optional[:class:`str`]: The voice privacy code of this E2EE session's group.

        A new privacy code is created each time a new transition is executed. This is ``None``
        when there is no active DAVE session.
        """
        return self._connection.native.voice_privacy_code

    # ------------------------------------------------------------------ connection

    async def on_voice_state_update(self, data: dict, /) -> None:
        """|coro|

        Called by discord.py for every ``VOICE_STATE_UPDATE`` payload addressed to us.
        """
        await self._connection.voice_state_update(data)

    async def on_voice_server_update(self, data: dict, /) -> None:
        """|coro|

        Called by discord.py for every ``VOICE_SERVER_UPDATE`` payload addressed to us.
        """
        await self._connection.voice_server_update(data)

    async def connect(
        self, *, reconnect: bool, timeout: float, self_deaf: bool = False, self_mute: bool = False
    ) -> None:
        """|coro|

        Connects to voice and completes the handshake.
        """
        await self._connection.connect(
            reconnect=reconnect,
            timeout=timeout,
            self_deaf=self_deaf,
            self_mute=self_mute,
            resume=False,
        )

    async def disconnect(self, *, force: bool = False) -> None:
        """|coro|

        Disconnects this voice client from voice.
        """
        self.stop()
        _log.info('Disconnecting from voice (force=%s)', force)
        await self._connection.disconnect(force=force, wait=True)
        self.cleanup()
        _log.info('Disconnected from voice')

    async def move_to(self, channel: Optional[Any], *, timeout: Optional[float] = 30.0) -> None:
        """|coro|

        Moves you to a different voice channel.
        """
        await self._connection.move_to(channel, timeout)

    def is_connected(self) -> bool:
        """Indicates if the voice client is connected to voice."""
        return self._connection.is_connected()

    def wait_until_connected(self, timeout: Optional[float] = 30.0) -> bool:
        """Block until the connection is established (or ``timeout`` expires)."""
        self._connection.wait(timeout)
        return self._connection.is_connected()

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

        The finalizer ``after`` is called after the source has been exhausted or an error
        occurred. The encoder parameters are only used for PCM based sources; the native player
        validates them and raises :class:`ValueError` for a bad value.
        """
        if not self.is_connected():
            raise ClientException('Not connected to voice.')

        if self.is_playing():
            raise ClientException('Already playing audio.')

        # Any `discord.AudioSource` (or duck-typed equivalent) is accepted, not only the
        # sources this package ships, so a user's own source keeps working.
        if not is_audio_source(source):
            raise TypeError(f'source must be an AudioSource not {source.__class__.__name__}')

        if source.is_opus():
            # The packets already are Opus: no encoder is involved at all.
            self._connection.native.clear_encoder()
        else:
            self._connection.native.set_encoder(
                application,
                bitrate,
                fec,
                expected_packet_loss,
                bandwidth,
                signal_type,
            )

        _log.info(
            'Playing %r (opus passthrough=%s, encoder=%s)',
            source,
            source.is_opus(),
            'none' if source.is_opus() else 'native',
        )
        self._player = AudioPlayer(source, self, after=after)
        self._player.start()

    def is_playing(self) -> bool:
        """Indicates if we're currently playing audio."""
        return self._player is not None and self._player.is_playing()

    def is_paused(self) -> bool:
        """Indicates if we're playing audio, but if we're paused."""
        return self._player is not None and self._player.is_paused()

    def stop(self) -> None:
        """Stops playing audio."""
        if self._player:
            self._player.stop()
            self._player = None

    def pause(self) -> None:
        """Pauses the audio playing."""
        if self._player:
            self._player.pause()

    def resume(self) -> None:
        """Resumes the audio playing."""
        if self._player:
            self._player.resume()

    @property
    def source(self) -> Optional[AudioSource]:
        """Optional[:class:`AudioSource`]: The audio source being played, if playing.

        This property can also be used to change the audio source currently being played.
        """
        return self._player.source if self._player else None

    @source.setter
    def source(self, value: AudioSource) -> None:
        if not is_audio_source(value):
            raise TypeError(f'expected AudioSource not {value.__class__.__name__}.')

        if self._player is None:
            raise ValueError('Not playing anything.')

        self._player.set_source(value)

    def send_audio_packet(self, data: bytes, *, encode: bool = True) -> None:
        """Sends an audio packet composed of the data.

        The packet is Opus-encoded (when ``encode`` is set), DAVE-encrypted when the session is
        ready, RTP-encrypted and transmitted — all by the native transport.

        Parameters
        ----------
        data: :class:`bytes`
            The :term:`py:bytes-like object` denoting PCM or Opus voice data.
        encode: :class:`bool`
            Indicates if ``data`` should be encoded into Opus.
        """
        _log.debug(
            'send_audio_packet: %d bytes, encode=%s, dave=%s',
            len(data),
            encode,
            self._connection.native.dave_protocol_version > 0,
        )
        try:
            self._connection.native.send_audio_packet(data, encode=encode)
        except NotConnected as exc:
            raise ClientException(str(exc)) from exc

    def __repr__(self) -> str:
        return (
            f'<VoiceClient guild={getattr(self.channel.guild, "id", None)} '
            f'channel={getattr(self.channel, "id", None)} connected={self.is_connected()}>'
        )
