"""The voice websocket facade.

Rust owns the socket: the TCP/TLS/websocket handshake, the heartbeat, IP discovery, the
encryption negotiation, the DAVE/MLS session and every incoming frame are handled in the
native reader. This class is the thin Python object the rest of the layer talks to:

* the voice opcode constants,
* :meth:`speak`, which discord.py's player calls before it sends audio,
* :meth:`close` / :meth:`wait_closed`, which the reconnect loop drives,
* the negotiated session values, read straight from the native connection.

discord.py's ``DiscordVoiceWebSocket`` is *not* inherited. It is a socket-level implementation
(``aiohttp`` frames, ``aiohttp`` heartbeat, ``DiscordVoiceWebSocket.from_connection_state``)
that has nothing left to do here, so this class only keeps the surface that is still
meaningful.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Optional

from discord.errors import ConnectionClosed

from . import _native

if TYPE_CHECKING:
    from .voice_state import VoiceConnectionState

__all__ = ('DiscordVoiceWebSocket',)


class DiscordVoiceWebSocket:
    """The voice websocket surface, backed by the native transport."""

    #: Voice gateway opcodes, with the values Discord assigns them.
    #: https://docs.discord.com/developers/topics/opcodes-and-status-codes#voice-voice-opcodes
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

    def __init__(self, connection: _native.VoiceConnection, state: 'VoiceConnectionState') -> None:
        self._native = connection
        self._connection = state
        self._close_code: Optional[int] = None

    def __repr__(self) -> str:
        return f'<{type(self).__name__} close_code={self._close_code!r}>'

    # ------------------------------------------------------------------ native state

    @property
    def close_code(self) -> Optional[int]:
        """Optional[:class:`int`]: The close code, once the socket is gone."""
        reported = self._native.close_code
        return reported if reported is not None else self._close_code

    @property
    def seq_ack(self) -> int:
        """:class:`int`: The sequence number of the last numbered frame the reader saw."""
        return int(self._native.seq_ack)

    @seq_ack.setter
    def seq_ack(self, value: int) -> None:
        # The native reader tracks the sequence number.
        pass

    @property
    def secret_key(self) -> Optional[list[int]]:
        """Optional[List[:class:`int`]]: The RTP encryption key, once negotiated."""
        key = self._native.secret_key
        return list(key) if key else None

    @secret_key.setter
    def secret_key(self, value: Any) -> None:
        # The native session owns the negotiated key.
        pass

    @property
    def latency(self) -> float:
        """:class:`float`: Latency between a HEARTBEAT and a HEARTBEAT_ACK in seconds."""
        return float(self._native.latency)

    @property
    def average_latency(self) -> float:
        """:class:`float`: The average of the most recent 20 latencies in seconds."""
        return float(self._native.average_latency)

    # ------------------------------------------------------------------ writing

    async def send_as_json(self, data: Any) -> bool:
        """|coro| Send a JSON frame, returning whether the socket accepted it."""
        accepted = self._native.send_json_text(json.dumps(data))
        if not accepted:
            raise ConnectionClosed(self, shard_id=None, code=self.close_code)
        return accepted

    async def send_binary(self, opcode: int, data: bytes) -> bool:
        """|coro| Send a binary frame as ``[opcode][payload]``."""
        accepted = self._native.send_binary_op(opcode, data)
        if not accepted:
            raise ConnectionClosed(self, shard_id=None, code=self.close_code)
        return accepted

    #: discord.py's spelling for :meth:`send_as_json`.
    send_heartbeat = send_as_json

    async def speak(self, state: Any = None) -> None:
        """|coro| Announce our SSRC and speaking state.

        discord.py's player calls this with a :class:`discord.enums.SpeakingState` before it
        sends audio; Discord drops audio from an SSRC it has not been told about.
        """
        if state is None:
            from discord.enums import SpeakingState

            state = SpeakingState.voice

        await self.send_as_json(
            {
                'op': self.SPEAKING,
                'd': {
                    'speaking': int(state),
                    'delay': 0,
                    'ssrc': self._native.ssrc,
                },
            }
        )

    async def close(self, code: int = 1000) -> None:
        """|coro| Close the voice websocket."""
        self._close_code = code
        await self._native.close(code)

    # ------------------------------------------------------------------ reading

    async def wait_closed(self) -> tuple[int, str]:
        """|coro| Wait until the socket ends and return ``(code, reason)``."""
        code, reason = await self._native.wait_closed()
        if code:
            self._close_code = code
        return int(code or 0), str(reason)

    async def poll_event(self) -> None:
        """|coro| Wait for the socket to end, then raise :class:`discord.ConnectionClosed`.

        discord.py's poller spells the wait this way; :meth:`wait_closed` returns the code
        instead of raising, which is what the reconnect policy needs.
        """
        code, reason = await self.wait_closed()
        raise ConnectionClosed(self, shard_id=None, code=code or None)
