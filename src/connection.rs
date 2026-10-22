//! The Python-facing native connection object.
//!
//! `VoiceConnection` owns the transport hub and exposes everything `discord.py`'s
//! `VoiceClient` needs from a backend: the connection lifecycle, the RTP encryption path, the
//! Opus encoder and the DAVE session.

use std::borrow::Cow;
use std::sync::Arc;
use std::time::Duration;

use parking_lot::Mutex;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyTuple};

use crate::crypto::Mode;
use crate::dave::Outgoing;
use crate::enums;
use crate::errors::{
    AlreadyConnected, ConnectionClosedError, DaveError, EncryptionError, HandshakeError,
    NotConnected, OpusError,
};
use crate::flow::{Backoff, Flow};
use crate::opus::{EncoderConfig, OpusEncoder, SAMPLES_PER_FRAME};
use crate::transport::{self, Hub, Phase, Session, UdpSend, WsCommand};
use crate::{log_debug, log_info};

/// A Discord voice connection implemented natively.
///
/// This mirrors the state that `discord.voice_state.VoiceConnectionState` plus
/// `discord.gateway.DiscordVoiceWebSocket` keep in pure Python, while the transport, the
/// Opus codec, the RTP encryption and the DAVE/MLS session all live in Rust.
#[pyclass(module = "discord_ext_voice._native", name = "VoiceConnection")]
pub struct VoiceConnection {
    hub: Arc<Hub>,
    session: Arc<Mutex<Option<Session>>>,
    /// The reconnect backoff of the flow, which outlives a single poller run but restarts when
    /// one does.
    backoff: Arc<Mutex<Backoff>>,
}

#[pymethods]
impl VoiceConnection {
    #[new]
    fn new(guild_id: u64, user_id: u64, channel_id: u64) -> Self {
        Self {
            hub: Hub::new(guild_id, user_id, channel_id),
            session: Arc::new(Mutex::new(None)),
            backoff: Arc::new(Mutex::new(Backoff::default())),
        }
    }

    // ----------------------------------------------------------------- identifiers

    #[getter]
    fn guild_id(&self) -> u64 {
        self.hub.state.lock().guild_id
    }

    #[getter]
    fn user_id(&self) -> u64 {
        self.hub.state.lock().user_id
    }

    #[getter]
    fn channel_id(&self) -> u64 {
        self.hub.state.lock().channel_id
    }

    #[setter]
    fn set_channel_id(&self, channel_id: u64) {
        let mut state = self.hub.state.lock();
        state.channel_id = channel_id;
        state.dave.set_channel_id(channel_id);
    }

    // ----------------------------------------------------------------- handshake inputs

    /// Store the `session_id` from a `VOICE_STATE_UPDATE` payload.
    fn set_session_id(&self, session_id: Option<String>) {
        {
            let mut state = self.hub.state.lock();
            state.session_id = session_id;
        }
        self.hub.bump();
    }

    /// Store the `token`/`endpoint`/`guild_id` from a `VOICE_SERVER_UPDATE` payload.
    ///
    /// Returns whether anything changed, which is what the flow uses to tell a repeated update
    /// from a move to a different voice server.
    fn set_server_info(
        &self,
        server_id: u64,
        token: Option<String>,
        endpoint: Option<String>,
    ) -> bool {
        let endpoint = endpoint.map(|endpoint| {
            // `wss://` is implicit, but a `ws://` prefix is meaningful: it asks for a plaintext
            // websocket (used by locally hosted gateways and the test suite).
            endpoint
                .strip_prefix("wss://")
                .unwrap_or(&endpoint)
                .to_owned()
        });

        let changed = {
            let mut state = self.hub.state.lock();
            let changed = state.server_id != Some(server_id)
                || state.token != token
                || state.endpoint != endpoint;
            state.server_id = Some(server_id);
            state.token = token;
            state.endpoint = endpoint;
            changed
        };
        self.hub.bump();
        changed
    }

    // ----------------------------------------------------------------- connection state

    #[getter]
    fn phase(&self) -> u8 {
        self.hub.state.lock().phase as u8
    }

