"""The voice connection flow.

This is this package's own state machine, implemented on top of the native transport: it joins
the channel over the main gateway, drives the native handshake, keeps the session alive through
the reconnect policy and tears it down again. discord.py's ``VoiceConnectionState`` is *not*
inherited — discord.py provides the host framework (the ``VoiceProtocol`` extension point, the
gateway connection that delivers ``VOICE_STATE_UPDATE``/``VOICE_SERVER_UPDATE``, and the player
thread), not the voice implementation.

The flow is what Discord expects, which is what discord.py's state machine encodes:

* op 4 over the main gateway joins the channel,
* ``VOICE_SERVER_UPDATE`` and ``VOICE_STATE_UPDATE`` both have to arrive before the voice
  websocket opens,
* the close code decides what happens next: 1000 disconnects, 4014/4022 may mean we were moved,
  4015 resumes, 4021 is a rate limit to give up on, and anything else is retried with
  exponential backoff.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from enum import Enum
from threading import Event as ThreadEvent
from typing import Any, Callable, Optional, Tuple

from discord.errors import ConnectionClosed
from discord.utils import MISSING

from . import _native
from .gateway import DiscordVoiceWebSocket

__all__ = ('VoiceConnectionState', 'ConnectionFlowState', 'DAVE_PROTOCOL_VERSION')

_log = logging.getLogger(__name__)

#: The highest DAVE protocol version this build advertises and negotiates.
DAVE_PROTOCOL_VERSION: int = int(_native.DAVE_PROTOCOL_VERSION)


class ConnectionFlowState(Enum):
    """Where the connection flow is.

    The members and their values are discord.py's, so the numbers mean the same thing to
    anything that already speaks them. The native transport keeps its own phase in step with
    this enum, which is how it knows when audio may be sent.
    """

    # fmt: off
    disconnected            = 0
    set_guild_voice_state   = 1
    got_voice_state_update  = 2
    got_voice_server_update = 3
    got_both_voice_updates  = 4
    websocket_connected     = 5
    got_websocket_ready     = 6
    got_ip_discovery        = 7
    connected               = 8
    # fmt: on


class _Backoff:
    """The reconnect delay sequence discord.py's poller uses.

    A random delay in ``[0, base * 2**n)``, with ``n`` capped at 10 and reset when the previous
    invocation is more than ``base * 2**11`` seconds old.
    """

    def __init__(self, base: float = 1.0) -> None:
        self.base = base
        self._exponent = 0
        self._last_invocation = 0.0

    def reset(self) -> None:
        self._exponent = 0
        self._last_invocation = 0.0

    def delay(self) -> float:
        now = time.monotonic()
        if now - self._last_invocation > self.base * 2**11:
            self._exponent = 0
        self._last_invocation = now
        self._exponent = min(self._exponent + 1, 10)
        return random.uniform(0, self.base * 2**self._exponent)


class VoiceConnectionState:
    """The connection flow of one :class:`VoiceClient`, on top of the native transport."""

    def __init__(self, voice_client: Any, *, hook: Optional[Callable[..., Any]] = None) -> None:
        self.voice_client = voice_client
        self.hook = hook

        self.timeout: float = 30.0
        self.reconnect: bool = True
        self.self_deaf: bool = False
        self.self_mute: bool = False

        # Credentials from the main gateway.
        self.server_id: Optional[int] = None
        self.session_id: Optional[str] = None
        self.token: Optional[str] = None
        self.endpoint: Optional[str] = None

        # The session the native transport negotiated.
        self.endpoint_ip: Optional[str] = None
        self.ip: Optional[str] = None
        self.port: Optional[int] = None
        self.voice_port: Optional[int] = None
        self.ssrc: Any = MISSING
        self.mode: Optional[str] = None
        self.secret_key: Any = MISSING

        # Rust owns the UDP socket, so discord.py's socket slot stays a sentinel.
        self.socket: Any = MISSING
        self.ws: Any = MISSING

        self._state = ConnectionFlowState.disconnected
        self._expecting_disconnect = False
        #: Whether the gateway has been asked to put us in a channel and has not been asked to
        #: take us out again. The flow state alone cannot answer that: while the poller is
        #: reconnecting, the state is not `connected` but we are still in the channel.
        self._joined = False
        #: Whether the next websocket connection must send RESUME instead of IDENTIFY.
        self._resume = False
        self._connected = ThreadEvent()
        self._disconnected = asyncio.Event()
        self._state_event = asyncio.Event()
        self._runner: Optional[asyncio.Task] = None
        self._connector: Optional[asyncio.Task] = None

        channel = voice_client.channel
        self._native = _native.VoiceConnection(
            int(getattr(channel.guild, 'id', 0) or 0),
            int(getattr(voice_client.user, 'id', 0) or 0),
            int(channel.id),
        )

    def __repr__(self) -> str:
        return f'<{type(self).__name__} state={self._state.name} connected={self.is_connected()}>'

    # ------------------------------------------------------------------ native state

    @property
    def native(self) -> _native.VoiceConnection:
        """The native connection implementing this state: websocket, UDP, RTP and DAVE."""
        return self._native

    @property
    def state(self) -> ConnectionFlowState:
        """Where the connection flow is."""
        return self._state

    @state.setter
    def state(self, state: ConnectionFlowState) -> None:
        if state is not self._state:
            _log.debug('Connection state changed to %s', state.name)

        self._state = state
        # Wake every waiter; each re-checks its condition, so nothing is lost to the clear.
        self._state_event.set()
        self._state_event.clear()

        if state is ConnectionFlowState.connected:
            self._connected.set()
        else:
            self._connected.clear()

        # Keep the transport in step: it only accepts audio once the flow is connected.
        self._native.set_phase(int(state.value))

    @property
    def guild(self) -> Any:
        """:class:`discord.Guild`: The guild this state belongs to."""
        return self.voice_client.guild

    @property
    def user(self) -> Any:
        """:class:`discord.ClientUser`: The user connected to voice (i.e. ourselves)."""
        return self.voice_client.user

    @property
    def supported_modes(self) -> Tuple[str, ...]:
        """The RTP encryption modes this backend supports."""
        return self.voice_client.supported_modes

    @property
    def self_voice_state(self) -> Any:
        """Optional[:class:`discord.VoiceState`]: Our own voice state, if discord.py knows it."""
        return self.guild.me.voice

    @property
    def max_dave_protocol_version(self) -> int:
        """:class:`int`: The DAVE protocol version this build advertises."""
        return DAVE_PROTOCOL_VERSION

    @property
    def dave_protocol_version(self) -> int:
        """:class:`int`: The DAVE protocol version in use (``0`` when E2EE is off)."""
        return int(self._native.dave_protocol_version)

    @dave_protocol_version.setter
    def dave_protocol_version(self, value: int) -> None:
        # The native session owns the negotiation, so discord.py's bookkeeping writes are
        # ignored; the getter always reports the live value.
        pass

    @property
    def can_encrypt(self) -> bool:
        """:class:`bool`: Whether the DAVE session is ready to encrypt frames."""
        return bool(self._native.can_encrypt())

    @property
    def dave_session(self) -> Optional[_native.VoiceConnection]:
        """Optional[:class:`_native.VoiceConnection`]: The native DAVE session handle."""
        return self._native if self._native.has_dave_session() else None

    @dave_session.setter
    def dave_session(self, value: Any) -> None:
        # See `dave_protocol_version`: the native session is the source of truth.
        pass

    @property
    def voice_privacy_code(self) -> Optional[str]:
        """Optional[:class:`str`]: The displayable E2EE privacy code, when DAVE is active."""
        return self._native.voice_privacy_code

    @property
    def latency(self) -> float:
        """:class:`float`: Latency between a HEARTBEAT and a HEARTBEAT_ACK in seconds."""
        return float(self._native.latency)

    @property
    def average_latency(self) -> float:
        """:class:`float`: The average of the most recent 20 latencies in seconds."""
        return float(self._native.average_latency)

    # ------------------------------------------------------------------ DAVE

    async def reinit_dave_session(self) -> None:
        """|coro| (Re)create the MLS session and send a fresh key package.

        The native reader creates the session and answers protocol transitions as the opcodes
        arrive; this is the manual equivalent, for a caller that wants to start over.
        """
        self._native.reinit_dave_session()

    # ------------------------------------------------------------------ gateway events

    async def voice_state_update(self, data: dict) -> None:
        """|coro| Handle a ``VOICE_STATE_UPDATE`` for our own user."""
        channel_id = data.get('channel_id')

        if channel_id is None:
            self._joined = False
            self._disconnected.set()

            # A `channel_id` of None is expected while disconnecting; anything else means
            # Discord moved or removed us, so the client has to follow.
            if self._expecting_disconnect:
                self._expecting_disconnect = False
            else:
                _log.debug('We were externally disconnected from voice')
                await self.disconnect()
            return

        channel_id = int(channel_id)
        self._joined = True
        self.session_id = data.get('session_id')
        self._native.set_session_id(self.session_id)

        if self.state in (
            ConnectionFlowState.set_guild_voice_state,
            ConnectionFlowState.got_voice_server_update,
        ):
            if self.state is ConnectionFlowState.set_guild_voice_state:
                self.state = ConnectionFlowState.got_voice_state_update

                # We moved ourselves while connecting.
                if channel_id != self.voice_client.channel.id:
                    self._update_voice_channel(channel_id)
            else:
                self.state = ConnectionFlowState.got_both_voice_updates
            return

        if self.state is ConnectionFlowState.connected:
            self._update_voice_channel(channel_id)
        elif self.state is not ConnectionFlowState.disconnected:
            if channel_id != self.voice_client.channel.id:
                # We were moved mid-handshake: run the flow again on the new channel.
                _log.info('Handling channel move while connecting...')
                self._update_voice_channel(channel_id)
                await self.soft_disconnect(with_state=ConnectionFlowState.got_voice_state_update)
                await self.connect(
                    reconnect=self.reconnect,
                    timeout=self.timeout,
                    self_deaf=self.self_deaf,
                    self_mute=self.self_mute,
                    resume=False,
                    wait=False,
                )
            else:
                _log.debug('Ignoring unexpected voice_state_update event')

    async def voice_server_update(self, data: dict) -> None:
        """|coro| Handle a ``VOICE_SERVER_UPDATE``: the voice server endpoint and token."""
        previous_token = self.token
        previous_server_id = self.server_id
        previous_endpoint = self.endpoint

        token = data.get('token')
        endpoint = data.get('endpoint')
        self.token = token
        self.server_id = int(data['guild_id']) if data.get('guild_id') is not None else None

        # Feed the native transport *before* the flow below runs: that can schedule a
        # reconnect, which would otherwise race with this update.
        if token is not None and endpoint is not None and self.server_id is not None:
            self._native.set_server_info(self.server_id, token, endpoint)

        if self.token is None or endpoint is None:
            _log.warning(
                'Awaiting endpoint... This requires waiting. If timeout occurred considering '
                'raising the timeout and reconnecting.'
            )
            return

        self.endpoint = endpoint[6:] if endpoint.startswith('wss://') else endpoint

        # We got the event while connecting.
        if self.state in (
            ConnectionFlowState.set_guild_voice_state,
            ConnectionFlowState.got_voice_state_update,
        ):
            # This gets set after READY is received.
            self.endpoint_ip = MISSING

            if self.state is ConnectionFlowState.set_guild_voice_state:
                self.state = ConnectionFlowState.got_voice_server_update
            else:
                self.state = ConnectionFlowState.got_both_voice_updates

        elif self.state is ConnectionFlowState.connected:
            _log.debug('Voice server update, closing old voice websocket')
            await self.ws.close(4014)
            self.state = ConnectionFlowState.got_voice_server_update

        elif self.state is not ConnectionFlowState.disconnected:
            if (
                previous_token == self.token
                and previous_server_id == self.server_id
                and previous_endpoint == self.endpoint
            ):
                return

            _log.debug('Unexpected server update event, attempting to handle')
            await self.soft_disconnect(with_state=ConnectionFlowState.got_voice_server_update)
            await self.connect(
                reconnect=self.reconnect,
                timeout=self.timeout,
                self_deaf=self.self_deaf,
                self_mute=self.self_mute,
                resume=False,
                wait=False,
            )

    # ------------------------------------------------------------------ connecting

    async def connect(
        self,
        *,
        reconnect: bool,
        timeout: float,
        self_deaf: bool = False,
        self_mute: bool = False,
        resume: bool = False,
        wait: bool = True,
    ) -> None:
        """|coro| Run the connection flow and, unless ``wait`` is false, wait for it to end."""
        if self._connector:
            self._connector.cancel()
            self._connector = None

        if self._runner:
            self._runner.cancel()
            self._runner = None

        self.timeout = timeout
        self.reconnect = reconnect
        self.self_deaf = self_deaf
        self.self_mute = self_mute
        self._connector = asyncio.get_running_loop().create_task(
            self._wrap_connect(reconnect, timeout, self_deaf, self_mute, resume),
            name='discord-ext-voice connector',
        )
        if wait:
            await self._connector

    async def _wrap_connect(self, *args: Any) -> None:
        try:
            await self._connect(*args)
        except asyncio.CancelledError:
            _log.debug('Cancelling voice connection')
            await self.soft_disconnect()
            raise
        except asyncio.TimeoutError:
            _log.info('Timed out connecting to voice')
            await self.disconnect()
            raise
        except Exception:
            _log.exception('Error connecting to voice... disconnecting')
            await self.disconnect()
            raise

    async def _inner_connect(
        self, reconnect: bool, self_deaf: bool, self_mute: bool, resume: bool
    ) -> None:
        for i in range(5):
            _log.info('Starting voice handshake... (connection attempt %d)', i + 1)

            await self._voice_connect(self_deaf=self_deaf, self_mute=self_mute)
            self._joined = True
            # Setting this unnecessarily will break reconnecting.
            if self.state is ConnectionFlowState.disconnected:
                self.state = ConnectionFlowState.set_guild_voice_state

            await self._wait_for_state(ConnectionFlowState.got_both_voice_updates)

            _log.info('Voice handshake complete. Endpoint found: %s', self.endpoint)

            try:
                self.ws = await self._connect_websocket(resume)
                await self._handshake_websocket()
                break
            except ConnectionClosed:
                if reconnect:
                    wait = 1 + i * 2.0
                    _log.exception('Failed to connect to voice... Retrying in %ss...', wait)
                    await self.disconnect(cleanup=False)
                    await asyncio.sleep(wait)
                    continue
                await self.disconnect()
                raise

    async def _connect(
        self, reconnect: bool, timeout: float, self_deaf: bool, self_mute: bool, resume: bool
    ) -> None:
        _log.info('Connecting to voice...')

        await asyncio.wait_for(
            self._inner_connect(
                reconnect=reconnect, self_deaf=self_deaf, self_mute=self_mute, resume=resume
            ),
            timeout=timeout,
        )
        _log.info('Voice connection complete.')

        if not self._runner:
            self._runner = asyncio.get_running_loop().create_task(
                self._poll_voice_ws(reconnect), name='discord-ext-voice websocket poller'
            )

    async def disconnect(
        self, *, force: bool = True, cleanup: bool = True, wait: bool = False
    ) -> None:
        """|coro| Leave the channel and tear the native session down."""
        if not force and not self.is_connected() and not self._joined:
            return

        requested = False
        try:
            requested = await self._voice_disconnect()
            if self.ws is not MISSING and self.ws is not None:
                await self.ws.close()
        except Exception:
            _log.warning('Ignoring exception disconnecting from voice', exc_info=True)
        finally:
            self.state = ConnectionFlowState.disconnected

            if cleanup:
                self.voice_client.stop()

            # Flip the connected event to unlock any waiters.
            self._connected.set()
            self._connected.clear()

            self.ip = None
            self.port = None

            # Skip this part if disconnect was called from the poll loop task, or if the
            # gateway was never told to remove us (there would be nothing to confirm).
            if wait and requested and not self._inside_runner():
                try:
                    await asyncio.wait_for(self._disconnected.wait(), timeout=self.timeout)
                except asyncio.TimeoutError:
                    _log.warning(
                        'Timed out after %ss waiting for the voice disconnection confirmation; '
                        'carrying on',
                        self.timeout,
                    )
                except asyncio.CancelledError:
                    pass

            if cleanup:
                self.voice_client.cleanup()

    async def soft_disconnect(
        self, *, with_state: ConnectionFlowState = ConnectionFlowState.got_both_voice_updates
    ) -> None:
        """|coro| Drop the socket but stay in the flow, ready to connect again."""
        _log.debug('Soft disconnecting from voice')

        # Stop the poller, because closing the socket would otherwise trigger a reconnect.
        if self._runner:
            self._runner.cancel()
            self._runner = None

        try:
            if self.ws is not MISSING and self.ws is not None:
                await self.ws.close()
        except Exception:
            _log.debug('Ignoring exception soft disconnecting from voice', exc_info=True)
        finally:
            self.state = with_state
            self.ip = None
            self.port = None

    async def move_to(self, channel: Optional[Any], timeout: Optional[float]) -> None:
        """|coro| Move to another voice channel, or leave voice when ``channel`` is ``None``."""
        if channel is None:
            # This is only called externally, so waiting for the disconnect is fine.
            await self.disconnect(wait=True)
            return

        if self.voice_client.channel and channel.id == self.voice_client.channel.id:
            return

        previous_state = self.state

        # This is only an outgoing gateway request: if it fails, nothing changes but our state.
        await self._move_to(channel)

        last_state = self.state
        try:
            await self.wait_async(timeout)
        except asyncio.TimeoutError:
            _log.warning(
                'Timed out trying to move to channel %s in guild %s', channel.id, self.guild.id
            )
            if self.state is last_state:
                _log.debug('Reverting to previous state %s', previous_state.name)
                self.state = previous_state

    async def _move_to(self, channel: Any) -> None:
        await self.voice_client.channel.guild.change_voice_state(channel=channel)
        self.state = ConnectionFlowState.set_guild_voice_state

    def _update_voice_channel(self, channel_id: Optional[int]) -> None:
        self.voice_client.channel = channel_id and self.guild.get_channel(channel_id)
        if channel_id:
            # Keep the native session's channel binding (used by DAVE) in step.
            self._native.channel_id = int(channel_id)

    # ------------------------------------------------------------------ waiting

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until the connection is established (or ``timeout`` expires)."""
        return self._connected.wait(timeout)

    async def wait_async(self, timeout: Optional[float] = None) -> None:
        """|coro| Wait until the connection is established."""
        await self._wait_for_state(ConnectionFlowState.connected, timeout=timeout)

    def is_connected(self) -> bool:
        """Whether the flow reached :attr:`ConnectionFlowState.connected`."""
        return self._state is ConnectionFlowState.connected

    async def _wait_for_state(
        self,
        state: ConnectionFlowState,
        *other_states: ConnectionFlowState,
        timeout: Optional[float] = None,
    ) -> None:
        states = (state, *other_states)

        async def waiter() -> None:
            while self.state not in states:
                await self._state_event.wait()

        await asyncio.wait_for(waiter(), timeout=timeout)

    def _inside_runner(self) -> bool:
        return self._runner is not None and asyncio.current_task() is self._runner

    # ------------------------------------------------------------------ transport wiring

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
            # A connect timeout is an `asyncio.TimeoutError` for discord.py (and retried).
            raise asyncio.TimeoutError(str(exc)) from exc
        except _native.ConnectionClosedError as exc:
            # Discord puts the interesting part in the close code: 4006 and 4014/4015 mean the
            # session is gone and the flow should retry, 4021 means it was rate limited.
            code = self._native.close_code
            _log.warning(
                'The voice websocket closed during the handshake (code=%s, reason=%s)',
                code if code is not None else '<none>',
                self._native.close_reason or '<none>',
            )
            raise ConnectionClosed(self.ws, shard_id=None, code=code or None) from exc

        self.endpoint_ip = self._native.ip
        self.ssrc = self._native.ssrc
        self.ip = self._native.ip
        self.port = self._native.port
        self.voice_port = self._native.voice_port
        self.mode = self._native.mode
        self.secret_key = self._native.secret_key
        self.state = ConnectionFlowState.got_ip_discovery
        self.state = ConnectionFlowState.connected

    async def _voice_connect(self, *, self_deaf: bool = False, self_mute: bool = False) -> None:
        channel = self.voice_client.channel
        await channel.guild.change_voice_state(
            channel=channel, self_deaf=self_deaf, self_mute=self_mute
        )

    async def _voice_disconnect(self) -> bool:
        """Ask the main gateway to remove us. Returns whether the request went out."""
        channel = self.voice_client.channel
        _log.info(
            'The voice handshake is being terminated for Channel ID %s (Guild ID %s)',
            getattr(channel, 'id', None),
            getattr(self.guild, 'id', None),
        )
        self.state = ConnectionFlowState.disconnected
        try:
            await channel.guild.change_voice_state(channel=None)
        except Exception as exc:
            # A gateway that is reconnecting cannot take the request; there is nothing to wait
            # for, so let the caller carry on instead of blocking for the whole timeout.
            _log.warning('Could not tell the gateway to leave voice: %r', exc)
            return False
        self._joined = False
        self._expecting_disconnect = True
        self._disconnected.clear()
        return True

    def send_packet(self, packet: bytes) -> None:
        """Send a raw UDP packet. Mostly useful for tests and future receive-side work."""
        self._native.send_packet(packet)

    def add_socket_listener(self, callback: Callable[[bytes], Any]) -> None:
        """Not supported: this library implements the sending side of voice only."""
        raise NotImplementedError('receiving voice packets is not implemented')

    def remove_socket_listener(self, callback: Callable[[bytes], Any]) -> None:
        """Not supported: this library implements the sending side of voice only."""
        raise NotImplementedError('receiving voice packets is not implemented')

    # ------------------------------------------------------------------ reconnecting

    async def _poll_voice_ws(self, reconnect: bool) -> None:
        """Watch the socket and apply the close-code policy once it ends."""
        backoff = _Backoff()
        while True:
            try:
                code, reason = await self.ws.wait_closed()
            except asyncio.CancelledError:
                return

            # The close codes Discord documents for the voice gateway:
            #   1000 - normal closure
            #   4014 - we were externally disconnected (kicked, channel deleted, moved)
            #   4015 - the voice server crashed, so resume
            #   4021 - rate limited, do not reconnect
            #   4022 - the call was terminated, similar to 4014
            if code == 1000:
                # Don't disconnect a second time when the socket closed because we closed it.
                if not self._expecting_disconnect:
                    _log.info('Disconnecting from voice normally, close code %d.', code)
                    await self.disconnect()
                break

            if code in (4014, 4022):
                # This is a race between the main gateway event and the socket closing.
                if self._disconnected.is_set():
                    _log.info('Disconnected from voice by discord, close code %d.', code)
                    await self.disconnect()
                    break

                # We may have been moved to a different channel.
                _log.info('Disconnected from voice by force... potentially reconnecting.')
                if not await self._potential_reconnect():
                    _log.info('Reconnect was unsuccessful, disconnecting from voice normally...')
                    if self.state is not ConnectionFlowState.disconnected:
                        await self.disconnect()
                    break
                continue

            if code == 4021:
                _log.warning(
                    'We are being ratelimited while trying to connect to voice. Disconnecting...'
                )
                if self.state is not ConnectionFlowState.disconnected:
                    await self.disconnect()
                break

            if code == 4015:
                _log.info('Disconnected from voice, attempting a resume...')
                try:
                    await self._connect(
                        reconnect=reconnect,
                        timeout=self.timeout,
                        self_deaf=self.self_deaf,
                        self_mute=self.self_mute,
                        resume=True,
                    )
                except asyncio.TimeoutError:
                    _log.info('Could not resume the voice connection... Disconnecting...')
                    if self.state is not ConnectionFlowState.disconnected:
                        await self.disconnect()
                    break
                _log.info('Successfully resumed voice connection')
                continue

            _log.debug('Not handling close code %s (%s)', code, reason or 'no reason')

            if not reconnect:
                await self.disconnect()
                raise ConnectionClosed(self.ws, shard_id=None, code=code)

            retry = backoff.delay()
            _log.warning('Disconnected from voice... Reconnecting in %.2fs.', retry)
            await asyncio.sleep(retry)
            await self.disconnect(cleanup=False)

            try:
                await self._connect(
                    reconnect=reconnect,
                    timeout=self.timeout,
                    self_deaf=self.self_deaf,
                    self_mute=self.self_mute,
                    resume=False,
                )
            except asyncio.TimeoutError:
                # At this point the inner flow has retried 5 times, so go around again.
                _log.warning('Could not connect to voice... Retrying...')
                continue

    async def _potential_reconnect(self) -> bool:
        """Try to connect to the channel again after Discord moved us."""
        try:
            await self._wait_for_state(
                ConnectionFlowState.got_voice_server_update,
                ConnectionFlowState.got_both_voice_updates,
                ConnectionFlowState.disconnected,
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            return False

        if self.state is ConnectionFlowState.disconnected:
            return False

        previous_ws = self.ws
        try:
            self.ws = await self._connect_websocket(False)
            await self._handshake_websocket()
        except (ConnectionClosed, asyncio.TimeoutError):
            return False
        finally:
            try:
                await previous_ws.close()
            except Exception:
                pass
        return True
