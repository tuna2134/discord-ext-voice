//! The connection flow: the decisions `discord.py`'s `VoiceConnectionState` encodes.
//!
//! What lives here is the *policy* of the voice connection flow — which gateway event moves the
//! phase where, what a close code means, how long a retry waits and how long a reconnect backs
//! off. The awaits stay in Python (`discord_ext_voice.voice_state`), because they are discord.py
//! coroutines (`Guild.change_voice_state`, the native handshake, the websocket close): Python
//! asks the flow what to do and then does it.
//!
//! The phase itself is the transport's ([`crate::transport::VoiceState::phase`]), so there is one
//! state and not two: the handshake internals move it as the frames arrive, and these functions
//! move it as the main gateway events arrive — exactly the two writers `discord.py` has.
//!
//! ```text
//!   0 disconnected ──join──▶ 1 set_guild_voice_state
//!                            │  voice_state_update
//!                            ▼
//!   3 got_voice_server_update ◀── voice_server_update ── 2 got_voice_state_update
//!                            │                                  │
//!                            └──────────────┬───────────────────┘
//!                                           ▼
//!                              4 got_both_voice_updates
//!                                           │  IDENTIFY
//!                                           ▼
//!                        5 websocket_connected ▶ 6 ready ▶ 7 ip_discovery ▶ 8 connected
//! ```

use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use parking_lot::Mutex;
use pyo3::prelude::*;

use crate::transport::{Hub, Phase, VoiceState, wait_duration};

/// How many times the inner connect flow tries before giving up.
pub const MAX_ATTEMPTS: usize = 5;

/// How long the blocking wait sleeps between checks of the phase.
const WAIT_POLL: Duration = Duration::from_millis(2);

/// What `VoiceConnectionState.voice_state_update` does after the flow has seen the event.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum VoiceStateAction {
    /// Nothing to do: the phase moved, or the event was not about this channel.
    Ignore = 0,
    /// Discord moved or removed us: the client follows by disconnecting.
    Disconnect = 1,
    /// The channel changed: point the voice client (and the native session) at the new one.
    UpdateChannel = 2,
    /// The channel changed mid-handshake: run the flow again on the new channel.
    MoveDuringHandshake = 3,
}

/// What `VoiceConnectionState.voice_server_update` does after the flow has seen the event.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum VoiceServerAction {
    /// The endpoint was missing, or the event repeats what we already have.
    Ignore = 0,
    /// A new endpoint while connected: close the old websocket (4014) and wait for the updates.
    CloseOldWebsocket = 1,
    /// A new endpoint while the flow was running: soft disconnect and start over.
    ReconnectFlow = 2,
}

/// What the websocket poller does with a close code.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum CloseAction {
    /// Close code 1000, and we did not ask for it: disconnect normally.
    Normal = 0,
    /// Close code 1000 because we closed the socket: just stop polling.
    Quiet = 1,
    /// 4014/4022 with the leave confirmation already in: Discord removed us.
    ByDiscord = 2,
    /// 4014/4022 without it: we may have been moved, so try the channel again.
    PotentialReconnect = 3,
    /// 4021: rate limited while connecting, so give up.
    RateLimited = 4,
    /// 4015: the voice server crashed, so resume.
    Resume = 5,
    /// Anything else, with reconnecting enabled: back off and connect again.
    Reconnect = 6,
    /// Anything else, with reconnecting disabled: raise.
    Raise = 7,
}

/// The reconnect backoff `discord.py`'s poller uses.
///
/// A random delay in `[0, base * 2**n)`, with `n` capped at 10 and reset when the previous
/// invocation is more than `base * 2**11` seconds old.
pub struct Backoff {
    base: f64,
    exponent: u32,
    last: Option<Instant>,
}

impl Default for Backoff {
    fn default() -> Self {
        Self {
            base: 1.0,
            exponent: 0,
            last: None,
        }
    }
}

impl Backoff {
    pub fn reset(&mut self) {
        self.exponent = 0;
        self.last = None;
    }

    pub fn delay(&mut self) -> f64 {
        let now = Instant::now();
        if let Some(last) = self.last
            && now.duration_since(last).as_secs_f64() > self.base * 2f64.powi(11)
        {
            self.exponent = 0;
        }
        self.last = Some(now);
        self.exponent = (self.exponent + 1).min(10);
        rand::random::<f64>() * self.base * 2f64.powi(self.exponent as i32)
    }
}