    fn set_phase(&self, phase: u8) -> PyResult<u8> {
        crate::flow::set_phase(&self.hub, phase).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "{phase} is not a valid connection flow state"
            ))
        })
    }

    fn is_connected(&self) -> bool {
        self.hub.state.lock().phase.is_connected()
    }

    // ----------------------------------------------------------------- flow state

    /// The flow phase as a [`ConnectionFlowState`](crate::enums) member.
    #[getter]
    fn state(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        enums::member(py, &enums::FLOW_STATE, self.hub.state.lock().phase.as_u8())
    }

    #[setter]
    fn set_state(&self, value: u8) -> PyResult<()> {
        self.set_phase(value).map(|_| ())
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

    /// Whether the gateway has confirmed that we are out of the channel.
    #[getter]
    fn disconnected(&self) -> bool {
        self.hub.state.lock().disconnected
    }

    #[setter]
    fn set_disconnected(&self, disconnected: bool) {
        self.hub.state.lock().disconnected = disconnected;
    }

    /// The RTP encryption modes this build supports, most preferred first.
    #[getter]
    fn supported_modes<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        PyTuple::new(py, supported_modes())
    }

    /// The DAVE protocol version this build advertises.
    #[getter]
    fn max_dave_protocol_version(&self) -> u16 {
        self.hub.state.lock().max_dave_protocol_version
    }

    /// The native DAVE session, when one exists (`None` while E2EE is off).
    #[getter]
    fn dave_session(slf: PyRef<'_, Self>, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        if !slf.hub.state.lock().dave.has_session() {
            return Ok(None);
        }
        Ok(Some(slf.into_pyobject(py)?.into_any().unbind()))
    }

    /// This connection, which is also the connection state.
    #[getter]
    fn native(slf: PyRef<'_, Self>, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(slf.into_pyobject(py)?.into_any().unbind())
    }

    /// The `session_id` from the last `VOICE_STATE_UPDATE`.
    #[getter]
    fn session_id(&self) -> Option<String> {
        self.hub.state.lock().session_id.clone()
    }

    /// The voice endpoint (with the `wss://` scheme stripped), once known.
    #[getter]
    fn endpoint(&self) -> Option<String> {
        self.hub.state.lock().endpoint.clone()
    }

    // ----------------------------------------------------------------- flow decisions

    /// Handle a `VOICE_STATE_UPDATE` for our own user.
    fn voice_state_update(
        &self,
        py: Python<'_>,
        channel_id: Option<u64>,
        current_channel_id: Option<u64>,
    ) -> PyResult<Py<PyAny>> {
        let action = {
            let mut state = self.hub.state.lock();
            Flow::voice_state_update(&mut state, channel_id, current_channel_id)
        };
        self.hub.bump();
        enums::member(py, &enums::VOICE_STATE_ACTION, action as u8)
    }

    /// Handle a `VOICE_SERVER_UPDATE`.
    ///
    /// `changed` is whether the credentials differ from the ones already stored, which is what
    /// tells a repeat apart from a genuine move.
    fn voice_server_update(
        &self,
        py: Python<'_>,
        has_endpoint: bool,
        has_token: bool,
        changed: bool,
    ) -> PyResult<Py<PyAny>> {
        let action = {
            let mut state = self.hub.state.lock();
            Flow::voice_server_update(&mut state, has_endpoint, has_token, changed)
        };
        self.hub.bump();
        enums::member(py, &enums::VOICE_SERVER_ACTION, action as u8)
    }

    /// Record that a join request went out.
    fn note_attempt(&self) {
        {
            let mut state = self.hub.state.lock();
            Flow::note_attempt(&mut state);
        }
        self.hub.bump();
    }

    /// What the websocket poller does with a close code.
    fn close_action(&self, py: Python<'_>, code: u16, reconnect: bool) -> PyResult<Py<PyAny>> {
        let action = {
            let state = self.hub.state.lock();
            Flow::close_action(&state, code, reconnect)
        };
        enums::member(py, &enums::CLOSE_ACTION, action as u8)
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

    // ----------------------------------------------------------------- waiting

    /// Wait until the flow reaches one of `states`, or `timeout` elapses.
    ///
    /// Returns whether the state was reached; the caller raises `asyncio.TimeoutError`, so the
    /// timeout is the caller's to report.
    #[pyo3(signature = (states, timeout=None))]
    fn wait_for_state<'py>(
        &self,
        py: Python<'py>,
        states: Vec<u8>,
        timeout: Option<f64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let mut wanted = Vec::with_capacity(states.len());
        for value in states {
            let Some(phase) = transport::Phase::from_u8(value) else {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "{value} is not a connection flow state (0-8)"
                )));
            };
            wanted.push(phase);
        }

        let hub = Arc::clone(&self.hub);
        let timeout = timeout.map(transport::wait_duration);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            Ok(hub
                .wait_for(timeout, |state| wanted.contains(&state.phase))
                .await)
        })
    }

    /// Block until the flow reaches `connected`, or `timeout` elapses.
    #[pyo3(signature = (timeout=None))]
    fn wait_connected(&self, py: Python<'_>, timeout: Option<f64>) -> bool {
        let hub = Arc::clone(&self.hub);
        let deadline =
            timeout.map(|seconds| std::time::Instant::now() + transport::wait_duration(seconds));
        py.detach(move || {
            loop {
                let connected = hub.state.lock().phase.is_connected();
                if connected
                    || deadline.is_some_and(|deadline| std::time::Instant::now() >= deadline)
                {
                    return connected;
                }
                std::thread::sleep(std::time::Duration::from_millis(2));
            }
        })
    }

    /// Alias of [`wait_connected`](Self::wait_connected).
    #[pyo3(signature = (timeout=None))]
    fn wait(&self, py: Python<'_>, timeout: Option<f64>) -> bool {
        self.wait_connected(py, timeout)
    }

    #[getter]
    fn latency(&self) -> f64 {
        self.hub.state.lock().latency
    }

    #[getter]
    fn average_latency(&self) -> f64 {
        self.hub.state.lock().average_latency()
    }

    /// Packets dropped by the UDP send queue (congestion or a closed socket).
    #[getter]
    fn dropped_packets(&self) -> u64 {
        self.hub
            .dropped_packets
            .load(std::sync::atomic::Ordering::Relaxed)
    }

    #[getter]
    fn seq_ack(&self) -> i32 {
        self.hub.state.lock().seq_ack
    }

    /// The websocket close code, once the socket is gone (`None` while it is alive).
    ///
    /// `VoiceConnectionState` puts this on the `discord.ConnectionClosed` it raises for a
    /// failed handshake, which is how discord.py decides whether to retry (a 4006 or a 4015
    /// is worth another attempt, a 4021 rate limit is not).
    #[getter]
    fn close_code(&self) -> Option<u16> {
        self.hub.state.lock().close_code
    }

    /// The websocket close reason, once the socket is gone.
    #[getter]
    fn close_reason(&self) -> String {
        self.hub.state.lock().close_reason.clone()
    }

    // ----------------------------------------------------------------- negotiated session

    #[getter]
    fn ssrc(&self) -> u32 {
        self.hub.state.lock().ssrc
    }

    #[getter]
    fn ip(&self) -> Option<String> {
        self.hub.state.lock().ip.map(|ip| ip.to_string())
    }

    #[getter]
    fn port(&self) -> u16 {
        self.hub.state.lock().port
    }

    #[getter]
    fn voice_port(&self) -> u16 {
        self.hub.state.lock().voice_port
    }

    #[getter]
    fn mode(&self) -> Option<String> {
        self.hub.state.lock().mode_name.clone()
    }

    /// The RTP secret key as a list of ints, exactly like `discord.py`.
    #[getter]
    fn secret_key(&self) -> Vec<u8> {
        self.hub.state.lock().secret_key.clone()
    }

    // ----------------------------------------------------------------- DAVE

    #[getter]
    fn dave_protocol_version(&self) -> u16 {
        self.hub.state.lock().dave.protocol_version()
    }

    #[getter]
    fn can_encrypt(&self) -> bool {
        self.hub.state.lock().dave.can_encrypt()
    }

    fn has_dave_session(&self) -> bool {
        self.hub.state.lock().dave.has_session()
    }

    #[getter]
    fn voice_privacy_code(&self) -> Option<String> {
        self.hub.state.lock().dave.voice_privacy_code()
    }

    /// (Re)create the DAVE session and send the resulting MLS key package.
    fn reinit_dave_session(&self, py: Python<'_>) -> PyResult<()> {
        let hub = self.hub.clone();
        py.detach(move || {
            let outgoing = transport::with_state(&hub, |state| state.dave.reinit());
            match outgoing {
                Ok(frames) => {
                    for frame in frames {
                        let command = match frame {
                            Outgoing::Json(value) => WsCommand::Json(value),
                            Outgoing::Binary(bytes) => WsCommand::Binary(bytes),
                        };
                        hub.send(command);
                    }
                    Ok(())
                }
                Err(err) => Err(DaveError::new_err(err.to_string())),
            }
        })
    }

    // ----------------------------------------------------------------- websocket control

    /// Serialize and send a JSON frame, returning whether the socket accepted it.
    fn send_json_text(&self, text: &str) -> PyResult<bool> {
        let value: serde_json::Value = serde_json::from_str(text).map_err(|err| {
            pyo3::exceptions::PyValueError::new_err(format!("invalid JSON payload: {err}"))
        })?;
        Ok(self.hub.send(WsCommand::Json(value)))
    }

    /// Send a binary frame as `[opcode][payload]`.
    fn send_binary_op(&self, opcode: u8, data: &[u8]) -> bool {
        let mut frame = Vec::with_capacity(data.len() + 1);
        frame.push(opcode);
        frame.extend_from_slice(data);
        self.hub.send(WsCommand::Binary(frame))
    }

    /// Send a raw UDP packet. Mostly useful for tests and future receive-side work.
    fn send_packet(&self, packet: &[u8]) -> PyResult<()> {
        match self.hub.queue_udp(packet.to_vec()) {
            UdpSend::Sent | UdpSend::Full => Ok(()),
            UdpSend::Closed => Err(NotConnected::new_err(
                "the voice UDP socket is not connected",
            )),
        }
    }

    // ----------------------------------------------------------------- handshake

    /// Connect the voice websocket, complete the handshake and wait until audio can be sent.
    #[pyo3(signature = (resume=false, timeout=30.0))]
    fn handshake<'py>(
        &self,
        py: Python<'py>,
        resume: bool,
        timeout: f64,
    ) -> PyResult<Bound<'py, PyAny>> {
        let hub = self.hub.clone();
        let session = self.session.clone();

        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let endpoint = hub.state.lock().endpoint.clone();
            let Some(endpoint) = endpoint else {
                return Err(HandshakeError::new_err(
                    "no voice endpoint is known yet; VOICE_SERVER_UPDATE has not been handled",
                ));
            };

            {
                let mut state = hub.state.lock();
                if state.phase.is_connected() && !resume {
                    return Err(AlreadyConnected::new_err(
                        "the voice connection is already established",
                    ));
                }
                state.closed = false;
                state.close_code = None;
                state.close_reason.clear();
                state.phase = Phase::WebsocketConnected;
            }
            hub.bump();

            log_info!("opening the voice websocket to {endpoint} (resume={resume})");
            let stream = transport::connect_websocket(&endpoint)
                .await
                .map_err(|err| HandshakeError::new_err(err.to_string()))?;
            let new_session = transport::spawn_session(&hub, stream, resume)
                .map_err(|err| HandshakeError::new_err(err.to_string()))?;

            let previous = session.lock().replace(new_session);
            if let Some(previous) = previous {
                previous
                    .shutdown(Some(1000), "replaced by a new session".to_owned())
                    .await;
            }

            let timeout = Duration::from_secs_f64(timeout.max(0.1));
            hub.wait_for(Some(timeout), |state| {
                state.phase.is_connected() || state.closed
            })
            .await;

            let (connected, closed, reason, code) = {
                let state = hub.state.lock();
                (
                    state.phase.is_connected(),
                    state.closed,
                    state.close_reason.clone(),
                    state.close_code,
                )
            };

            if connected {
                return Ok(());
            }

            let taken = session.lock().take();
            if let Some(session) = taken {
                session.shutdown(None, "handshake failed".to_owned()).await;
            }

            if closed {
                // `finish` is a no-op here: the reader already recorded this close, and
                // passing `None` keeps its code and reason intact.
                hub.finish(None, reason.clone());
                Err(ConnectionClosedError::new_err(match code {
                    Some(code) => format!(
                        "the voice websocket closed during the handshake: {reason} \
                         (close code {code})"
                    ),
                    None => format!("the voice websocket closed during the handshake: {reason}"),
                }))
            } else {
                hub.finish(None, "handshake timed out".to_owned());
                Err(HandshakeError::new_err(
                    "timed out waiting for the voice session description",
                ))
            }
        })
    }

    /// Close the websocket and mark the connection as finished.
    #[pyo3(signature = (code=None))]
    fn close<'py>(&self, py: Python<'py>, code: Option<u16>) -> PyResult<Bound<'py, PyAny>> {
        self.close_future(py, code)
    }

    /// Await the websocket close and return `(code, reason)`.
    fn wait_closed<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let hub = self.hub.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            hub.wait_for(None, |state| state.closed).await;
            let state = hub.state.lock();
            Ok((state.close_code.unwrap_or(0u16), state.close_reason.clone()))
        })
    }

    // ----------------------------------------------------------------- audio

    /// Build, encrypt and send one RTP audio packet.
    fn send_audio_packet(&self, data: &[u8], encode: bool) -> PyResult<()> {
        send_audio_frame(&self.hub, data, encode).map_err(SendError::into_pyerr)
    }

    /// Configure the encoder `play` uses for PCM sources, and that `send_audio_packet` shares.
    ///
    /// These are `discord.py`'s `play` parameters: they are validated here, so a bad one raises
    /// `ValueError` before playback starts.
    #[pyo3(signature = (application="audio", bitrate=128, fec=true, expected_packet_loss=0.15, bandwidth="full", signal_type="auto"))]
    fn set_encoder(
        &self,
        application: &str,
        bitrate: u16,
        fec: bool,
        expected_packet_loss: f64,
        bandwidth: &str,
        signal_type: &str,
    ) -> PyResult<()> {
        let config = crate::opus::EncoderConfig {
            application: application.to_owned(),
            bitrate,
            fec,
            expected_packet_loss,
            bandwidth: bandwidth.to_owned(),
            signal_type: signal_type.to_owned(),
        };
        let encoder = crate::opus::OpusEncoder::new(&config)
            .map_err(pyo3::exceptions::PyValueError::new_err)?;
        *self.hub.encoder.lock() = Some(encoder);
        Ok(())
    }

    /// Drop the configured encoder: Opus-passthrough sources need none, and the next PCM frame
    /// falls back to the defaults.
    fn clear_encoder(&self) {
        *self.hub.encoder.lock() = None;
    }

    /// Decrypt a received RTP payload (for receive-side extensions and tests).
    fn decrypt_rtp(&self, header: &[u8], payload: &[u8]) -> PyResult<Vec<u8>> {
        let state = self.hub.state.lock();
        let encryptor = state
            .encryptor
            .as_ref()
            .ok_or_else(|| NotConnected::new_err("no voice encryption key has been negotiated"))?;
        encryptor
            .decrypt(header, payload)
            .map_err(|err| EncryptionError::new_err(err.to_string()))
    }

    fn __repr__(&self) -> String {
        let state = self.hub.state.lock();
        format!(
            "<discord_ext_voice.VoiceConnection guild={} channel={} phase={:?} mode={:?} dave={}>",
            state.guild_id,
            state.channel_id,
            state.phase,
            state.mode_name,
            state.dave.protocol_version()
        )
    }
}

