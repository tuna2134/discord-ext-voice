"""A drop-in replacement for :class:`discord.voice_client.VoiceClient`.

Use it exactly like the built-in client::

    import discord_ext_voice

    voice = await channel.connect(cls=discord_ext_voice.VoiceClient)
    voice.play(discord_ext_voice.FFmpegPCMAudio('song.mp3'))

Everything below the API surface — the voice websocket, the RTP encryption, the DAVE/MLS
session, Opus and FFmpeg — is implemented in Rust.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional, Tuple

from . import _native, opus
from .errors import ClientException, DaveError, NotConnected, OpusNotLoaded
from .player import AudioPlayer, AudioSource
from .voice_state import DAVE_PROTOCOL_VERSION, VoiceConnectionState

if TYPE_CHECKING:
    from .gateway import DiscordVoiceWebSocket

__all__ = ('VoiceProtocol', 'VoiceClient', 'has_nacl', 'has_dave')

_log = logging.getLogger(__name__)

has_nacl: bool = True
"""Whether the RTP encryption is available.

Always ``True``: the encryption is implemented natively with RustCrypto instead of PyNaCl.
"""

has_dave: bool = True
"""Whether DAVE end-to-end encryption is available.

Always ``True``: the MLS session is implemented natively with the ``davey`` crate.
"""

_tt = Tuple[int, ...]

try:  # pragma: no cover - depends on whether discord.py is installed
    from discord.voice_client import VoiceProtocol as _DiscordVoiceProtocol
except Exception:  # pragma: no cover
    _DiscordVoiceProtocol = None


class VoiceProtocol:
    """Mirror of :class:`discord.voice_client.VoiceProtocol` for standalone use."""

    def __init__(self, client: Any, channel: Any) -> None:
        self.client: Any = client
        self.channel: Any = channel

    async def on_voice_state_update(self, data: dict, /) -> None:
        raise NotImplementedError

    async def on_voice_server_update(self, data: dict, /) -> None:
        raise NotImplementedError

    async def connect(
        self, *, timeout: float, reconnect: bool, self_deaf: bool = False, self_mute: bool = False
    ) -> None:
        raise NotImplementedError

    async def disconnect(self, *, force: bool) -> None:
        raise NotImplementedError

    def cleanup(self) -> None:
        key_id, _ = self.channel._get_voice_client_key()
        self.client._connection._remove_voice_client(key_id)


_Base = _DiscordVoiceProtocol if _DiscordVoiceProtocol is not None else VoiceProtocol


class VoiceClient(_Base):  # type: ignore[valid-type,misc]
    """Represents a Discord voice connection.

    Parameters
    ----------
    client: :class:`discord.Client`
        The client that started the connection request.
    channel: :class:`discord.abc.Connectable`
        The voice channel being connected to.
    """

    warn_nacl: bool = not has_nacl
    warn_dave: bool = not has_dave
    #: The encryption modes this library advertises, in preference order. Sourced from the
    #: native core so the Python and Rust sides can never disagree.
    supported_modes: Tuple[str, ...] = tuple(_native.SUPPORTED_MODES)

    def __init__(self, client: Any, channel: Any) -> None:
        if not has_nacl:
            raise RuntimeError('PyNaCl library needed in order to use voice')
        if not has_dave:
            raise RuntimeError('davey library needed in order to use voice')

        super().__init__(client, channel)
        state = client._connection
        self.server_id: int = 0
        self.loop = state.loop
        self._state = state

        self.sequence: int = 0
        self.timestamp: int = 0
        self._player: Optional[AudioPlayer] = None
        self.encoder: Any = None
        self._incr_nonce: int = 0

        self._connection: VoiceConnectionState = self.create_connection_state()

    # ------------------------------------------------------------------ attributes

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
        return self._connection.session_id

    @property
    def token(self) -> Optional[str]:
        return self._connection.token

    @property
    def endpoint(self) -> Optional[str]:
        return self._connection.endpoint

    @property
    def ssrc(self) -> int:
        return self._connection.ssrc

    @property
    def mode(self) -> Optional[str]:
        return self._connection.mode

    @property
    def secret_key(self) -> Optional[list[int]]:
        return self._connection.secret_key

    @property
    def ws(self) -> 'DiscordVoiceWebSocket':
        return self._connection.ws

    @property
    def timeout(self) -> float:
        return self._connection.timeout

    @property
    def voice_privacy_code(self) -> Optional[str]:
        """:class:`str`: The voice privacy code of this E2EE session's group.

        A new privacy code is created each time a new transition is executed. This is ``None``
        when there is no active DAVE session.
        """
        return self._connection._native.voice_privacy_code

    @property
    def native(self) -> Any:
        """The underlying :class:`discord_ext_voice._native.VoiceConnection`."""
        return self._connection._native

    def checked_add(self, attr: str, value: int, limit: int) -> None:
        val = getattr(self, attr)
        if val + value > limit:
            setattr(self, attr, 0)
        else:
            setattr(self, attr, val + value)

    # ------------------------------------------------------------------ connection

    def create_connection_state(self) -> VoiceConnectionState:
        return VoiceConnectionState(self)

    async def on_voice_state_update(self, data: dict, /) -> None:
        await self._connection.voice_state_update(data)

    async def on_voice_server_update(self, data: dict, /) -> None:
        await self._connection.voice_server_update(data)

    async def connect(
        self, *, reconnect: bool, timeout: float, self_deaf: bool = False, self_mute: bool = False
    ) -> None:
        await self._connection.connect(
            reconnect=reconnect, timeout=timeout, self_deaf=self_deaf, self_mute=self_mute
        )

    def wait_until_connected(self, timeout: Optional[float] = 30.0) -> bool:
        self._connection.wait(timeout)
        return self._connection.is_connected()

    @property
    def latency(self) -> float:
        """:class:`float`: Latency between a HEARTBEAT and a HEARTBEAT_ACK in seconds."""
        return float(self._connection._native.latency)

    @property
    def average_latency(self) -> float:
        """:class:`float`: Average of the most recent 20 HEARTBEAT latencies in seconds."""
        return float(self._connection._native.average_latency)

    async def disconnect(self, *, force: bool = False) -> None:
        """|coro|

        Disconnects this voice client from voice.
        """
        self.stop()
        await self._connection.disconnect(force=force, wait=True)
        self.cleanup()

    async def move_to(self, channel: Optional[Any], *, timeout: Optional[float] = 30.0) -> None:
        """|coro|

        Moves you to a different voice channel.
        """
        await self._connection.move_to(channel, timeout)

    def is_connected(self) -> bool:
        """Indicates if the voice client is connected to voice."""
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
        occurred. Encoder parameters are only used for PCM based sources.
        """
        if not self.is_connected():
            raise ClientException('Not connected to voice.')

        if self.is_playing():
            raise ClientException('Already playing audio.')

        if not isinstance(source, AudioSource):
            raise TypeError(f'source must be an AudioSource not {source.__class__.__name__}')

        if not source.is_opus():
            try:
                self.encoder = opus.Encoder(
                    application=application,
                    bitrate=bitrate,
                    fec=fec,
                    expected_packet_loss=expected_packet_loss,
                    bandwidth=bandwidth,
                    signal_type=signal_type,
                )
            except opus.OpusError as exc:  # pragma: no cover - only on a broken build
                raise OpusNotLoaded() from exc

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
        if not isinstance(value, AudioSource):
            raise TypeError(f'expected AudioSource not {value.__class__.__name__}.')

        if self._player is None:
            raise ValueError('Not playing anything.')

        self._player.set_source(value)

    def send_audio_packet(self, data: bytes, *, encode: bool = True) -> None:
        """Sends an audio packet composed of the data.

        Parameters
        ----------
        data: :class:`bytes`
            The bytes-like object denoting PCM or Opus voice data.
        encode: :class:`bool`
            Indicates if ``data`` should be encoded into Opus.
        """
        try:
            self._connection._native.send_audio_packet(data, encode=encode, encoder=self.encoder)
        except NotConnected as exc:
            raise ClientException(str(exc)) from exc

    # ------------------------------------------------------------------ DAVE

    @property
    def dave_protocol_version(self) -> int:
        """The DAVE protocol version currently in use (``0`` when E2EE is off)."""
        return int(self._connection._native.dave_protocol_version)

    def __repr__(self) -> str:
        return (
            f'<VoiceClient guild={getattr(self.channel.guild, "id", None)} '
            f'channel={getattr(self.channel, "id", None)} connected={self.is_connected()}>'
        )