/// Move the phase, keeping the transport's `closed` flag in step: a move to any phase but
/// `Disconnected` is the start of a new connection.
///
/// Returns the previous phase value, or `None` when `value` is not a phase (0-8).
pub fn set_phase(hub: &Hub, value: u8) -> Option<u8> {
    let phase = Phase::from_u8(value)?;
    let previous = {
        let mut state = hub.state.lock();
        let previous = state.phase.as_u8();
        if phase != Phase::Disconnected {
            state.closed = false;
        }
        state.phase = phase;
        previous
    };
    hub.bump();
    Some(previous)
}

/// The flow decisions, as pure functions over the connection state.
pub struct Flow;

impl Flow {
    /// A `VOICE_STATE_UPDATE` for our own user.
    pub fn voice_state_update(
        state: &mut VoiceState,
        channel_id: Option<u64>,
        current_channel_id: Option<u64>,
    ) -> VoiceStateAction {
        let Some(channel_id) = channel_id else {
            state.joined = false;
            // A `channel_id` of None is expected while disconnecting; anything else means Discord
            // moved or removed us, so the client has to follow.
            if state.expecting_disconnect {
                state.expecting_disconnect = false;
                return VoiceStateAction::Ignore;
            }
            return VoiceStateAction::Disconnect;
        };

        state.joined = true;
        let moved = current_channel_id != Some(channel_id);

        match state.phase {
            Phase::SetGuildVoiceState => {
                state.phase = Phase::GotVoiceStateUpdate;
                // We moved ourselves while connecting.
                if moved {
                    VoiceStateAction::UpdateChannel
                } else {
                    VoiceStateAction::Ignore
                }
            }
            Phase::GotVoiceServerUpdate => {
                state.phase = Phase::GotBothVoiceUpdates;
                VoiceStateAction::Ignore
            }
            Phase::Connected => VoiceStateAction::UpdateChannel,
            Phase::Disconnected => VoiceStateAction::Ignore,
            // Mid-handshake (2, 4, 5, 6, 7).
            _ => {
                if moved {
                    VoiceStateAction::MoveDuringHandshake
                } else {
                    VoiceStateAction::Ignore
                }
            }
        }
    }

    /// A `VOICE_SERVER_UPDATE`: the voice server endpoint and token.
    ///
    /// `changed` is whether the token, endpoint or server id differ from what the caller had
    /// before, which is what tells a repeat apart from a move.
    pub fn voice_server_update(
        state: &mut VoiceState,
        has_endpoint: bool,
        has_token: bool,
        changed: bool,
    ) -> VoiceServerAction {
        if !has_endpoint || !has_token {
            return VoiceServerAction::Ignore;
        }

        match state.phase {
            // We got the event while connecting.
            Phase::SetGuildVoiceState => {
                state.phase = Phase::GotVoiceServerUpdate;
                VoiceServerAction::Ignore
            }
            Phase::GotVoiceStateUpdate => {
                state.phase = Phase::GotBothVoiceUpdates;
                VoiceServerAction::Ignore
            }
            Phase::Connected => {
                state.phase = Phase::GotVoiceServerUpdate;
                VoiceServerAction::CloseOldWebsocket
            }
            Phase::Disconnected => VoiceServerAction::Ignore,
            _ => {
                if changed {
                    state.phase = Phase::GotVoiceServerUpdate;
                    VoiceServerAction::ReconnectFlow
                } else {
                    VoiceServerAction::Ignore
                }
            }
        }
    }

    /// What the poller does with `code`.
    ///
    /// The codes Discord documents for the voice gateway:
    ///
    /// * 1000 — normal closure
    /// * 4014 — we were externally disconnected (kicked, channel deleted, moved)
    /// * 4015 — the voice server crashed, so resume
    /// * 4021 — rate limited, do not reconnect
    /// * 4022 — the call was terminated, similar to 4014
    pub fn close_action(
        state: &VoiceState,
        code: u16,
        disconnected: bool,
        reconnect: bool,
    ) -> CloseAction {
        match code {
            1000 => {
                if state.expecting_disconnect {
                    CloseAction::Quiet
                } else {
                    CloseAction::Normal
                }
            }
            4014 | 4022 => {
                if disconnected {
                    CloseAction::ByDiscord
                } else {
                    CloseAction::PotentialReconnect
                }
            }
            4021 => CloseAction::RateLimited,
            4015 => CloseAction::Resume,
            _ => {
                if reconnect {
                    CloseAction::Reconnect
                } else {
                    CloseAction::Raise
                }
            }
        }
    }

