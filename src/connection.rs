//! The Python-facing native connection object.
//!
//! `VoiceConnection` owns the transport hub and exposes everything `discord.py`'s
//! `VoiceClient` needs from a backend: the connection lifecycle, the RTP encryption path, the
//! Opus encoder and the DAVE session.

use std::io;
use std::sync::Arc;
use std::time::Duration;

use parking_lot::Mutex;
use pyo3::prelude::*;
use tokio::net::UdpSocket;

use crate::crypto::Mode;
use crate::dave::Outgoing;
use crate::errors::{
    AlreadyConnected, ConnectionClosedError, DaveError, EncryptionError, HandshakeError,
    NotConnected, OpusError,
};
use crate::flow::{Backoff, PyConnectionFlow};
use crate::opus::{EncoderConfig, OpusEncoder, SAMPLES_PER_FRAME};
use crate::transport::{self, Hub, Phase, Session, WsCommand};
use crate::{log_debug, log_info};

/// Send a UDP datagram without ever blocking the GIL.
fn send_datagram(socket: &Arc<UdpSocket>, packet: Vec<u8>) {
    match socket.try_send(&packet) {
        Ok(_) => {}
        Err(err) if err.kind() == io::ErrorKind::WouldBlock => {
            let socket = socket.clone();
            pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
                if let Err(err) = socket.send(&packet).await {
                    log_debug!("a voice packet has been dropped: {err}");
                }
            });
        }
        Err(err) => log_debug!("a voice packet has been dropped: {err}"),
    }
}

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
    fn set_server_info(&self, server_id: u64, token: Option<String>, endpoint: Option<String>) {
        {
            let mut state = self.hub.state.lock();
            state.server_id = Some(server_id);
            state.token = token;
            // `wss://` is implicit, but a `ws://` prefix is meaningful: it asks for a
            // plaintext websocket (used by locally hosted gateways and the test suite).
            state.endpoint = endpoint.map(|endpoint| {
                endpoint
                    .strip_prefix("wss://")
                    .unwrap_or(&endpoint)
                    .to_owned()
            });
        }
        self.hub.bump();
    }

    // ----------------------------------------------------------------- connection state

    #[getter]
    fn phase(&self) -> u8 {
        self.hub.state.lock().phase as u8
    }

    fn set_phase(&self, phase: u8) -> PyResult<()> {
        crate::flow::set_phase(&self.hub, phase).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "{phase} is not a valid connection flow state"
            ))
        })?;
        Ok(())
    }

    fn is_connected(&self) -> bool {
        self.hub.state.lock().phase.is_connected()
    }

    /// The connection flow: the decisions the Python `VoiceConnectionState` dispatches on.
    #[getter]
    fn flow(&self) -> PyConnectionFlow {
        PyConnectionFlow::new(Arc::clone(&self.hub), Arc::clone(&self.backoff))
    }

    #[getter]
    fn latency(&self) -> f64 {
        self.hub.state.lock().latency
    }

    #[getter]
    fn average_latency(&self) -> f64 {
        self.hub.state.lock().average_latency()
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
        let socket = self.hub.state.lock().udp.clone();
        let Some(socket) = socket else {
            return Err(NotConnected::new_err(
                "the voice UDP socket is not connected",
            ));
        };
        send_datagram(&socket, packet.to_vec());
        Ok(())
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
        let hub = self.hub.clone();
        let session = self.session.clone();

        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let taken = session.lock().take();
            if let Some(session) = taken {
                session.shutdown(code, String::new()).await;
            }
            hub.finish(code, String::new());
            Ok(())
        })
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
    let payload = if encode {
        let samples = crate::opus::bytes_to_samples(data);
        let mut slot = hub.encoder.lock();
        let encoder = slot.get_or_insert_with(|| {
            OpusEncoder::new(&EncoderConfig::default())
                .expect("the default Opus encoder must build")
        });
        encoder.encode_frame(&samples).map_err(SendError::Opus)?
    } else {
        data.to_vec()
    };

    // Records are collected and emitted after the lock is released: logging needs the GIL,
    // and a Python thread that holds the GIL may be waiting for this lock.
    let mut pending: Vec<(&'static str, String)> = Vec::new();
    let (packet, socket) = {
        let mut state = hub.state.lock();

        state.sequence =
            crate::transport::VoiceState::checked_add(u64::from(state.sequence), 1, 65_535) as u16;

        let dave_active = state.dave.can_encrypt();
        let payload = if dave_active {
            match state.dave.encrypt_opus(&payload) {
                Ok(Some(encrypted)) => encrypted,
                Ok(None) => payload,
                Err(err) => {
                    pending.push((
                        "warning",
                        format!("DAVE encryption failed, sending plaintext: {err}"),
                    ));
                    payload
                }
            }
        } else {
            payload
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
            .encrypt(&header, &payload)
            .map_err(|err| SendError::Encryption(err.to_string()))?;

        pending.push((
            "debug",
            format!(
                "sending RTP: seq={} ssrc={} {} bytes (opus={}, dave={})",
                state.sequence,
                ssrc,
                packet.len(),
                if encode { "encoded" } else { "passthrough" },
                if dave_active { "on" } else { "off" }
            ),
        ));

        state.timestamp = crate::transport::VoiceState::checked_add(
            u64::from(state.timestamp),
            SAMPLES_PER_FRAME as u64,
            u64::from(u32::MAX),
        ) as u32;

        (packet, state.udp.clone())
    };

    for (level, message) in pending {
        crate::logging::log(level, &message);
    }

    let Some(socket) = socket else {
        return Err(SendError::NotConnected(
            "the voice UDP socket is not connected".to_owned(),
        ));
    };
    send_datagram(&socket, packet);
    Ok(())
}
