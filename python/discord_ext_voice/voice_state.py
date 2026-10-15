"""discord.py's voice connection state machine, wired to the native transport.

:class:`VoiceConnectionState` subclasses :class:`discord.voice_state.VoiceConnectionState` and
overrides only the points that touch the native core:

* the UDP socket and the websocket (both owned by Rust),
* IP discovery, the session description and the DAVE session,
* the encryption key, SSRC and latency accessors.

Everything else — ``voice_state_update``/``voice_server_update``, the reconnect policy in
``_poll_voice_ws``, ``move_to``, the disconnect handshake and ``wait``/``wait_async`` — is
inherited unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from discord.errors import ConnectionClosed
from discord.voice_state import ConnectionFlowState
from discord.voice_state import VoiceConnectionState as _VoiceConnectionState

from . import _native
from .gateway import DiscordVoiceWebSocket

__all__ = ('VoiceConnectionState', 'ConnectionFlowState', 'DAVE_PROTOCOL_VERSION')

_log = logging.getLogger(__name__)

#: The highest DAVE protocol version this build advertises and negotiates.
DAVE_PROTOCOL_VERSION: int = int(_native.DAVE_PROTOCOL_VERSION)


class VoiceConnectionState(_VoiceConnectionState):
    """The connection flow state machine from discord.py, driven by the native transport."""

    def __init__(self, voice_client: Any, *, hook: Optional[Callable[..., Any]] = None) -> None:
        super().__init__(voice_client, hook=hook)

        channel = voice_client.channel
        user = voice_client.user
        self._native = _native.VoiceConnection(
            int(getattr(channel.guild, 'id', 0) or 0),
            int(getattr(user, 'id', 0) or 0),
            int(channel.id),
        )
        #: Whether the next websocket connection must send RESUME instead of IDENTIFY.
        self._resume = False

    # ------------------------------------------------------------------ native state

    @property
    def native(self) -> _native.VoiceConnection:
        """The native connection implementing this state: websocket, UDP, RTP and DAVE."""
        return self._native

    @property
    def state(self) -> ConnectionFlowState:
        return self._state

    @state.setter
    def state(self, state: ConnectionFlowState) -> None:
        _VoiceConnectionState.state.fset(self, state)  # type: ignore[union-attr]
        # discord.py's `ConnectionFlowState` is not an `IntEnum`, so use `.value`.
        self._native.set_phase(int(state.value))

    @property
    def dave_protocol_version(self) -> int:
        """The DAVE protocol version in use (``0`` when end-to-end encryption is off)."""
        return int(self._native.dave_protocol_version)

    @dave_protocol_version.setter
    def dave_protocol_version(self, value: int) -> None:
        # The native session owns the negotiation, so discord.py's bookkeeping writes are
        # ignored; the getter above always reports the live value.
        pass

    @property
    def max_dave_protocol_version(self) -> int:
        return DAVE_PROTOCOL_VERSION

    @property
    def can_encrypt(self) -> bool:
        return bool(self._native.can_encrypt())

    @property
    def dave_session(self) -> Optional[_native.VoiceConnection]:
        """The native DAVE session handle, or ``None`` when E2EE is inactive."""
        return self._native if self._native.has_dave_session() else None

    @dave_session.setter
    def dave_session(self, value: Any) -> None:
        # See `dave_protocol_version`: the native session is the source of truth.
        pass

    # ------------------------------------------------------------------ gateway events

    async def voice_state_update(self, data: dict) -> None:
        if data.get('channel_id') is not None:
            self._native.set_session_id(data.get('session_id'))
        await super().voice_state_update(data)

    async def voice_server_update(self, data: dict) -> None:
        # Feed the native transport *before* discord.py's handler runs: that handler can
        # schedule a reconnect, which would otherwise race with this update.
        token = data.get('token')
        endpoint = data.get('endpoint')
        if token is not None and endpoint is not None:
            self._native.set_server_info(int(data['guild_id']), token, endpoint)
        await super().voice_server_update(data)

    # ------------------------------------------------------------------ transport wiring

    def _create_socket(self) -> None:
        """The native transport owns the UDP socket, so there is nothing to create here."""

    async def _connect_websocket(self, resume: bool) -> DiscordVoiceWebSocket:
        """Return the websocket facade; the native handshake happens in `_handshake_websocket`."""
        self._resume = resume
        self.state = ConnectionFlowState.websocket_connected
        return DiscordVoiceWebSocket(self._native, self)

    async def _handshake_websocket(self) -> None:
        """Complete the voice handshake: IDENTIFY/RESUME, UDP discovery, key negotiation."""
        try:
            await self._native.handshake(resume=self._resume, timeout=self.timeout)
        except _native.HandshakeError as exc:
            # discord.py treats a connect timeout as `asyncio.TimeoutError` (and retries).
            raise asyncio.TimeoutError(str(exc)) from exc
        except _native.ConnectionClosedError as exc:
            raise ConnectionClosed(
                self.ws, shard_id=None, code=self._native.close_code or None
            ) from exc

        self.endpoint_ip = self._native.ip
        self.ssrc = self._native.ssrc
        self.ip = self._native.ip
        self.port = self._native.port
        self.voice_port = self._native.voice_port
        self.mode = self._native.mode
        self.secret_key = self._native.secret_key
        self.state = ConnectionFlowState.got_ip_discovery
        self.state = ConnectionFlowState.connected

    def send_packet(self, packet: bytes) -> None:
        self._native.send_packet(packet)

    def add_socket_listener(self, callback: Callable[[bytes], Any]) -> None:
        """Not supported: this library implements the sending side of voice only."""
        raise NotImplementedError('receiving voice packets is not implemented')

    def remove_socket_listener(self, callback: Callable[[bytes], Any]) -> None:
        """Not supported: this library implements the sending side of voice only."""
        raise NotImplementedError('receiving voice packets is not implemented')

    # ------------------------------------------------------------------ DAVE

    async def reinit_dave_session(self) -> None:
        self._native.reinit_dave_session()

    async def _recover_from_invalid_commit(self, transition_id: int) -> None:
        # The native session answers with `dave_mls_invalid_commit_welcome` itself; asking it
        # to re-create the session is the Python-side equivalent of discord.py's recovery.
        self._native.reinit_dave_session()

    async def _execute_transition(self, transition_id: int) -> None:
        # Transitions are executed by the native reader task as the opcodes arrive.
        return None