    /// A join request went out: we are in the channel, and the flow starts if it was idle.
    ///
    /// Setting the phase unnecessarily breaks reconnecting, which is why it only happens from
    /// `Disconnected`.
    pub fn note_attempt(state: &mut VoiceState) {
        state.joined = true;
        if state.phase == Phase::Disconnected {
            state.phase = Phase::SetGuildVoiceState;
        }
    }

    /// How long attempt `attempt` (zero based) waits before trying again.
    pub fn retry_wait(attempt: usize) -> f64 {
        1.0 + attempt as f64 * 2.0
    }
}

/// The connection flow of one voice connection, as a Python object.
///
/// Reached through `VoiceConnection.flow`. Every method is a decision the Python flow dispatches
/// on; the phase lives in the transport, so `state` here is the same number as
/// `VoiceConnectionState.state`.
#[pyclass(module = "discord_ext_voice._native", name = "ConnectionFlow")]
pub struct PyConnectionFlow {
    hub: Arc<Hub>,
    backoff: Arc<Mutex<Backoff>>,
}

impl PyConnectionFlow {
    pub fn new(hub: Arc<Hub>, backoff: Arc<Mutex<Backoff>>) -> Self {
        Self { hub, backoff }
    }
}

#[pymethods]
impl PyConnectionFlow {
    /// Where the flow is: a `ConnectionFlowState` value (0-8).
    #[getter]
    fn state(&self) -> u8 {
        self.hub.state.lock().phase.as_u8()
    }