impl VoiceConnection {
    /// The transport hub, shared with the playback thread so that it can send frames and see
    /// the connection state without going through Python.
    pub fn hub(&self) -> Arc<Hub> {
        Arc::clone(&self.hub)
    }

    /// Close the websocket, for [`VoiceConnection::close`] and the websocket facade.
    pub(crate) fn close_future<'py>(
        &self,
        py: Python<'py>,
        code: Option<u16>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let hub = Arc::clone(&self.hub);
        let session = Arc::clone(&self.session);

        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let taken = session.lock().take();
            if let Some(session) = taken {
                session.shutdown(code, String::new()).await;
            }
            hub.finish(code, String::new());
            Ok(())
        })
    }

    /// The heartbeat round trip, for the websocket facade.
    pub(crate) fn current_latency(&self) -> f64 {
        self.hub.state.lock().latency
    }

    /// The websocket close code, for the websocket facade.
    pub(crate) fn close_code_raw(&self) -> Option<u16> {
        self.hub.state.lock().close_code
    }
}

/// The encryption modes this build supports, most preferred first.
pub fn supported_modes() -> Vec<&'static str> {
    Mode::supported().iter().map(|mode| mode.as_str()).collect()
}

/// Why a frame could not be sent, in the form both callers need: a Python exception for
/// `VoiceConnection.send_audio_packet`, a message for the playback thread's `after` callback.
pub enum SendError {
    /// No encryption key, or no UDP socket: the handshake has not got that far, or is over.
    NotConnected(String),
    /// The Opus encoder rejected the frame.
    Opus(String),
    /// RTP encryption failed.
    Encryption(String),
}

