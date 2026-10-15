"""The voice websocket facade.

:class:`DiscordVoiceWebSocket` is :class:`discord.gateway.DiscordVoiceWebSocket` with the
socket swapped for the native transport. Every opcode constant and every outgoing frame
builder (``identify``, ``select_protocol``, ``speak``, ``resume``, ``send_transition_ready``,
``send_as_json``, ``send_binary``) is therefore inherited unchanged; only the read side is
replaced, because the native reader consumes and answers frames itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from discord.errors import ConnectionClosed
from discord.gateway import DiscordVoiceWebSocket as _DiscordVoiceWebSocket

from . import _native

if TYPE_CHECKING:
    from .voice_state import VoiceConnectionState

__all__ = ('DiscordVoiceWebSocket',)


class _NativeSocket:
    """The slice of aiohttp's websocket interface that ``discord.gateway`` uses."""

    __slots__ = ('_connection', 'close_code')

    def __init__(self, connection: _native.VoiceConnection) -> None:
        self._connection = connection
        self.close_code: Optional[int] = None

    async def send_str(self, data: str) -> None:
        self._connection.send_json_text(data)

    async def send_bytes(self, data: bytes) -> None:
        # discord.py prefixes the opcode itself: `bytes([opcode]) + payload`.
        self._connection.send_binary_op(data[0], data[1:])

    async def close(self, *, code: int = 1000) -> None:
        self.close_code = code
        await self._connection.close(code)


class DiscordVoiceWebSocket(_DiscordVoiceWebSocket):
    """The voice websocket surface from discord.py, backed by the Rust transport."""

    def __init__(self, connection: _native.VoiceConnection, state: 'VoiceConnectionState') -> None:
        self._native = connection
        super().__init__(_NativeSocket(connection), state.voice_client.loop)
        self._connection = state

    # ------------------------------------------------------------------ native state

    @property
    def close_code(self) -> Optional[int]:
        return self._close_code

    @property
    def seq_ack(self) -> int:
        return int(self._native.seq_ack)

    @seq_ack.setter
    def seq_ack(self, value: int) -> None:
        # The native reader tracks the acknowledged sequence number.
        pass

    @property
    def secret_key(self) -> Optional[list[int]]:
        key = self._native.secret_key
        return list(key) if key else None

    @secret_key.setter
    def secret_key(self, value: object) -> None:
        # The native session owns the negotiated key.
        pass

    @property
    def latency(self) -> float:
        """Latency between a HEARTBEAT and a HEARTBEAT_ACK in seconds."""
        return float(self._native.latency)

    @property
    def average_latency(self) -> float:
        """Average of the most recent 20 HEARTBEAT latencies in seconds."""
        return float(self._native.average_latency)

    # ------------------------------------------------------------------ reading

    async def poll_event(self) -> None:
        """|coro|

        Wait until the voice websocket ends, then raise :class:`~discord.ConnectionClosed`.

        The native reader handles HELLO/READY/SESSION_DESCRIPTION, heartbeats and the DAVE
        opcodes as they arrive, so the only event Python has to observe is the closure.
        """
        code, _reason = await self._native.wait_closed()
        self._close_code = code or None
        self.ws.close_code = self._close_code
        raise ConnectionClosed(self, shard_id=None, code=self._close_code)
