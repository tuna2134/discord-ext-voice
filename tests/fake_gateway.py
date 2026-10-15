"""A minimal fake Discord voice gateway used by the integration tests.

It speaks enough of the voice protocol to exercise the native transport end to end:

* websocket handshake (plaintext ``ws://``, which the native transport supports explicitly),
* ``HELLO`` / ``IDENTIFY`` / ``READY`` / ``SELECT_PROTOCOL`` / ``SESSION_DESCRIPTION``,
* UDP IP discovery,
* heartbeat / heartbeat acknowledgement,
* reception of RTP packets, which the tests then decrypt with PyNaCl to prove wire
  compatibility with ``discord.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import socket
from typing import Any, Optional

import websockets
from websockets.asyncio.server import serve

from discord_ext_voice import _native

#: The SSRC the fake gateway hands out.
SSRC = 0x1234ABCD

#: The identifiers the fake gateway expects in an IDENTIFY payload.
GUILD_ID = 111
USER_ID = 222
CHANNEL_ID = 333


async def open_connection(gateway: 'FakeVoiceGateway', *, resume: bool = False):
    """Create a native ``VoiceConnection``, feed it the gateway credentials and handshake.

    This mirrors what the Python layer does: store the values from ``VOICE_STATE_UPDATE`` and
    ``VOICE_SERVER_UPDATE``, move the connection flow to ``got_both_voice_updates`` and then
    let the transport negotiate.
    """
    connection = _native.VoiceConnection(GUILD_ID, USER_ID, CHANNEL_ID)
    connection.set_session_id('session-abc')
    connection.set_server_info(GUILD_ID, 'voice-token', gateway.endpoint)
    # ConnectionFlowState.got_both_voice_updates (or got_voice_server_update when resuming).
    connection.set_phase(3 if resume else 4)
    await connection.handshake(resume=resume, timeout=10.0)
    return connection


def make_secret_key() -> list[int]:
    return list(secrets.token_bytes(32))


class FakeVoiceGateway:
    """A single-connection fake voice gateway."""

    def __init__(
        self,
        *,
        modes: Optional[list[str]] = None,
        secret_key: Optional[list[int]] = None,
        dave_protocol_version: int = 0,
        heartbeat_interval_ms: float = 400.0,
        address: str = '127.0.0.1',
    ) -> None:
        self.modes = list(modes or ('aead_xchacha20_poly1305_rtpsize', 'xsalsa20_poly1305'))
        self.secret_key = list(secret_key if secret_key is not None else make_secret_key())
        self.dave_protocol_version = dave_protocol_version
        self.heartbeat_interval_ms = heartbeat_interval_ms
        self.address = address

        self.received_json: list[dict[str, Any]] = []
        self.received_binary: list[bytes] = []
        self.rtp_packets: list[bytes] = []
        self.ip_discovery_requests = 0

        self._ws_task: Optional[asyncio.Task[None]] = None
        self._udp_task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._server: Any = None
        self._udp: Optional[socket.socket] = None
        self._udp_port: int = 0
        self._ws_port: int = 0
        self._connection: Any = None
        self._closed = asyncio.Event()
        self._seq = 0

        self._waiter_tasks: list[asyncio.Task[Any]] = []

    # ------------------------------------------------------------------ lifecycle

    @property
    def endpoint(self) -> str:
        return f'ws://{self.address}:{self._ws_port}'

    async def __aenter__(self) -> 'FakeVoiceGateway':
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.stop()

    async def start(self) -> 'FakeVoiceGateway':
        loop = asyncio.get_running_loop()

        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.bind((self.address, 0))
        self._udp.setblocking(False)
        self._udp_port = self._udp.getsockname()[1]
        self._udp_task = loop.create_task(self._udp_loop())

        ready = asyncio.Event()

        async def runner() -> None:
            async with serve(self._handler, self.address, 0, ping_interval=None) as server:
                self._server = server
                self._ws_port = server.sockets[0].getsockname()[1]
                ready.set()
                await self._stop.wait()

        self._ws_task = loop.create_task(runner())
        await ready.wait()
        return self

    async def stop(self) -> None:
        self._stop.set()
        if self._connection is not None:
            with contextlib.suppress(Exception):
                await self._connection.close()
        if self._ws_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._ws_task, timeout=5)
            self._ws_task = None
        if self._udp_task is not None:
            self._udp_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._udp_task
            self._udp_task = None
        if self._udp is not None:
            self._udp.close()
            self._udp = None

    # ------------------------------------------------------------------ helpers for tests

    async def wait_for_op(self, op: int, *, timeout: float = 10.0) -> dict[str, Any]:
        """Wait until a JSON frame with the given opcode has been received."""

        async def poll() -> dict[str, Any]:
            while True:
                for frame in self.received_json:
                    if frame.get('op') == op:
                        return frame
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(poll(), timeout=timeout)

    async def wait_for_rtp(self, *, count: int = 1, timeout: float = 10.0) -> list[bytes]:
        async def poll() -> list[bytes]:
            while len(self.rtp_packets) < count:
                await asyncio.sleep(0.01)
            return self.rtp_packets[:count]

        return await asyncio.wait_for(poll(), timeout=timeout)

    async def wait_for_binary_op(self, opcode: int, *, timeout: float = 10.0) -> bytes:
        async def poll() -> bytes:
            while True:
                for frame in self.received_binary:
                    if frame and frame[0] == opcode:
                        return frame
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(poll(), timeout=timeout)

    async def send_json(self, payload: dict[str, Any]) -> None:
        assert self._connection is not None, 'no client is connected'
        await self._send(payload)

    async def _send(self, payload: dict[str, Any]) -> None:
        """Send a JSON frame with a monotonic `seq`, like the real voice gateway."""
        assert self._connection is not None
        self._seq += 1
        payload = {'seq': self._seq, **payload}
        await self._connection.send(json.dumps(payload))

    async def close(self, code: int = 1000, reason: str = '') -> None:
        if self._connection is not None:
            await self._connection.close(code, reason)

    # ------------------------------------------------------------------ UDP

    async def _udp_loop(self) -> None:
        assert self._udp is not None
        loop = asyncio.get_running_loop()
        while True:
            try:
                data, peer = await loop.sock_recvfrom(self._udp, 4096)
            except asyncio.CancelledError:
                return
            except OSError:
                return

            # IP discovery: 74 bytes starting with 0x0001 0x0046.
            if len(data) >= 74 and data[0] == 0 and data[1] == 1:
                self.ip_discovery_requests += 1
                ssrc = int.from_bytes(data[4:8], 'big')
                response = bytearray(74)
                response[0:2] = (2).to_bytes(2, 'big')
                response[2:4] = (70).to_bytes(2, 'big')
                response[4:8] = ssrc.to_bytes(4, 'big')
                encoded = peer[0].encode('ascii')
                response[8 : 8 + len(encoded)] = encoded
                response[72:74] = peer[1].to_bytes(2, 'big')
                await loop.sock_sendto(self._udp, bytes(response), peer)
                continue

            self.rtp_packets.append(data)

    # ------------------------------------------------------------------ websocket

    async def _handler(self, connection: Any) -> None:
        self._connection = connection
        try:
            self._connection = connection
            await self._send(
                {'op': 8, 'd': {'heartbeat_interval': self.heartbeat_interval_ms}}
            )

            async for raw in connection:
                if isinstance(raw, bytes):
                    self.received_binary.append(raw)
                    continue

                frame = json.loads(raw)
                self.received_json.append(frame)
                op = frame.get('op')
                data = frame.get('d') or {}

                if op == 0:  # IDENTIFY
                    await self._send(
                        {
                            'op': 2,
                            'd': {
                                'ssrc': SSRC,
                                'ip': self.address,
                                'port': self._udp_port,
                                'modes': self.modes,
                            },
                        }
                    )
                elif op == 1:  # SELECT_PROTOCOL
                    await self._send(
                        {
                            'op': 4,
                            'd': {
                                'mode': data.get('data', {}).get('mode', self.modes[0]),
                                'secret_key': self.secret_key,
                                'dave_protocol_version': self.dave_protocol_version,
                            },
                        }
                    )
                elif op == 3:  # HEARTBEAT
                    # Discord's voice heartbeat payload is a plain integer nonce which the
                    # server echoes back; accept the legacy `{t, seq_ack}` shape too.
                    if isinstance(data, int):
                        nonce = data
                    elif isinstance(data, dict):
                        nonce = data.get('nonce', data.get('t', 0))
                    else:
                        nonce = 0
                    await self._send({'op': 6, 'd': nonce})
                elif op == 7:  # RESUME
                    await self._send({'op': 9, 'd': None})
                    await self._send(
                        {
                            'op': 4,
                            'd': {
                                'mode': self.modes[0],
                                'secret_key': self.secret_key,
                                'dave_protocol_version': self.dave_protocol_version,
                            },
                        }
                    )
        except Exception as exc:  # pragma: no cover - debugging aid
            import traceback

            self.handler_error = f'{exc!r}'
            traceback.print_exc()
        finally:
            self._closed.set()
