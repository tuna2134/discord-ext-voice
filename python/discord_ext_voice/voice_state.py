"""The voice connection state machine, mirroring :mod:`discord.voice_state`.

Everything that touches the network — the websocket, the UDP socket, IP discovery, the
heartbeat, the RTP encryption and the DAVE/MLS session — lives in Rust. This module keeps the
parts that need Discord API access or asyncio scheduling:

* driving ``Guild.change_voice_state`` to move the bot in and out of channels,
* deciding how to react to each voice websocket close code,
* waiting on the connection flow states with timeouts.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional

from . import _native
from ._compat import strip_scheme
from .backoff import ExponentialBackoff
from .errors import ClientException, ConnectionClosedError
from .gateway import DiscordVoiceWebSocket

if TYPE_CHECKING:
    from .voice_client import VoiceClient

__all__ = ('VoiceConnectionState', 'ConnectionFlowState')

_log = logging.getLogger(__name__)

#: The DAVE protocol version this build supports.
DAVE_PROTOCOL_VERSION: int = int(_native.DAVE_PROTOCOL_VERSION)


class ConnectionFlowState(IntEnum):
    """Represents the voice connection flow state."""

    disconnected = 0
    set_guild_voice_state = 1
    got_voice_state_update = 2
    got_voice_server_update = 3
    got_both_voice_updates = 4
    websocket_connected = 5
    got_websocket_ready = 6
    got_ip_discovery = 7
    connected = 8


class VoiceConnectionState:
    """Represents the internal state of a voice connection."""

    def __init__(
        self,
        voice_client: 'VoiceClient',
        *,
        hook: Optional[Callable[[DiscordVoiceWebSocket, dict], Coroutine[Any, Any, Any]]] = None,
    ) -> None:
        self.voice_client = voice_client
        self.hook = hook

        self.timeout: float = 30.0
        self.reconnect: bool = True
        self.self_deaf: bool = False
        self.self_mute: bool = False
        self.token: Optional[str] = None
        self.session_id: Optional[str] = None
        self.endpoint: Optional[str] = None
        self.endpoint_ip: Optional[str] = None
        self.server_id: Optional[int] = None
        self.ip: Optional[str] = None
        self.port: Optional[int] = None
        self.voice_port: Optional[int] = None

        channel = voice_client.channel
        user = voice_client.user
        self._native = _native.VoiceConnection(
            int(getattr(channel.guild, 'id', 0) or 0),
            int(getattr(user, 'id', 0) or 0),
            int(channel.id),
        )
        self.ws = DiscordVoiceWebSocket(self._native, self)

        self._state: ConnectionFlowState = ConnectionFlowState.disconnected
        self._expecting_disconnect: bool = False
        self._connected = threading.Event()
        self._state_event = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._runner: Optional[asyncio.Task[None]] = None
        self._connector: Optional[asyncio.Task[None]] = None
        self._closed_task: Optional[asyncio.Task[None]] = None

    # ------------------------------------------------------------------ convenience properties

    @property
    def state(self) -> ConnectionFlowState:
        return self._state

    @state.setter
    def state(self, state: ConnectionFlowState) -> None:
        if state is not self._state:
            _log.debug('Connection state changed to %s', state.name)
        self._state = state
        self._native.set_phase(int(state))
        self._state_event.set()
        self._state_event.clear()

        if state is ConnectionFlowState.connected:
            self._connected.set()
        else:
            self._connected.clear()

    @property
    def guild(self) -> Any:
        return self.voice_client.guild

    @property
    def user(self) -> Any:
        return self.voice_client.user

    @property
    def supported_modes(self) -> tuple[str, ...]:
        return self.voice_client.supported_modes

    @property
    def self_voice_state(self) -> Any:
        return self.guild.me.voice

    @property
    def max_dave_protocol_version(self) -> int:
        return DAVE_PROTOCOL_VERSION

    @property
    def dave_session(self) -> Any:
        """The native DAVE session handle, or ``None`` when E2EE is inactive."""
        return self._native if self._native.has_dave_session() else None

    @property
    def dave_protocol_version(self) -> int:
        return int(self._native.dave_protocol_version)

    @property
    def can_encrypt(self) -> bool:
        return bool(self._native.can_encrypt())

    @property
    def secret_key(self) -> Optional[list[int]]:
        key = self._native.secret_key
        return list(key) if key else None

    @property
    def ssrc(self) -> int:
        return int(self._native.ssrc)

    @property
    def mode(self) -> Optional[str]:
        return self._native.mode

    @property
    def socket(self) -> Any:
        """The native UDP socket handle.

        ``discord.py`` exposes a :class:`socket.socket` here; receiving audio is not
        implemented by this library, so the native object is returned instead.
        """
        return self._native

    def is_connected(self) -> bool:
        return self.state is ConnectionFlowState.connected

    def send_packet(self, packet: bytes) -> None:
        self._native.send_packet(packet)

    def add_socket_listener(self, callback: Callable[[bytes], Any]) -> None:
        """Not supported: this library implements the sending side of voice only."""
        raise NotImplementedError('receiving voice packets is not implemented')

    def remove_socket_listener(self, callback: Callable[[bytes], Any]) -> None:
        """Not supported: this library implements the sending side of voice only."""
        raise NotImplementedError('receiving voice packets is not implemented')

    # ------------------------------------------------------------------ DAVE helpers

    async def reinit_dave_session(self) -> None:
        self._native.reinit_dave_session()

    async def _recover_from_invalid_commit(self, transition_id: int) -> None:
        # The native session handles this inline; kept for API compatibility.
        self._native.reinit_dave_session()

    async def _execute_transition(self, transition_id: int) -> None:
        # Transitions are executed inside the native reader task.
        return None

    # ------------------------------------------------------------------ gateway event entry points

    async def voice_state_update(self, data: dict) -> None:
        channel_id = data['channel_id']

        if channel_id is None:
            self._disconnected.set()
            if self._expecting_disconnect:
                self._expecting_disconnect = False
            else:
                _log.debug('We were externally disconnected from voice.')
                await self.disconnect()
            return

        channel_id = int(channel_id)
        self.session_id = data['session_id']
        self._native.set_session_id(self.session_id)

        if self.state in (
            ConnectionFlowState.set_guild_voice_state,
            ConnectionFlowState.got_voice_server_update,
        ):
            if self.state is ConnectionFlowState.set_guild_voice_state:
                self.state = ConnectionFlowState.got_voice_state_update
                if channel_id != self.voice_client.channel.id:
                    self._update_voice_channel(channel_id)
            else:
                self.state = ConnectionFlowState.got_both_voice_updates
            return

        if self.state is ConnectionFlowState.connected:
            self._update_voice_channel(channel_id)
        elif self.state is not ConnectionFlowState.disconnected:
            if channel_id != self.voice_client.channel.id:
                _log.info('Handling channel move while connecting...')
                self._update_voice_channel(channel_id)
                await self.soft_disconnect()
                await self.connect(
                    reconnect=self.reconnect,
                    timeout=self.timeout,
                    self_deaf=self.self_deaf,
                    self_mute=self.self_mute,
                    wait=False,
                )
            else:
                _log.debug('Ignoring unexpected voice_state_update event')

    async def voice_server_update(self, data: dict) -> None:
        previous_token = self.token
        previous_server_id = self.server_id
        previous_endpoint = self.endpoint

        self.token = data['token']
        self.server_id = int(data['guild_id'])
        endpoint = data.get('endpoint')

        if self.token is None or endpoint is None:
            _log.warning(
                'Awaiting endpoint... This requires waiting. If timeout occurred consider '
                'raising the timeout and reconnecting.'
            )
            return

        # `discord.py` exposes the bare host:port. The native transport receives the original
        # string, because a `ws://` prefix there means "use a plaintext websocket".
        self.endpoint = strip_scheme(endpoint)
        self._native.set_server_info(self.server_id, self.token, endpoint)

        if self.state in (
            ConnectionFlowState.set_guild_voice_state,
            ConnectionFlowState.got_voice_state_update,
        ):
            self.endpoint_ip = None
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
            await self.soft_disconnect()
            await self.connect(
                reconnect=self.reconnect,
                timeout=self.timeout,
                self_deaf=self.self_deaf,
                self_mute=self.self_mute,
                wait=False,
            )

    # ------------------------------------------------------------------ connecting

    async def connect(
        self,
        *,
        reconnect: bool,
        timeout: float,
        self_deaf: bool,
        self_mute: bool,
        wait: bool = True,
    ) -> None:
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
        self._connector = self.voice_client.loop.create_task(
            self._wrap_connect(reconnect, timeout, self_deaf, self_mute), name='Voice connector'
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

    async def _inner_connect(self, reconnect: bool, self_deaf: bool, self_mute: bool) -> None:
        for attempt in range(5):
            _log.info('Starting voice handshake... (connection attempt %d)', attempt + 1)

            await self._voice_connect(self_deaf=self_deaf, self_mute=self_mute)
            if self.state is ConnectionFlowState.disconnected:
                self.state = ConnectionFlowState.set_guild_voice_state

            try:
                await self._wait_for_state(ConnectionFlowState.got_both_voice_updates)
            except asyncio.TimeoutError:
                if not reconnect:
                    await self.disconnect()
                    raise
                wait = 1 + attempt * 2.0
                _log.exception('Failed to receive the voice credentials... Retrying in %ss...', wait)
                await self.disconnect(cleanup=False)
                await asyncio.sleep(wait)
                continue

            _log.info('Voice handshake complete. Endpoint found: %s', self.endpoint)

            try:
                await self._handshake_websocket()
                break
            except ConnectionClosedError:
                if reconnect:
                    wait = 1 + attempt * 2.0
                    _log.exception('Failed to connect to voice... Retrying in %ss...', wait)
                    await self.disconnect(cleanup=False)
                    await asyncio.sleep(wait)
                    continue
                await self.disconnect()
                raise

    async def _handshake_websocket(self, *, resume: bool = False) -> None:
        """Run the voice websocket handshake.

        The native transport performs the whole exchange (IDENTIFY/RESUME, UDP IP discovery,
        SELECT_PROTOCOL, SESSION_DESCRIPTION, DAVE setup); this method only mirrors the
        resulting connection flow states so that ``discord.py``-style state observations and
        the reconnect logic keep working.
        """
        self.state = ConnectionFlowState.websocket_connected
        await self._native.handshake(resume=resume, timeout=self.timeout)
        self.endpoint_ip = self._native.ip
        self.ip = self._native.ip
        self.port = self._native.port
        self.voice_port = self._native.voice_port
        self.state = ConnectionFlowState.got_ip_discovery
        self.state = ConnectionFlowState.connected

    async def _connect(self, reconnect: bool, timeout: float, self_deaf: bool, self_mute: bool) -> None:
        _log.info('Connecting to voice...')
        await asyncio.wait_for(
            self._inner_connect(reconnect=reconnect, self_deaf=self_deaf, self_mute=self_mute),
            timeout=timeout,
        )
        _log.info('Voice connection complete.')

        if not self._runner:
            self._runner = self.voice_client.loop.create_task(
                self._poll_voice_ws(reconnect), name='Voice websocket poller'
            )

    async def disconnect(self, *, force: bool = True, cleanup: bool = True, wait: bool = False) -> None:
        if not force and not self.is_connected():
            return

        try:
            await self._voice_disconnect()
            await self._native.close()
        except Exception:
            _log.debug('Ignoring exception disconnecting from voice', exc_info=True)
        finally:
            self.state = ConnectionFlowState.disconnected

            if cleanup:
                self.voice_client.stop()

            self._connected.set()
            self._connected.clear()

            self.ip = None
            self.port = None

            if wait and not self._inside_runner():
                try:
                    await asyncio.wait_for(self._disconnected.wait(), timeout=self.timeout)
                except (asyncio.TimeoutError, TimeoutError):
                    _log.debug('Timed out waiting for voice disconnection confirmation')
                except asyncio.CancelledError:
                    pass

            if cleanup:
                self.voice_client.cleanup()

    async def soft_disconnect(
        self, *, with_state: ConnectionFlowState = ConnectionFlowState.got_both_voice_updates
    ) -> None:
        _log.debug('Soft disconnecting from voice')
        if self._runner:
            self._runner.cancel()
            self._runner = None

        try:
            await self._native.close()
        except Exception:
            _log.debug('Ignoring exception soft disconnecting from voice', exc_info=True)
        finally:
            self.state = with_state
            self.ip = None
            self.port = None

    async def move_to(self, channel: Optional[Any], timeout: Optional[float]) -> None:
        if channel is None:
            await self.disconnect(wait=True)
            return

        if self.voice_client.channel and channel.id == self.voice_client.channel.id:
            return

        previous_state = self.state
        await self._move_to(channel)

        last_state = self.state
        try:
            await self.wait_async(timeout)
        except asyncio.TimeoutError:
            _log.warning('Timed out trying to move to channel %s in guild %s', channel.id, self.guild.id)
            if self.state is last_state:
                _log.debug('Reverting to previous state %s', previous_state.name)
                self.state = previous_state

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._connected.wait(timeout)

    async def wait_async(self, timeout: Optional[float] = None) -> None:
        await self._wait_for_state(ConnectionFlowState.connected, timeout=timeout)

    # ------------------------------------------------------------------ internals

    def _inside_runner(self) -> bool:
        try:
            return self._runner is not None and asyncio.current_task() == self._runner
        except RuntimeError:
            return False

    async def _wait_for_state(
        self,
        state: ConnectionFlowState,
        *other_states: ConnectionFlowState,
        timeout: Optional[float] = None,
    ) -> None:
        states = (state, *other_states)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.state in states:
                return
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise asyncio.TimeoutError
            try:
                await asyncio.wait_for(self._state_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                if deadline is None or time.monotonic() >= deadline:
                    raise

    async def _voice_connect(self, *, self_deaf: bool = False, self_mute: bool = False) -> None:
        channel = self.voice_client.channel
        await channel.guild.change_voice_state(channel=channel, self_deaf=self_deaf, self_mute=self_mute)

    async def _voice_disconnect(self) -> None:
        _log.info(
            'The voice handshake is being terminated for Channel ID %s (Guild ID %s)',
            self.voice_client.channel.id,
            self.voice_client.guild.id,
        )
        self.state = ConnectionFlowState.disconnected
        # Set before awaiting `change_voice_state`: the VOICE_STATE_UPDATE it triggers must be
        # recognised as ours, otherwise it looks like an external disconnect and recurses.
        self._expecting_disconnect = True
        self._disconnected.clear()
        await self.voice_client.channel.guild.change_voice_state(channel=None)

    async def _poll_voice_ws(self, reconnect: bool) -> None:
        backoff = ExponentialBackoff()
        while True:
            try:
                code, reason = await self._native.wait_closed()
            except asyncio.CancelledError:
                return

            # The following close codes are undocumented:
            # 1000 - normal closure
            # 4014 - externally disconnected (channel deleted, moved, ...)
            # 4015 - voice server has crashed, we should resume
            # 4021 - rate limited, we should not reconnect
            # 4022 - call terminated, similar to 4014
            if code == 1000:
                if not self._expecting_disconnect:
                    _log.info('Disconnecting from voice normally, close code %d.', code)
                    await self.disconnect()
                break

            if code in (4014, 4022):
                if self._disconnected.is_set():
                    _log.info('Disconnected from voice by discord, close code %d.', code)
                    await self.disconnect()
                    break

                _log.info('Disconnected from voice by force... potentially reconnecting.')
                if await self._potential_reconnect():
                    continue

                _log.info('Reconnect was unsuccessful, disconnecting from voice normally...')
                if self.state is not ConnectionFlowState.disconnected:
                    await self.disconnect()
                break

            if code == 4021:
                _log.warning('We are being ratelimited while trying to connect to voice. Disconnecting...')
                if self.state is not ConnectionFlowState.disconnected:
                    await self.disconnect()
                break

            if code == 4015:
                _log.info('Disconnected from voice, attempting a resume...')
                try:
                    await self._handshake_websocket(resume=True)
                except (asyncio.TimeoutError, ConnectionClosedError):
                    _log.info('Could not resume the voice connection... Disconnecting...')
                    if self.state is not ConnectionFlowState.disconnected:
                        await self.disconnect()
                    break
                else:
                    _log.info('Successfully resumed voice connection')
                    continue

            _log.debug('Not handling close code %s (%s)', code, reason or 'no reason')

            if not reconnect:
                await self.disconnect()
                raise ConnectionClosedError(f'voice websocket closed ({code})')

            retry = backoff.delay()
            _log.info('Disconnected from voice... Reconnecting in %.2fs.', retry)
            await asyncio.sleep(retry)
            await self.disconnect(cleanup=False)

            try:
                await self._connect(
                    reconnect=reconnect,
                    timeout=self.timeout,
                    self_deaf=self.self_deaf,
                    self_mute=self.self_mute,
                )
            except (asyncio.TimeoutError, TimeoutError, ClientException):
                _log.warning('Could not connect to voice... Retrying...')
                continue

    async def _potential_reconnect(self) -> bool:
        try:
            await self._wait_for_state(
                ConnectionFlowState.got_voice_server_update,
                ConnectionFlowState.got_both_voice_updates,
                ConnectionFlowState.disconnected,
                timeout=self.timeout,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return False

        if self.state is ConnectionFlowState.disconnected:
            return False

        try:
            await self._handshake_websocket(resume=False)
        except (asyncio.TimeoutError, TimeoutError, ConnectionClosedError):
            return False
        return True

    async def _move_to(self, channel: Any) -> None:
        await self.voice_client.channel.guild.change_voice_state(channel=channel)
        self.state = ConnectionFlowState.set_guild_voice_state

    def _update_voice_channel(self, channel_id: Optional[int]) -> None:
        self.voice_client.channel = channel_id and self.guild.get_channel(channel_id)  # type: ignore[assignment]
        self._native.channel_id = int(channel_id or 0)
