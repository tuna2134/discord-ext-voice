"""A `discord.py` voice backend implemented with :class:`discord.VoiceProtocol`.

``VoiceProtocol`` is discord.py's documented extension point for third-party voice backends
("This class allows you to implement a protocol to allow for an external method of sending
voice, such as Lavalink or a native library implementation"). This module implements it on top
of the native connection, so nothing from discord.py's own `VoiceClient` — its PyNaCl-based
encryption, its Python `davey` session, its capability guards — is inherited or worked around.

This is the only Python file left besides :mod:`discord_ext_voice.player`. Everything
voice-specific is native:

==============================  ==============================================================
Concern                         Rust module
==============================  ==============================================================
Voice websocket + RTP           ``transport.rs``
RTP encryption                  ``crypto.rs``
DAVE / MLS                      ``dave.rs``
Opus codec                      ``opus.rs``
Audio sources                   ``source.rs`` + ``sources.rs``
Audio demux/decode/resample     ``symphonia`` + ``rubato``
Connection flow state           ``flow.rs`` + ``connection.rs``
Playback clock                  ``player.rs``
Python bindings                 ``pyo3`` + ``pyo3-async-runtimes``
==============================  ==============================================================

The discord.py boundary is deliberately confined to this class: joining, leaving and moving are
``guild.change_voice_state`` calls made here, in Python, and the resulting gateway events are fed
to the native connection. Rust never holds a discord.py object.

Usage is unchanged::

    import discord_ext_voice as dev

    voice = await channel.connect(cls=dev.VoiceClient)
    voice.play(dev.FFmpegPCMAudio('song.mp3'))
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional, Tuple

from discord.errors import ClientException, ConnectionClosed
from discord.voice_client import VoiceProtocol

from . import _native
from .player import AudioPlayer

__all__ = ('VoiceClient', 'VoiceProtocol', 'has_nacl', 'has_dave')

_log = logging.getLogger(__name__)

#: Whether RTP encryption is available. Always ``True``: it is implemented natively with
#: RustCrypto instead of PyNaCl.
has_nacl: bool = True

#: Whether DAVE end-to-end encryption is available. Always ``True``: the MLS session is
#: implemented natively with the ``davey`` crate.
has_dave: bool = True

#: The connection flow phases, in discord.py's names and order.
ConnectionFlowState = _native.ConnectionFlowState

#: Defaults used by :meth:`VoiceClient.play` for PCM sources. They mirror discord.py, where
#: `expected_packet_loss=0.15` makes Opus spend most of its CPU on in-band FEC. A bot that
#: plays many streams can trade packet-loss resilience for CPU by setting e.g.
#: ``voice.expected_packet_loss = 0.02`` (or ``voice.fec = False``) once per client.
DEFAULT_FEC: bool = True
DEFAULT_EXPECTED_PACKET_LOSS: float = 0.15


class VoiceClient(VoiceProtocol):
    """Represents a Discord voice connection backed by the native transport.

    Parameters
    ----------
    client: :class:`discord.Client`
        The client that started the connection request.
    channel: :class:`discord.abc.Connectable`
        The voice channel being connected to.
    """

    #: The encryption modes this backend advertises. Taken from discord.py's list so the two can
    #: never disagree; the test suite also asserts the native list matches it.
    supported_modes: Tuple[str, ...] = tuple(_native.SUPPORTED_MODES)

    #: Opus encoder defaults for PCM sources. Set them on an instance to change the cost/quality
    #: tradeoff for that voice client; ``expected_packet_loss`` has the largest effect.
    fec: bool = DEFAULT_FEC
    expected_packet_loss: float = DEFAULT_EXPECTED_PACKET_LOSS

    def __init__(self, client: Any, channel: Any) -> None:
        super().__init__(client, channel)
        state = client._connection
        self.loop = state.loop
        #: The guild is cached: once Discord removes us, `channel` can become `None`, but the
        #: gateway still has to be told to leave (and logs still name the guild).
        self._guild = getattr(channel, 'guild', None)
        self._state = state
        self._player: Optional[AudioPlayer] = None
        self._timeout: float = 30.0
        self._reconnect: bool = True
        self._self_deaf: bool = False
        self._self_mute: bool = False
        self._runner: Optional[asyncio.Task[None]] = None
        self._connector: Optional[asyncio.Task[None]] = None
        self._disconnected = asyncio.Event()
        #: The transport, the flow state and the negotiated session, all native.
        self._connection: _native.VoiceConnection = self.create_connection_state()

    # ------------------------------------------------------------------ state accessors

    def create_connection_state(self) -> _native.VoiceConnection:
        """Build the native connection that drives this client."""
        channel = self.channel
        return _native.VoiceConnection(
            int(getattr(channel.guild, 'id', 0) or 0),
            int(getattr(self.user, 'id', 0) or 0),
            int(channel.id),
        )

    @property
    def native(self) -> _native.VoiceConnection:
        """The underlying :class:`discord_ext_voice._native.VoiceConnection`."""
        return self._connection

    @property
    def guild(self) -> Any:
        """:class:`discord.Guild`: The guild we're connected to."""
        channel = self.channel
        return channel.guild if channel is not None else self._guild

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
    def timeout(self) -> float:
        """:class:`float`: How long to wait for the connection flow, in seconds."""
        return self._timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        self._timeout = float(value)

    @property
    def latency(self) -> float:
        """:class:`float`: Latency between a HEARTBEAT and a HEARTBEAT_ACK in seconds."""
        return self._connection.latency

    @property
    def average_latency(self) -> float:
        """:class:`float`: Average of the most recent 20 HEARTBEAT latencies in seconds."""
        return self._connection.average_latency

    @property
    def voice_privacy_code(self) -> Optional[str]:
        """Optional[:class:`str`]: The voice privacy code of this E2EE session's group.

        A new privacy code is created each time a new transition is executed. This is ``None``
        when there is no active DAVE session.
        """
        return self._connection.voice_privacy_code

    # ------------------------------------------------------------------ the discord.py boundary

    async def _change_voice_state(
        self,
        channel: Optional[Any],
        *,
        self_deaf: bool = False,
        self_mute: bool = False,
    ) -> None:
        """Ask the main gateway to join, move to or leave a channel.

        This is the only discord.py write this library makes. It lives here, in Python, so the
        native side never has to know about guilds, channels or the gateway.
        """
        await self.guild.change_voice_state(
            channel=channel, self_deaf=self_deaf, self_mute=self_mute
        )

    # ------------------------------------------------------------------ connection

    async def on_voice_state_update(self, data: dict, /) -> None:
        """|coro|

        Called by discord.py for every ``VOICE_STATE_UPDATE`` payload addressed to us.
        """
        channel_id = data.get('channel_id')
        current = getattr(self.channel, 'id', None)

        if channel_id is None:
            # The native flow records that we are out of the channel and consumes an expected
            # disconnect, so the action tells us whether to follow Discord or not.
            self._disconnected.set()
            action = self._connection.voice_state_update(
                None, None if current is None else int(current)
            )
            if action is _native.VoiceStateAction.disconnect:
                _log.debug('We were externally disconnected from voice')
                await self.disconnect()
            return

        channel_id = int(channel_id)
        self._connection.set_session_id(data.get('session_id'))
        action = self._connection.voice_state_update(
            channel_id, None if current is None else int(current)
        )
        if action is _native.VoiceStateAction.update_channel:
            self._update_voice_channel(channel_id)
        elif action is _native.VoiceStateAction.move_during_handshake:
            # We were moved mid-handshake: run the flow again on the new channel.
            _log.info('Handling channel move while connecting...')
            self._update_voice_channel(channel_id)
            await self._soft_disconnect(with_state=ConnectionFlowState.got_voice_state_update)
            self._spawn_connect()

    async def on_voice_server_update(self, data: dict, /) -> None:
        """|coro|

        Called by discord.py for every ``VOICE_SERVER_UPDATE`` payload addressed to us.
        """
        token = data.get('token')
        endpoint = data.get('endpoint')
        server_id = int(data['guild_id']) if data.get('guild_id') is not None else None

        # Feed the native transport *before* the flow below runs: that can schedule a reconnect,
        # which would otherwise race with this update.
        changed = False
        if token is not None and endpoint is not None and server_id is not None:
            changed = self._connection.set_server_info(server_id, token, endpoint)

        action = self._connection.voice_server_update(
            has_endpoint=endpoint is not None,
            has_token=token is not None,
            changed=changed,
        )

        if endpoint is None or token is None:
            _log.warning(
                'Awaiting endpoint... This requires waiting. If timeout occurred considering '
                'raising the timeout and reconnecting.'
            )
            return

        if action is _native.VoiceServerAction.close_old_websocket:
            _log.debug('Voice server update, closing old voice websocket')
            await self._connection.close(4014)
        elif action is _native.VoiceServerAction.reconnect_flow:
            _log.debug('Unexpected server update event, attempting to handle')
            await self._soft_disconnect(with_state=ConnectionFlowState.got_voice_server_update)
            self._spawn_connect()

    async def connect(
        self,
        *,
        reconnect: bool,
        timeout: float,
        self_deaf: bool = False,
        self_mute: bool = False,
    ) -> None:
        """|coro|

        Connects to voice and completes the handshake.
        """
        if self.is_connected():
            raise ClientException('Already connected to voice.')

        self._timeout = timeout
        self._reconnect = reconnect
        self._self_deaf = self_deaf
        self._self_mute = self_mute
        self._spawn_connect()
        assert self._connector is not None
        await self._connector

    def _spawn_connect(self) -> None:
        """Start the connect flow without waiting for it.

        The gateway event handlers use this when Discord moves us mid-handshake: the flow has to
        run again, but the handler itself must return to the gateway.
        """
        if self._connector is not None:
            self._connector.cancel()
            self._connector = None

        if self._runner is not None:
            self._runner.cancel()
            self._runner = None

        self._connector = asyncio.get_running_loop().create_task(
            self._wrap_connect(self._reconnect, self._timeout, self._self_deaf, self._self_mute),
            name='discord-ext-voice connector',
        )

    async def _wrap_connect(
        self, reconnect: bool, timeout: float, self_deaf: bool, self_mute: bool
    ) -> None:
        try:
            await self._connect(reconnect, timeout, self_deaf, self_mute, resume=False)
        except asyncio.CancelledError:
            _log.debug('Cancelling voice connection')
            await self._soft_disconnect()
            raise
        except asyncio.TimeoutError:
            _log.info('Timed out connecting to voice')
            await self._disconnect(force=True)
            raise
        except Exception:
            _log.exception('Error connecting to voice... disconnecting')
            await self._disconnect(force=True)
            raise

    async def _inner_connect(
        self, reconnect: bool, self_deaf: bool, self_mute: bool, resume: bool
    ) -> None:
        for attempt in range(_native.MAX_ATTEMPTS):
            _log.info('Starting voice handshake... (connection attempt %d)', attempt + 1)

            await self._change_voice_state(
                self.channel, self_deaf=self_deaf, self_mute=self_mute
            )
            self._connection.note_attempt()

            await self._wait_for_state(ConnectionFlowState.got_both_voice_updates)

            _log.info('Voice handshake complete. Endpoint found: %s', self.endpoint)

            try:
                await self._open_websocket(resume)
                break
            except ConnectionClosed:
                if reconnect:
                    wait = _native.retry_wait(attempt)
                    _log.exception('Failed to connect to voice... Retrying in %ss...', wait)
                    await self._disconnect(force=True, cleanup=False)
                    await asyncio.sleep(wait)
                    continue
                await self._disconnect(force=True)
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

        if self._runner is None or self._runner.done():
            self._runner = asyncio.get_running_loop().create_task(
                self._poll_voice_ws(reconnect), name='discord-ext-voice websocket poller'
            )

    async def _open_websocket(self, resume: bool) -> None:
        """Open the native websocket and complete the handshake."""
        self._connection.state = ConnectionFlowState.websocket_connected
        try:
            await self._connection.handshake(resume=resume, timeout=self._timeout)
        except _native.HandshakeError as exc:
            # A connect timeout is an `asyncio.TimeoutError` for discord.py (and retried).
            raise asyncio.TimeoutError(str(exc)) from exc
        except _native.ConnectionClosedError as exc:
            # Discord puts the interesting part in the close code: 4006 and 4014/4015 mean the
            # session is gone and the flow should retry, 4021 means it was rate limited.
            code = self._connection.close_code
            _log.warning(
                'The voice websocket closed during the handshake (code=%s, reason=%s)',
                code if code is not None else '<none>',
                self._connection.close_reason or '<none>',
            )
            raise ConnectionClosed(self._connection, shard_id=None, code=code or None) from exc

    async def disconnect(self, *, force: bool = False) -> None:
        """|coro|

        Disconnects this voice client from voice.
        """
        self.stop()
        _log.info('Disconnecting from voice (force=%s)', force)
        await self._disconnect(force=force, wait=True)
        self.cleanup()
        self._cancel_tasks()
        _log.info('Disconnected from voice')

    def _cancel_tasks(self) -> None:
        """Cancel the connector and poller, unless this coroutine is one of them."""
        current = asyncio.current_task()
        if self._connector is not None and self._connector is not current:
            self._connector.cancel()
            self._connector = None
        if self._runner is not None and self._runner is not current:
            self._runner.cancel()
            self._runner = None

    async def _disconnect(
        self, *, force: bool = True, cleanup: bool = True, wait: bool = False
    ) -> None:
        """Leave the channel and tear the native session down."""
        if not force and not self.is_connected() and not self._connection.joined:
            return

        requested = False
        try:
            requested = await self._leave_voice()
            await self._connection.close()
        except Exception:
            _log.warning('Ignoring exception disconnecting from voice', exc_info=True)
        finally:
            self._connection.state = ConnectionFlowState.disconnected

            if cleanup:
                self.stop()

            # Skip this part if disconnect was called from the poll loop task, or if the gateway
            # was never told to remove us (there would be nothing to confirm).
            if wait and requested and not self._inside_runner():
                try:
                    await asyncio.wait_for(self._disconnected.wait(), timeout=self._timeout)
                except asyncio.TimeoutError:
                    _log.warning(
                        'Timed out after %ss waiting for the voice disconnection confirmation; '
                        'carrying on',
                        self._timeout,
                    )
                except asyncio.CancelledError:
                    pass

            if cleanup:
                self.cleanup()

    async def _leave_voice(self) -> bool:
        """Ask the main gateway to remove us. Returns whether the request went out."""
        channel = self.channel
        guild = getattr(channel, 'guild', None) if channel is not None else None
        _log.info(
            'The voice handshake is being terminated for Channel ID %s (Guild ID %s)',
            getattr(channel, 'id', None),
            getattr(guild, 'id', None),
        )
        self._connection.state = ConnectionFlowState.disconnected
        # Arm the confirmation *before* the request goes out: the gateway can deliver the
        # resulting `VOICE_STATE_UPDATE` before `change_voice_state` returns.
        self._connection.expecting_disconnect = True
        self._connection.disconnected = False
        self._disconnected.clear()
        try:
            await self._change_voice_state(None)
        except Exception as exc:
            # A gateway that is reconnecting cannot take the request; there is nothing to wait
            # for, so let the caller carry on instead of blocking for the whole timeout.
            _log.warning('Could not tell the gateway to leave voice: %r', exc)
            self._connection.expecting_disconnect = False
            return False
        self._connection.joined = False
        return True

    async def _soft_disconnect(
        self, *, with_state: Any = ConnectionFlowState.got_both_voice_updates
    ) -> None:
        """Drop the socket but stay in the flow, ready to connect again."""
        _log.debug('Soft disconnecting from voice')

        # Stop the poller, because closing the socket would otherwise trigger a reconnect.
        if self._runner is not None:
            self._runner.cancel()
            self._runner = None

        try:
            await self._connection.close()
        except Exception:
            _log.debug('Ignoring exception soft disconnecting from voice', exc_info=True)
        finally:
            self._connection.state = with_state

    async def move_to(self, channel: Optional[Any], *, timeout: Optional[float] = 30.0) -> None:
        """|coro|

        Moves you to a different voice channel.
        """
        if channel is None:
            # This is only called externally, so waiting for the disconnect is fine.
            await self._disconnect(force=True, wait=True)
            return

        if self.channel is not None and channel.id == self.channel.id:
            return

        previous_state = self._connection.state

        # This is only an outgoing gateway request: if it fails, nothing changes but our state.
        await self._change_voice_state(channel)
        self._connection.state = ConnectionFlowState.set_guild_voice_state

        last_state = self._connection.state
        try:
            await self._wait_for_state(ConnectionFlowState.connected, timeout=timeout)
        except asyncio.TimeoutError:
            _log.warning(
                'Timed out trying to move to channel %s in guild %s', channel.id, self.guild.id
            )
            if self._connection.state == last_state:
                _log.debug('Reverting to previous state %s', previous_state.name)
                self._connection.state = previous_state

    def _update_voice_channel(self, channel_id: Optional[int]) -> None:
        guild = self.guild
        self.channel = guild.get_channel(channel_id) if channel_id else None
        if channel_id:
            # Keep the native session's channel binding (used by DAVE) in step.
            self._connection.channel_id = int(channel_id)

    def is_connected(self) -> bool:
        """Indicates if the voice client is connected to voice."""
        return self._connection.is_connected()

    def wait_until_connected(self, timeout: Optional[float] = 30.0) -> bool:
        """Block until the connection is established (or ``timeout`` expires)."""
        self._connection.wait_connected(timeout)
        return self.is_connected()

    # ------------------------------------------------------------------ waiting

    async def _wait_for_state(self, *states: Any, timeout: Optional[float] = None) -> None:
        values = [int(state.value) for state in states]
        if not await self._connection.wait_for_state(values, timeout):
            raise asyncio.TimeoutError

    def _inside_runner(self) -> bool:
        return self._runner is not None and asyncio.current_task() is self._runner

    def add_socket_listener(self, callback: Callable[[bytes], Any]) -> None:
        """Not supported: this library implements the sending side of voice only."""
        raise NotImplementedError('receiving voice packets is not implemented')

    def remove_socket_listener(self, callback: Callable[[bytes], Any]) -> None:
        """Not supported: this library implements the sending side of voice only."""
        raise NotImplementedError('receiving voice packets is not implemented')

    # ------------------------------------------------------------------ reconnecting

    async def _poll_voice_ws(self, reconnect: bool) -> None:
        """Watch the socket and apply the close-code policy once it ends."""
        self._connection.reset_backoff()
        while True:
            try:
                code, _reason = await self._connection.wait_closed()
            except asyncio.CancelledError:
                return

            action = self._connection.close_action(code, reconnect)

            if action is _native.CloseAction.normal:
                _log.info('Disconnecting from voice normally, close code %d.', code)
                await self.disconnect()
                break

            if action is _native.CloseAction.quiet:
                # Don't disconnect a second time when the socket closed because we closed it.
                break

            if action is _native.CloseAction.by_discord:
                _log.info('Disconnected from voice by discord, close code %d.', code)
                await self.disconnect()
                break

            if action is _native.CloseAction.potential_reconnect:
                # This is a race between the main gateway event and the socket closing. We may
                # have been moved to a different channel.
                _log.info('Disconnected from voice by force... potentially reconnecting.')
                if not await self._potential_reconnect():
                    _log.info('Reconnect was unsuccessful, disconnecting from voice normally...')
                    if self._connection.state is not ConnectionFlowState.disconnected:
                        await self.disconnect()
                    break
                continue

            if action is _native.CloseAction.rate_limited:
                _log.warning(
                    'We are being ratelimited while trying to connect to voice. Disconnecting...'
                )
                if self._connection.state is not ConnectionFlowState.disconnected:
                    await self.disconnect()
                break

            if action is _native.CloseAction.resume:
                _log.info('Disconnected from voice, attempting a resume...')
                try:
                    await self._connect(
                        reconnect=reconnect,
                        timeout=self._timeout,
                        self_deaf=self._self_deaf,
                        self_mute=self._self_mute,
                        resume=True,
                    )
                except asyncio.TimeoutError:
                    _log.info('Could not resume the voice connection... Disconnecting...')
                    if self._connection.state is not ConnectionFlowState.disconnected:
                        await self.disconnect()
                    break
                _log.info('Successfully resumed voice connection')
                continue

            if action is _native.CloseAction.reconnect:
                retry = self._connection.reconnect_delay()
                _log.warning('Disconnected from voice... Reconnecting in %.2fs.', retry)
                await asyncio.sleep(retry)
                await self._disconnect(force=True, cleanup=False)

                try:
                    await self._connect(
                        reconnect=reconnect,
                        timeout=self._timeout,
                        self_deaf=self._self_deaf,
                        self_mute=self._self_mute,
                        resume=False,
                    )
                except asyncio.TimeoutError:
                    # At this point the inner flow has retried 5 times, so go around again.
                    _log.warning('Could not connect to voice... Retrying...')
                    continue
                continue

            # `CloseAction.raise_`: reconnecting is off, so this is fatal.
            _log.debug('Not handling close code %s (%s)', code, _reason or 'no reason')
            await self.disconnect()
            raise ConnectionClosed(self._connection, shard_id=None, code=code)

    async def _potential_reconnect(self) -> bool:
        """Try to connect to the channel again after Discord moved us."""
        try:
            await self._wait_for_state(
                ConnectionFlowState.got_voice_server_update,
                ConnectionFlowState.got_both_voice_updates,
                ConnectionFlowState.disconnected,
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            return False

        if self._connection.state is ConnectionFlowState.disconnected:
            return False

        try:
            await self._open_websocket(False)
        except (ConnectionClosed, asyncio.TimeoutError):
            return False
        return True

    # ------------------------------------------------------------------ audio

    def play(
        self,
        source: Any,
        *,
        after: Optional[Callable[[Optional[Exception]], Any]] = None,
        application: str = 'audio',
        bitrate: int = 128,
        fec: Optional[bool] = None,
        expected_packet_loss: Optional[float] = None,
        bandwidth: str = 'full',
        signal_type: str = 'auto',
    ) -> None:
        """Plays an :class:`AudioSource`.

        The finalizer ``after`` is called after the source has been exhausted or an error
        occurred. The encoder parameters are only used for PCM based sources; the native player
        validates them and raises :class:`ValueError` for a bad value.

        ``fec`` and ``expected_packet_loss`` default to this client's attributes (which start as
        :data:`DEFAULT_FEC` / :data:`DEFAULT_EXPECTED_PACKET_LOSS`, discord.py's values). In-band
        FEC is by far the most expensive part of Opus encoding: at 15% packet loss a 20ms frame
        costs about 1.2ms, at 2% about 0.22ms. Lower it when playing many streams and the network
        is reliable; ``fec=False`` also works but gives up loss resilience entirely.
        """
        if not self.is_connected():
            raise ClientException('Not connected to voice.')

        if self.is_playing():
            raise ClientException('Already playing audio.')

        if fec is None:
            fec = self.fec
        if expected_packet_loss is None:
            expected_packet_loss = self.expected_packet_loss

        # The playback pipeline is native-only: a source implemented from scratch in Python is
        # rejected rather than read through the interpreter.
        if not _native.is_audio_source(source):
            raise TypeError(f'source must be a native AudioSource not {source.__class__.__name__}')

        opus = _native.is_audio_opus(source)
        if opus:
            # The packets already are Opus: no encoder is involved at all.
            self._connection.clear_encoder()
        else:
            self._connection.set_encoder(
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
            opus,
            'none' if opus else 'native',
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
    def source(self) -> Any:
        """Optional[:class:`AudioSource`]: The audio source being played, if playing.

        This property can also be used to change the audio source currently being played.
        """
        return self._player.source if self._player else None

    @source.setter
    def source(self, value: Any) -> None:
        if not _native.is_audio_source(value):
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
            self._connection.dave_protocol_version > 0,
        )
        try:
            self._connection.send_audio_packet(data, encode=encode)
        except _native.NotConnected as exc:
            raise ClientException(str(exc)) from exc

    def __repr__(self) -> str:
        return (
            f'<VoiceClient guild={getattr(self.channel.guild, "id", None)} '
            f'channel={getattr(self.channel, "id", None)} connected={self.is_connected()}>'
        )