    /// Move the flow to `value`, returning the value it had before.
    fn set_state(&self, value: u8) -> PyResult<u8> {
        set_phase(&self.hub, value).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "{value} is not a connection flow state (0-8)"
            ))
        })
    }

    /// Whether the main gateway still has us in a channel.
    #[getter]
    fn joined(&self) -> bool {
        self.hub.state.lock().joined
    }

    #[setter]
    fn set_joined(&self, joined: bool) {
        self.hub.state.lock().joined = joined;
    }

    /// Whether a leave request is in flight.
    #[getter]
    fn expecting_disconnect(&self) -> bool {
        self.hub.state.lock().expecting_disconnect
    }

    #[setter]
    fn set_expecting_disconnect(&self, expecting: bool) {
        self.hub.state.lock().expecting_disconnect = expecting;
    }

    /// Handle a `VOICE_STATE_UPDATE` for our own user, returning a `VOICE_STATE_ACTION_*`.
    fn voice_state_update(&self, channel_id: Option<u64>, current_channel_id: Option<u64>) -> u8 {
        let action = {
            let mut state = self.hub.state.lock();
            Flow::voice_state_update(&mut state, channel_id, current_channel_id)
        };
        self.hub.bump();
        action as u8
    }

    /// Handle a `VOICE_SERVER_UPDATE`, returning a `VOICE_SERVER_ACTION_*`.
    fn voice_server_update(&self, has_endpoint: bool, has_token: bool, changed: bool) -> u8 {
        let action = {
            let mut state = self.hub.state.lock();
            Flow::voice_server_update(&mut state, has_endpoint, has_token, changed)
        };
        self.hub.bump();
        action as u8
    }

    /// Record that a join request went out: we are in the channel, and the flow starts if it
    /// was idle.
    fn note_attempt(&self) {
        {
            let mut state = self.hub.state.lock();
            Flow::note_attempt(&mut state);
        }
        self.hub.bump();
    }

    /// What the websocket poller does with a close code, as a `CLOSE_ACTION_*`.
    fn close_action(&self, code: u16, disconnected: bool, reconnect: bool) -> u8 {
        let state = self.hub.state.lock();
        Flow::close_action(&state, code, disconnected, reconnect) as u8
    }

    /// How long attempt `attempt` (zero based) waits before trying again.
    #[staticmethod]
    fn retry_wait(attempt: usize) -> f64 {
        Flow::retry_wait(attempt)
    }

    /// The jittered reconnect delay for the next attempt.
    fn reconnect_delay(&self) -> f64 {
        self.backoff.lock().delay()
    }

    /// Restart the backoff sequence, as a fresh poller does.
    fn reset_backoff(&self) {
        self.backoff.lock().reset();
    }

    /// Wait until the flow reaches one of `states`, or `timeout` elapses.
    ///
    /// Returns whether the state was reached; the caller raises `asyncio.TimeoutError`, so the
    /// timeout is the caller's to report. Awaitable, and woken by the transport's notification
    /// handle rather than by polling.
    #[pyo3(signature = (states, timeout=None))]
    fn wait_for_state<'py>(
        &self,
        py: Python<'py>,
        states: Vec<u8>,
        timeout: Option<f64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let mut wanted = Vec::with_capacity(states.len());
        for value in states {
            let Some(phase) = Phase::from_u8(value) else {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "{value} is not a connection flow state (0-8)"
                )));
            };
            wanted.push(phase);
        }

        let hub = Arc::clone(&self.hub);
        let timeout = timeout.map(wait_duration);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            Ok(hub
                .wait_for(timeout, |state| wanted.contains(&state.phase))
                .await)
        })
    }

    /// Block until the flow reaches `connected`, or `timeout` elapses.
    ///
    /// The GIL is released for the wait, so the interpreter keeps running (the connection is
    /// completed by the event loop, which needs it).
    #[pyo3(signature = (timeout=None))]
    fn wait_connected(&self, py: Python<'_>, timeout: Option<f64>) -> bool {
        let hub = Arc::clone(&self.hub);
        let deadline = timeout.map(|seconds| Instant::now() + wait_duration(seconds));
        py.detach(move || {
            loop {
                if hub.state.lock().phase.is_connected() {
                    return true;
                }
                if deadline.is_some_and(|deadline| Instant::now() >= deadline) {
                    return hub.state.lock().phase.is_connected();
                }
                thread::sleep(WAIT_POLL);
            }
        })
    }

    /// How many times the inner connect flow tries before giving up.
    #[classattr]
    const MAX_ATTEMPTS: usize = MAX_ATTEMPTS;

    // ---- actions, so that Python never hard-codes a number ------------------------------

    #[classattr]
    const VOICE_STATE_IGNORE: u8 = VoiceStateAction::Ignore as u8;
    #[classattr]
    const VOICE_STATE_DISCONNECT: u8 = VoiceStateAction::Disconnect as u8;
    #[classattr]
    const VOICE_STATE_UPDATE_CHANNEL: u8 = VoiceStateAction::UpdateChannel as u8;
    #[classattr]
    const VOICE_STATE_MOVE_DURING_HANDSHAKE: u8 = VoiceStateAction::MoveDuringHandshake as u8;

    #[classattr]
    const VOICE_SERVER_IGNORE: u8 = VoiceServerAction::Ignore as u8;
    #[classattr]
    const VOICE_SERVER_CLOSE_OLD_WEBSOCKET: u8 = VoiceServerAction::CloseOldWebsocket as u8;
    #[classattr]
    const VOICE_SERVER_RECONNECT_FLOW: u8 = VoiceServerAction::ReconnectFlow as u8;

    #[classattr]
    const CLOSE_NORMAL: u8 = CloseAction::Normal as u8;
    #[classattr]
    const CLOSE_QUIET: u8 = CloseAction::Quiet as u8;
    #[classattr]
    const CLOSE_BY_DISCORD: u8 = CloseAction::ByDiscord as u8;
    #[classattr]
    const CLOSE_POTENTIAL_RECONNECT: u8 = CloseAction::PotentialReconnect as u8;
    #[classattr]
    const CLOSE_RATE_LIMITED: u8 = CloseAction::RateLimited as u8;
    #[classattr]
    const CLOSE_RESUME: u8 = CloseAction::Resume as u8;
    #[classattr]
    const CLOSE_RECONNECT: u8 = CloseAction::Reconnect as u8;
    #[classattr]
    const CLOSE_RAISE: u8 = CloseAction::Raise as u8;

    fn __repr__(&self) -> String {
        let state = self.hub.state.lock();
        format!(
            "<discord_ext_voice.ConnectionFlow state={:?} joined={} expecting_disconnect={}>",
            state.phase, state.joined, state.expecting_disconnect
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn state() -> VoiceState {
        VoiceState::new(1, 2, 3)
    }

    fn set(state: &mut VoiceState, phase: Phase) {
        state.phase = phase;
    }

    // ---- VOICE_STATE_UPDATE ----

    #[test]
    fn voice_state_update_walks_the_phase_ladder() {
        let mut state = state();
        set(&mut state, Phase::SetGuildVoiceState);
        assert_eq!(
            Flow::voice_state_update(&mut state, Some(3), Some(3)),
            VoiceStateAction::Ignore
        );
        assert_eq!(state.phase, Phase::GotVoiceStateUpdate);
        assert!(state.joined);

        assert_eq!(
            Flow::voice_state_update(&mut state, Some(3), Some(3)),
            VoiceStateAction::Ignore
        );
        assert_eq!(state.phase, Phase::GotVoiceStateUpdate, "no second move");
    }

    #[test]
    fn voice_state_update_reports_a_channel_move() {
        let mut state = state();
        // While connecting, and while connected, a different channel is followed.
        for phase in [Phase::SetGuildVoiceState, Phase::Connected] {
            set(&mut state, phase);
            assert_eq!(
                Flow::voice_state_update(&mut state, Some(99), Some(3)),
                VoiceStateAction::UpdateChannel,
                "{phase:?}"
            );
        }

        // Mid-handshake the flow has to run again on the new channel.
        for phase in [
            Phase::GotVoiceStateUpdate,
            Phase::GotBothVoiceUpdates,
            Phase::WebsocketConnected,
            Phase::GotWebsocketReady,
            Phase::GotIpDiscovery,
        ] {
            set(&mut state, phase);
            assert_eq!(
                Flow::voice_state_update(&mut state, Some(99), Some(3)),
                VoiceStateAction::MoveDuringHandshake,
                "{phase:?}"
            );
            assert_eq!(
                Flow::voice_state_update(&mut state, Some(3), Some(3)),
                VoiceStateAction::Ignore,
                "{phase:?} must ignore an event about the channel we are in"
            );
        }
    }

    #[test]
    fn a_disconnect_we_did_not_ask_for_is_followed() {
        let mut state = state();
        set(&mut state, Phase::Connected);
        state.joined = true;

        assert_eq!(
            Flow::voice_state_update(&mut state, None, Some(3)),
            VoiceStateAction::Disconnect
        );
        assert!(!state.joined, "we are out of the channel now");
    }

    #[test]
    fn an_expected_disconnect_is_not_followed_twice() {
        let mut state = state();
        set(&mut state, Phase::Disconnected);
        state.joined = true;
        state.expecting_disconnect = true;

        assert_eq!(
            Flow::voice_state_update(&mut state, None, None),
            VoiceStateAction::Ignore
        );
        assert!(!state.expecting_disconnect, "the confirmation was consumed");
    }

    #[test]
    fn a_voice_state_update_while_disconnected_only_joins() {
        let mut state = state();
        assert_eq!(
            Flow::voice_state_update(&mut state, Some(3), Some(3)),
            VoiceStateAction::Ignore
        );
        assert_eq!(state.phase, Phase::Disconnected);
        assert!(state.joined);
    }

    // ---- VOICE_SERVER_UPDATE ----

    #[test]
    fn voice_server_update_walks_the_phase_ladder() {
        let mut state = state();
        set(&mut state, Phase::SetGuildVoiceState);
        assert_eq!(
            Flow::voice_server_update(&mut state, true, true, false),
            VoiceServerAction::Ignore
        );
        assert_eq!(state.phase, Phase::GotVoiceServerUpdate);

        set(&mut state, Phase::GotVoiceStateUpdate);
        assert_eq!(
            Flow::voice_server_update(&mut state, true, true, false),
            VoiceServerAction::Ignore
        );
        assert_eq!(state.phase, Phase::GotBothVoiceUpdates);
    }

    #[test]
    fn voice_server_update_without_an_endpoint_is_ignored() {
        let mut state = state();
        set(&mut state, Phase::SetGuildVoiceState);
        assert_eq!(
            Flow::voice_server_update(&mut state, false, true, true),
            VoiceServerAction::Ignore
        );
        assert_eq!(state.phase, Phase::SetGuildVoiceState);
        assert_eq!(
            Flow::voice_server_update(&mut state, true, false, true),
            VoiceServerAction::Ignore
        );
    }

    #[test]
    fn a_new_endpoint_while_connected_replaces_the_socket() {
        let mut state = state();
        set(&mut state, Phase::Connected);
        assert_eq!(
            Flow::voice_server_update(&mut state, true, true, true),
            VoiceServerAction::CloseOldWebsocket
        );
        assert_eq!(state.phase, Phase::GotVoiceServerUpdate);
    }

    #[test]
    fn a_repeated_endpoint_mid_flow_is_ignored() {
        let mut state = state();
        for phase in [
            Phase::GotVoiceServerUpdate,
            Phase::GotBothVoiceUpdates,
            Phase::WebsocketConnected,
            Phase::GotIpDiscovery,
        ] {
            set(&mut state, phase);
            assert_eq!(
                Flow::voice_server_update(&mut state, true, true, false),
                VoiceServerAction::Ignore,
                "{phase:?}"
            );
            assert_eq!(
                Flow::voice_server_update(&mut state, true, true, true),
                VoiceServerAction::ReconnectFlow,
                "{phase:?}"
            );
            assert_eq!(state.phase, Phase::GotVoiceServerUpdate);
        }
    }

    // ---- close codes ----

    #[test]
    fn close_code_1000_disconnects_unless_we_closed_it() {
        let mut state = state();
        assert_eq!(
            Flow::close_action(&state, 1000, false, true),
            CloseAction::Normal
        );

        state.expecting_disconnect = true;
        assert_eq!(
            Flow::close_action(&state, 1000, false, true),
            CloseAction::Quiet
        );
    }

    #[test]
    fn a_forced_disconnect_depends_on_the_gateway_confirmation() {
        let state = state();
        for code in [4014u16, 4022] {
            assert_eq!(
                Flow::close_action(&state, code, true, true),
                CloseAction::ByDiscord,
                "{code}"
            );
            assert_eq!(
                Flow::close_action(&state, code, false, true),
                CloseAction::PotentialReconnect,
                "{code}"
            );
        }
    }

    #[test]
    fn rate_limits_and_crashes_have_their_own_actions() {
        let state = state();
        assert_eq!(
            Flow::close_action(&state, 4021, false, true),
            CloseAction::RateLimited
        );
        assert_eq!(
            Flow::close_action(&state, 4015, false, true),
            CloseAction::Resume
        );
    }

    #[test]
    fn an_unknown_close_code_depends_on_the_reconnect_flag() {
        let state = state();
        assert_eq!(
            Flow::close_action(&state, 4000, false, true),
            CloseAction::Reconnect
        );
        assert_eq!(
            Flow::close_action(&state, 4000, false, false),
            CloseAction::Raise
        );
    }

    // ---- attempts, backoff, joins ----

    #[test]
    fn the_retry_spacing_is_discord_pys() {
        assert_eq!(MAX_ATTEMPTS, 5);
        let waits: Vec<f64> = (0..MAX_ATTEMPTS).map(Flow::retry_wait).collect();
        assert_eq!(waits, vec![1.0, 3.0, 5.0, 7.0, 9.0]);
    }

    #[test]
    fn an_attempt_joins_and_starts_the_flow_only_from_disconnected() {
        let mut state = state();
        Flow::note_attempt(&mut state);
        assert_eq!(state.phase, Phase::SetGuildVoiceState);
        assert!(state.joined);

        // Setting the phase unnecessarily breaks reconnecting.
        set(&mut state, Phase::Connected);
        Flow::note_attempt(&mut state);
        assert_eq!(state.phase, Phase::Connected);
    }

    #[test]
    fn the_backoff_grows_then_resets() {
        let mut backoff = Backoff::default();
        // The first delay is uniform in [0, 2), the second in [0, 4), and so on.
        for exponent in 1..=4 {
            let delay = backoff.delay();
            let limit = 2f64.powi(exponent);
            assert!((0.0..limit).contains(&delay), "{delay} not in [0, {limit})");
        }

        // The exponent is capped at 10: the delay stays inside [0, 2**10).
        for _ in 0..20 {
            assert!(backoff.delay() < 2f64.powi(10));
        }

        backoff.reset();
        assert!(backoff.delay() < 2.0);
    }

    // ---- the Python object ----

    #[test]
    fn the_action_numbers_are_distinct() {
        let close = [
            CloseAction::Normal,
            CloseAction::Quiet,
            CloseAction::ByDiscord,
            CloseAction::PotentialReconnect,
            CloseAction::RateLimited,
            CloseAction::Resume,
            CloseAction::Reconnect,
            CloseAction::Raise,
        ];
        for (index, action) in close.iter().enumerate() {
            assert_eq!(*action as usize, index);
        }

        assert_eq!(VoiceStateAction::Ignore as u8, 0);
        assert_eq!(VoiceServerAction::Ignore as u8, 0);
    }
}
