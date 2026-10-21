"""The voice websocket handle.

Rust owns the socket: the TCP/TLS/websocket handshake, the heartbeat, IP discovery, the
encryption negotiation, the DAVE/MLS session and every frame are handled natively. There is no
socket to drive from Python, so this object is only the two things the rest of the layer still
asks of it:

* :attr:`latency`, the heartbeat round trip discord.py exposes,
* :meth:`close`, which the reconnect loop calls.

discord.py's ``DiscordVoiceWebSocket`` is *not* inherited: it is a socket-level implementation
(``aiohttp`` frames, ``aiohttp`` heartbeat, ``from_connection_state``) that has nothing left to
do here.
"""

from __future__ import annotations

from . import _native

__all__ = ('DiscordVoiceWebSocket',)


class DiscordVoiceWebSocket:
    """The voice websocket handle, backed by the native transport."""

    def __init__(self, connection: _native.VoiceConnection, *, resume: bool = False) -> None:
        self._native = connection
        #: Whether this connection's handshake sends RESUME rather than IDENTIFY.
        self.resume = resume

    def __repr__(self) -> str:
        return f'<{type(self).__name__} resume={self.resume}>'

    @property
    def latency(self) -> float:
        """:class:`float`: Latency between a HEARTBEAT and a HEARTBEAT_ACK in seconds."""
        return float(self._native.latency)

    async def close(self, code: int = 1000) -> None:
        """|coro| Close the voice websocket."""
        await self._native.close(code)