impl SendError {
    pub fn into_pyerr(self) -> PyErr {
        match self {
            Self::NotConnected(message) => NotConnected::new_err(message),
            Self::Opus(message) => OpusError::new_err(message),
            Self::Encryption(message) => EncryptionError::new_err(message),
        }
    }

    fn message(&self) -> &str {
        match self {
            Self::NotConnected(message) | Self::Opus(message) | Self::Encryption(message) => {
                message
            }
        }
    }
}

impl std::fmt::Display for SendError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.message())
    }
}

/// Encode (when asked), DAVE-encrypt, RTP-encrypt and transmit one audio frame.
///
/// Callable from any thread: the playback thread uses it without holding the GIL, and
/// `VoiceConnection.send_audio_packet` uses it for a frame a caller sends by hand. The Opus
/// encode happens *before* the connection lock is taken, so no Python call can run while the
/// state is locked (see `crate::logging` for why that ordering matters).
pub fn send_audio_frame(hub: &Arc<Hub>, data: &[u8], encode: bool) -> Result<(), SendError> {
    // The encoder stays locked until the packet has been encrypted: its output buffer is reused
    // for every frame, so `payload` borrows it instead of copying it into a fresh `Vec`.
    let mut encoder_slot = encode.then(|| hub.encoder.lock());
    let encoded: Option<&[u8]> = match encoder_slot.as_mut() {
        Some(slot) => {
            let encoder = slot.get_or_insert_with(|| {
                OpusEncoder::new(&EncoderConfig::default())
                    .expect("the default Opus encoder must build")
            });
            Some(encoder.encode_pcm_ref(data).map_err(SendError::Opus)?)
        }
        None => None,
    };
    let payload: &[u8] = encoded.unwrap_or(data);

    // A DAVE failure is rare and worth a warning, but the per-frame trace is not worth an
    // allocation (or a GIL round trip) on the audio path. Records are emitted after the lock is
    // released: logging needs the GIL, and a Python thread that holds the GIL may be waiting for
    // this lock.
    let mut dave_error: Option<String> = None;
    let packet = {
        let mut state = hub.state.lock();

        state.sequence =
            crate::transport::VoiceState::checked_add(u64::from(state.sequence), 1, 65_535) as u16;

        let dave_active = state.dave.can_encrypt();
        let payload: Cow<'_, [u8]> = if dave_active {
            match state.dave.encrypt_opus(payload) {
                Ok(Some(encrypted)) => Cow::Owned(encrypted),
                Ok(None) => Cow::Borrowed(payload),
                Err(err) => {
                    dave_error = Some(format!("DAVE encryption failed, sending plaintext: {err}"));
                    Cow::Borrowed(payload)
                }
            }
        } else {
            Cow::Borrowed(payload)
        };

        let mut header = [0u8; 12];
        header[0] = 0x80;
        header[1] = 0x78;
        header[2..4].copy_from_slice(&state.sequence.to_be_bytes());
        header[4..8].copy_from_slice(&state.timestamp.to_be_bytes());
        header[8..12].copy_from_slice(&state.ssrc.to_be_bytes());

        let ssrc = state.ssrc;
        let encryptor = state.encryptor.as_mut().ok_or_else(|| {
            SendError::NotConnected(format!(
                "the voice encryption key is not available yet (ssrc={ssrc})"
            ))
        })?;
        let packet = encryptor
            .encrypt(&header, payload.as_ref())
            .map_err(|err| SendError::Encryption(err.to_string()))?;

        state.timestamp = crate::transport::VoiceState::checked_add(
            u64::from(state.timestamp),
            SAMPLES_PER_FRAME as u64,
            u64::from(u32::MAX),
        ) as u32;

        packet
    };

    if let Some(message) = dave_error {
        crate::log_warning!("{message}");
    }

    match hub.queue_udp(packet) {
        UdpSend::Sent => Ok(()),
        UdpSend::Full => {
            log_debug!("a voice packet has been dropped (the send queue is full)");
            Ok(())
        }
        UdpSend::Closed => Err(SendError::NotConnected(
            "the voice UDP socket is not connected".to_owned(),
        )),
    }
}
