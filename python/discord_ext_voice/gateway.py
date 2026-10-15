"""The voice websocket facade, mirroring :class:`discord.gateway.DiscordVoiceWebSocket`.

The real socket, heartbeat and DAVE handling live in Rust; this class only provides the
opcode constants and the coroutine surface that ``discord.py`` code expects.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Optional

from ._compat import strip_scheme
from .enums import SpeakingState

if TYPE_CHECKING:
    from .voice_state import VoiceConnectionState

__all__ = ('DiscordVoiceWebSocket',)


class DiscordVoiceWebSocket:
    """Implements the websocket protocol for handling voice connections.

    Attributes
    -----------
    IDENTIFY
        Send only. Starts a new voice session.
    SELECT_PROTOCOL
        Send only. Tells discord what encryption mode and how to connect for voice.
    READY
        Receive only. Tells the websocket that the initial connection has completed.
    HEARTBEAT
        Send only. Keeps your websocket connection alive.
    SESSION_DESCRIPTION
        Receive only. Gives you the secret key required for voice.
    SPEAKING
        Send only. Notifies the client if you are currently speaking.
    HEARTBEAT_ACK
        Receive only. Tells you your heartbeat has been acknowledged.
    RESUME
        Sent only. Tells the client to resume its session.
    HELLO
        Receive only. Tells you that your websocket connection was acknowledged.
    RESUMED
        Sent only. Tells you that your RESUME request has succeeded.
    CLIENT_CONNECT
        Indicates a user has connected to voice.
    CLIENT_DISCONNECT
        Receive only. Indicates a user has disconnected from voice.
    """

    # fmt: off
    IDENTIFY                       = 0
    SELECT_PROTOCOL                = 1
    READY                          = 2
    HEARTBEAT                      = 3
    SESSION_DESCRIPTION            = 4
    SPEAKING                       = 5
    HEARTBEAT_ACK                  = 6
    RESUME                         = 7
    HELLO                          = 8
    RESUMED                        = 9
    CLIENTS_CONNECT                = 11
    CLIENT_CONNECT                 = 12
    CLIENT_DISCONNECT              = 13
    DAVE_PREPARE_TRANSITION        = 21
    DAVE_EXECUTE_TRANSITION        = 22
    DAVE_TRANSITION_READY          = 23
    DAVE_PREPARE_EPOCH             = 24
    MLS_EXTERNAL_SENDER            = 25
    MLS_KEY_PACKAGE                = 26
    MLS_PROPOSALS                  = 27
    MLS_COMMIT_WELCOME             = 28
    MLS_ANNOUNCE_COMMIT_TRANSITION = 29
    MLS_WELCOME                    = 30
    MLS_INVALID_COMMIT_WELCOME     = 31
    # fmt: on

    def __init__(self, connection: Any, state: Optional['VoiceConnectionState'] = None) -> None:
        self._connection = connection
        self._state = state

    @property
    def gateway(self) -> Optional[str]:
        state = self._state
        native = getattr(state, '_native', None)
        raw = native.endpoint if native is not None else None
        if raw:
            scheme = 'ws' if raw.startswith('ws://') else 'wss'
            return f'{scheme}://{strip_scheme(raw)}/?v=8'
        endpoint = getattr(state, 'endpoint', None)
        return f'wss://{endpoint}/?v=8' if endpoint else None

    @property
    def seq_ack(self) -> int:
        return int(self._connection.seq_ack)

    @property
    def secret_key(self) -> Optional[list[int]]:
        key = self._connection.secret_key
        return list(key) if key else None

    @property
    def latency(self) -> float:
        """Latency between a HEARTBEAT and a HEARTBEAT_ACK in seconds."""
        return float(self._connection.latency)

    @property
    def average_latency(self) -> float:
        """Average of the most recent 20 HEARTBEAT latencies in seconds."""
        return float(self._connection.average_latency)

    async def send_as_json(self, data: Any) -> None:
        self._connection.send_json_text(json.dumps(data))

    async def send_binary(self, opcode: int, data: bytes) -> None:
        self._connection.send_binary_op(opcode, bytes(data))

    # `discord.py` aliases the heartbeat to a plain JSON frame.
    send_heartbeat = send_as_json

    async def speak(self, state: SpeakingState = SpeakingState.voice) -> None:
        self._connection.speak(int(state))

    async def close(self, code: int = 1000) -> None:
        await self._connection.close(code)

    def __repr__(self) -> str:
        return f'<DiscordVoiceWebSocket seq_ack={self.seq_ack}>'
