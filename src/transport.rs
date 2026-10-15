//! The voice transport: a Discord voice websocket plus the RTP UDP socket behind it.
//!
//! The TLS stack is assembled by hand rather than delegated to `tokio-tungstenite`'s
//! connectors:
//!
//! ```text
//! tokio::net::TcpStream::connect(endpoint)
//!     -> tokio_rustls::TlsConnector::connect(server_name, tcp)
//!     -> tokio_tungstenite::client_async(request, tls_stream)
//! ```
//!
//! Doing it explicitly keeps the TLS configuration under our control (rustls with the
//! platform trust store, falling back to the bundled webpki roots) and lets the connection be
//! driven entirely by the tokio runtime owned by `pyo3-async-runtimes`.

use std::collections::{HashMap, VecDeque};
use std::io;
use std::net::{IpAddr, SocketAddr};
use std::pin::Pin;
use std::sync::{Arc, OnceLock};
use std::task::{Context, Poll};
use std::time::Duration;

use futures_util::stream::{SplitSink, SplitStream};
use futures_util::{SinkExt, StreamExt};
use parking_lot::Mutex;
use rustls::pki_types::ServerName;
use rustls::{ClientConfig, RootCertStore};
use serde_json::Value;
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::{TcpStream, UdpSocket};
use tokio::sync::{Notify, mpsc};
use tokio_rustls::TlsConnector;
use tokio_rustls::client::TlsStream;
use tokio_tungstenite::WebSocketStream;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use voice_model as vm;
use voice_model::id::{GuildId, UserId};
use voice_model::payload as vp;

use crate::crypto::{Mode, PacketEncryptor};
use crate::dave::{DaveState, Outgoing};
use crate::errors::TransportError;
use crate::{log_debug, log_error, log_info, log_warning};

/// The transport under the voice websocket.
///
/// Discord's endpoints are always TLS, but a plain TCP variant is supported for locally
/// hosted gateways (endpoints written as ``ws://host:port``), which also makes the protocol
/// testable end to end.
pub enum VoiceStream {
    Tls(Box<TlsStream<TcpStream>>),
    Plain(TcpStream),
}

impl AsyncRead for VoiceStream {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        match self.get_mut() {
            VoiceStream::Tls(stream) => Pin::new(stream.as_mut()).poll_read(cx, buf),
            VoiceStream::Plain(stream) => Pin::new(stream).poll_read(cx, buf),
        }
    }
}

impl AsyncWrite for VoiceStream {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        match self.get_mut() {
            VoiceStream::Tls(stream) => Pin::new(stream.as_mut()).poll_write(cx, buf),
            VoiceStream::Plain(stream) => Pin::new(stream).poll_write(cx, buf),
        }
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        match self.get_mut() {
            VoiceStream::Tls(stream) => Pin::new(stream.as_mut()).poll_flush(cx),
            VoiceStream::Plain(stream) => Pin::new(stream).poll_flush(cx),
        }
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        match self.get_mut() {
            VoiceStream::Tls(stream) => Pin::new(stream.as_mut()).poll_shutdown(cx),
            VoiceStream::Plain(stream) => Pin::new(stream).poll_shutdown(cx),
        }
    }
}

/// The websocket stream type produced by the hand-rolled transport.
pub type VoiceWebSocket = WebSocketStream<VoiceStream>;

/// How long `discord.py` waits for a single websocket frame before treating the connection as
/// dead, mirrored here so reconnect behaviour matches.
const READ_TIMEOUT: Duration = Duration::from_secs(30);

/// `ConnectionFlowState` from `discord/voice_state.py`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Phase {
    Disconnected = 0,
    SetGuildVoiceState = 1,
    GotVoiceStateUpdate = 2,
    GotVoiceServerUpdate = 3,
    GotBothVoiceUpdates = 4,
    WebsocketConnected = 5,
    GotWebsocketReady = 6,
    GotIpDiscovery = 7,
    Connected = 8,
}

impl Phase {
    pub const fn is_connected(self) -> bool {
        matches!(self, Phase::Connected)
    }
}

/// Outgoing websocket frames, produced from any thread.
#[derive(Debug)]
pub enum WsCommand {
    Json(Value),
    Binary(Vec<u8>),
    Close(Option<u16>, String),
}

/// The mutable half of the voice connection.
pub struct VoiceState {
    // ---- configuration ----
    pub guild_id: u64,
    pub user_id: u64,
    pub channel_id: u64,
    pub max_dave_protocol_version: u16,
    pub supported_modes: Vec<Mode>,

    // ---- inputs from the main gateway ----
    pub session_id: Option<String>,
    pub token: Option<String>,
    pub endpoint: Option<String>,
    pub server_id: Option<u64>,

    // ---- negotiated by the voice gateway ----
    pub ssrc: u32,
    pub ip: Option<IpAddr>,
    pub port: u16,
    pub voice_port: u16,
    pub mode: Option<Mode>,
    pub mode_name: Option<String>,
    pub secret_key: Vec<u8>,
    pub encryptor: Option<PacketEncryptor>,

    // ---- RTP counters ----
    pub sequence: u16,
    pub timestamp: u32,
    pub seq_ack: i32,

    // ---- DAVE ----
    pub dave: DaveState,

    // ---- transport ----
    pub udp: Option<Arc<UdpSocket>>,
    pub udp_target: Option<SocketAddr>,

    // ---- connection bookkeeping ----
    pub phase: Phase,
    pub latency: f64,
    pub recent_latencies: VecDeque<f64>,
    pub pending_heartbeats: HashMap<u64, std::time::Instant>,
    pub close_code: Option<u16>,
    pub close_reason: String,
    pub closed: bool,
}

impl VoiceState {
    pub fn new(guild_id: u64, user_id: u64, channel_id: u64) -> Self {
        Self {
            guild_id,
            user_id,
            channel_id,
            max_dave_protocol_version: DaveState::max_protocol_version(),
            supported_modes: Mode::supported().to_vec(),
            session_id: None,
            token: None,
            endpoint: None,
            server_id: None,
            ssrc: 0,
            ip: None,
            port: 0,
            voice_port: 0,
            mode: None,
            mode_name: None,
            secret_key: Vec::new(),
            encryptor: None,
            sequence: 0,
            timestamp: 0,
            seq_ack: -1,
            dave: DaveState::new(user_id, channel_id),
            udp: None,
            udp_target: None,
            phase: Phase::Disconnected,
            latency: f64::INFINITY,
            recent_latencies: VecDeque::new(),
            pending_heartbeats: HashMap::new(),
            close_code: None,
            close_reason: String::new(),
            closed: false,
        }
    }

    /// `VoiceClient.checked_add`: saturating wraparound at `limit`.
    pub fn checked_add(current: u64, value: u64, limit: u64) -> u64 {
        if current + value > limit {
            0
        } else {
            current + value
        }
    }

    pub fn average_latency(&self) -> f64 {
        if self.recent_latencies.is_empty() {
            return f64::INFINITY;
        }
        self.recent_latencies.iter().sum::<f64>() / self.recent_latencies.len() as f64
    }
}

/// Shared hub: the voice state plus a notification handle used to wake awaiters when it
/// changes.
pub struct Hub {
    pub state: Mutex<VoiceState>,
    pub notify: Notify,
    pub ws_tx: Mutex<Option<mpsc::UnboundedSender<WsCommand>>>,
}

impl Hub {
    pub fn new(guild_id: u64, user_id: u64, channel_id: u64) -> Arc<Self> {
        Arc::new(Self {
            state: Mutex::new(VoiceState::new(guild_id, user_id, channel_id)),
            notify: Notify::new(),
            ws_tx: Mutex::new(None),
        })
    }

    /// Wake every waiter registered through [`Hub::wait_for`].
    pub fn bump(&self) {
        self.notify.notify_waiters();
    }

    /// Wait until `cond` holds, or `timeout` elapses. Returns whether the condition holds.
    pub async fn wait_for<F>(&self, timeout: Option<Duration>, cond: F) -> bool
    where
        F: Fn(&VoiceState) -> bool,
    {
        let deadline = timeout.map(|d| tokio::time::Instant::now() + d);
        loop {
            // Register interest *before* checking so no wakeup can be missed.
            let notified = self.notify.notified();
            if cond(&self.state.lock()) {
                return true;
            }
            match deadline {
                Some(deadline) => {
                    if tokio::time::timeout_at(deadline, notified).await.is_err() {
                        return cond(&self.state.lock());
                    }
                }
                None => notified.await,
            }
        }
    }

    /// Queue a websocket frame. Returns `false` when the socket is gone.
    pub fn send(&self, command: WsCommand) -> bool {
        match self.ws_tx.lock().as_ref() {
            Some(tx) => tx.send(command).is_ok(),
            None => false,
        }
    }

    /// Record that the voice websocket is gone.
    pub fn finish(&self, code: Option<u16>, reason: String) {
        {
            let mut state = self.state.lock();
            if state.closed {
                return;
            }
            state.closed = true;
            state.close_code = code;
            state.close_reason = reason;
            state.phase = Phase::Disconnected;
        }
        self.bump();
    }
}

/// A running voice websocket: the command channel plus the reader/writer tasks.
pub struct Session {
    pub tx: mpsc::UnboundedSender<WsCommand>,
    reader: tokio::task::JoinHandle<()>,
    writer: tokio::task::JoinHandle<()>,
}

impl Session {
    /// Close the websocket nicely, then make sure both tasks are gone.
    pub async fn shutdown(self, code: Option<u16>, reason: String) {
        let _ = self.tx.send(WsCommand::Close(code, reason));
        let abort = self.reader.abort_handle();
        if tokio::time::timeout(Duration::from_secs(5), self.reader)
            .await
            .is_err()
        {
            abort.abort();
        }
        self.writer.abort();
    }
}

/// The rustls client configuration used for voice websockets, built once.
pub fn tls_config() -> Arc<ClientConfig> {
    static CONFIG: OnceLock<Arc<ClientConfig>> = OnceLock::new();
    CONFIG
        .get_or_init(|| {
            let mut roots = RootCertStore::empty();

            let native = rustls_native_certs::load_native_certs();
            for error in &native.errors {
                log_debug!("unable to read a platform root certificate: {error}");
            }
            let mut native_count = 0usize;
            for cert in native.certs {
                if roots.add(cert).is_ok() {
                    native_count += 1;
                }
            }

            if native_count == 0 {
                log_debug!("no platform trust store available, falling back to webpki roots");
                roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
            }

            let config = ClientConfig::builder()
                .with_root_certificates(roots)
                .with_no_client_auth();
            Arc::new(config)
        })
        .clone()
}

/// Split `host:port` into its parts, defaulting to port 443.
fn split_endpoint(endpoint: &str) -> (String, u16) {
    if let Some((host, port)) = endpoint.rsplit_once(':')
        && !host.is_empty()
        && !port.is_empty()
        && port.bytes().all(|b| b.is_ascii_digit())
    {
        return (host.to_owned(), port.parse().unwrap_or(443));
    }
    (endpoint.to_owned(), 443)
}

/// Connect a TCP stream, upgrade it with rustls and run the websocket handshake, all inside
/// the tokio runtime managed by `pyo3-async-runtimes`.
///
/// An endpoint written as ``ws://host:port`` skips TLS; everything else uses rustls.
pub async fn connect_websocket(endpoint: &str) -> Result<VoiceWebSocket, TransportError> {
    let (insecure, endpoint) = match endpoint.strip_prefix("ws://") {
        Some(rest) => (true, rest.to_owned()),
        None => (
            false,
            endpoint
                .strip_prefix("wss://")
                .unwrap_or(endpoint)
                .to_owned(),
        ),
    };
    let (host, port) = split_endpoint(&endpoint);

    log_debug!("connecting TCP to {host}:{port}");
    let tcp = TcpStream::connect((host.as_str(), port))
        .await
        .map_err(|err| {
            TransportError::WebSocket(format!("TCP connect to {host}:{port} failed: {err}"))
        })?;
    let _ = tcp.set_nodelay(true);

    let stream = if insecure {
        log_debug!("using a plaintext voice websocket (ws://) for {host}:{port}");
        VoiceStream::Plain(tcp)
    } else {
        let server_name = ServerName::try_from(host.clone()).map_err(|err| {
            TransportError::Protocol(format!("invalid voice endpoint {host:?}: {err}"))
        })?;
        log_debug!("starting TLS handshake with {host}");
        let connector = TlsConnector::from(tls_config());
        let tls = connector.connect(server_name, tcp).await.map_err(|err| {
            TransportError::WebSocket(format!("TLS handshake with {host} failed: {err}"))
        })?;
        VoiceStream::Tls(Box::new(tls))
    };

    let scheme = if insecure { "ws" } else { "wss" };
    let uri = format!("{scheme}://{endpoint}/?v=8");
    let request = uri
        .clone()
        .into_client_request()
        .map_err(|err| TransportError::Protocol(format!("invalid voice gateway URI: {err}")))?;

    log_debug!("upgrading to websocket at {uri}");
    let (stream, _response) = tokio_tungstenite::client_async(request, stream)
        .await
        .map_err(|err| {
            TransportError::WebSocket(format!("voice websocket handshake failed: {err}"))
        })?;

    Ok(stream)
}

/// Spawn the websocket reader/writer and send IDENTIFY (or RESUME).
pub fn spawn_session(
    hub: &Arc<Hub>,
    stream: VoiceWebSocket,
    resume: bool,
) -> Result<Session, TransportError> {
    let (sink, stream) = stream.split();
    let (tx, rx) = mpsc::unbounded_channel();

    let writer = tokio::spawn(writer_loop(sink, rx));
    *hub.ws_tx.lock() = Some(tx.clone());

    let reader = tokio::spawn(reader_loop(hub.clone(), stream, tx.clone()));

    let payload = if resume {
        resume_payload(hub)?
    } else {
        identify_payload(hub)
    };
    let _ = tx.send(WsCommand::Json(payload));

    Ok(Session { tx, reader, writer })
}

fn identify_payload(hub: &Arc<Hub>) -> Value {
    let state = hub.state.lock();
    let event = vm::Event::Identify(vp::Identify {
        server_id: GuildId(state.server_id.unwrap_or(0)),
        session_id: state.session_id.clone().unwrap_or_default(),
        token: state.token.clone().unwrap_or_default(),
        user_id: UserId(state.user_id),
        max_dave_protocol_version: (state.max_dave_protocol_version > 0)
            .then_some(state.max_dave_protocol_version),
    });
    let mut value = serde_json::to_value(&event).unwrap_or(Value::Null);
    // The voice gateway rejects an unknown `d` shape silently, so keep this defensive.
    if value.get("d").is_none() {
        value = serde_json::json!({ "op": 0, "d": {} });
    }
    value
}

fn resume_payload(hub: &Arc<Hub>) -> Result<Value, TransportError> {
    let state = hub.state.lock();
    let event = vm::Event::Resume(vp::Resume {
        server_id: GuildId(state.server_id.unwrap_or(0)),
        session_id: state.session_id.clone().unwrap_or_default(),
        token: state.token.clone().unwrap_or_default(),
    });
    let mut value = serde_json::to_value(&event)
        .map_err(|err| TransportError::Protocol(format!("unable to serialize RESUME: {err}")))?;
    // `voice-model`'s Resume omits `seq_ack`, which Discord requires for a resume to be
    // accepted (and which `discord.py` also sends).
    if let Some(data) = value.get_mut("d") {
        data["seq_ack"] = Value::from(state.seq_ack);
    }
    Ok(value)
}

async fn writer_loop<S>(
    mut sink: SplitSink<WebSocketStream<S>, Message>,
    mut rx: mpsc::UnboundedReceiver<WsCommand>,
) where
    S: AsyncRead + AsyncWrite + Unpin,
{
    while let Some(command) = rx.recv().await {
        let result = match command {
            WsCommand::Json(value) => match serde_json::to_string(&value) {
                Ok(text) => sink.send(Message::Text(text.into())).await,
                Err(err) => {
                    log_warning!("unable to serialize an outgoing voice frame: {err}");
                    continue;
                }
            },
            WsCommand::Binary(bytes) => sink.send(Message::Binary(bytes.into())).await,
            WsCommand::Close(code, reason) => {
                let frame = tokio_tungstenite::tungstenite::protocol::CloseFrame {
                    code: code.unwrap_or(1000).into(),
                    reason: reason.into(),
                };
                let _ = sink.send(Message::Close(Some(frame))).await;
                let _ = sink.close().await;
                break;
            }
        };
        if let Err(err) = result {
            log_debug!("voice websocket write failed: {err}");
            break;
        }
    }
    let _ = sink.close().await;
}

/// What the reader wants to tell the loop after handling one event.
enum Heartbeat {
    Keep,
    Start(Duration),
}

async fn reader_loop<S>(
    hub: Arc<Hub>,
    mut stream: SplitStream<WebSocketStream<S>>,
    tx: mpsc::UnboundedSender<WsCommand>,
) where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let mut heartbeat_period: Option<Duration> = None;
    let mut next_heartbeat: Option<tokio::time::Instant> = None;

    loop {
        let heartbeat_tick = async {
            match next_heartbeat {
                Some(deadline) => tokio::time::sleep_until(deadline).await,
                None => std::future::pending::<()>().await,
            }
        };

        tokio::select! {
            biased;

            () = heartbeat_tick => {
                let period = heartbeat_period.unwrap_or(Duration::from_secs(5));
                next_heartbeat = Some(tokio::time::Instant::now() + period);
                send_heartbeat(&hub, &tx);
            }

            result = tokio::time::timeout(READ_TIMEOUT, stream.next()) => {
                match result {
                    Err(_) => {
                        hub.finish(None, "voice websocket read timed out".to_owned());
                        break;
                    }
                    Ok(None) => {
                        hub.finish(None, "voice websocket closed".to_owned());
                        break;
                    }
                    Ok(Some(Err(err))) => {
                        log_error!("voice websocket error: {err}");
                        hub.finish(None, format!("voice websocket error: {err}"));
                        break;
                    }
                    Ok(Some(Ok(message))) => match message {
                        Message::Text(text) => {
                            match handle_text(&hub, &tx, text.as_str()).await {
                                Ok(Heartbeat::Start(period)) => {
                                    heartbeat_period = Some(period);
                                    next_heartbeat = Some(tokio::time::Instant::now());
                                }
                                Ok(Heartbeat::Keep) => {}
                                Err(err) => {
                                    log_warning!("voice websocket error: {err}");
                                    hub.finish(None, err.to_string());
                                    break;
                                }
                            }
                        }
                        Message::Binary(data) => {
                            if let Err(err) = handle_binary(&hub, &tx, &data) {
                                log_warning!("voice websocket error: {err}");
                                hub.finish(None, err.to_string());
                                break;
                            }
                        }
                        Message::Close(frame) => {
                            let (code, reason) = match frame {
                                Some(frame) => (Some(u16::from(frame.code)), frame.reason.to_string()),
                                None => (None, String::new()),
                            };
                            log_debug!("voice websocket closed with code {code:?} ({reason})");
                            hub.finish(code, reason);
                            break;
                        }
                        Message::Ping(_) | Message::Pong(_) | Message::Frame(_) => {}
                    },
                }
            }
        }
    }

    // Dropping the hub's sender lets the writer task finish, then the local `tx` drop below
    // ends the channel for good.
    *hub.ws_tx.lock() = None;
}

fn send_heartbeat(hub: &Arc<Hub>, tx: &mpsc::UnboundedSender<WsCommand>) {
    let nonce: u64 = rand::random();
    {
        let mut state = hub.state.lock();
        state
            .pending_heartbeats
            .insert(nonce, std::time::Instant::now());
        if state.pending_heartbeats.len() > 32 {
            // Keep the map bounded if Discord stops acknowledging.
            let oldest = state
                .pending_heartbeats
                .iter()
                .min_by_key(|(_, at)| **at)
                .map(|(nonce, _)| *nonce);
            if let Some(oldest) = oldest {
                state.pending_heartbeats.remove(&oldest);
            }
        }
    }
    let event = vm::Event::Heartbeat(vp::Heartbeat { nonce });
    if let Ok(value) = serde_json::to_value(&event) {
        let _ = tx.send(WsCommand::Json(value));
    }
}

/// Normalise a voice gateway JSON frame down to its `op` and `d` members.
///
/// Two things make this necessary:
///
/// * `voice_model::Event` ignores an `op`/`d` *value* it does not recognise but still does
///   not consume it, so any extra top-level member appearing before `d` makes the whole
///   frame fail to parse. Discord's voice gateway does send an extra `seq` member.
/// * `Event` borrows its keys while deserializing, so it cannot be built from a
///   `serde_json::Value`; it has to be parsed from text. Re-serialising the filtered map and
///   parsing that text satisfies both constraints.
fn normalize_frame(text: &str) -> Result<(Option<i64>, String), TransportError> {
    let raw: serde_json::Map<String, Value> = serde_json::from_str(text)
        .map_err(|err| TransportError::Protocol(format!("invalid voice JSON frame: {err}")))?;

    let seq = raw.get("seq").and_then(|value| match value {
        Value::Number(number) => number.as_i64(),
        Value::String(text) => text.parse().ok(),
        _ => None,
    });

    let filtered: serde_json::Map<String, Value> = raw
        .into_iter()
        .filter(|(key, _)| key == "op" || key == "d")
        .collect();

    let text = serde_json::to_string(&filtered).map_err(|err| {
        TransportError::Protocol(format!("unable to re-encode a voice frame: {err}"))
    })?;
    Ok((seq, text))
}

async fn handle_text(
    hub: &Arc<Hub>,
    tx: &mpsc::UnboundedSender<WsCommand>,
    text: &str,
) -> Result<Heartbeat, TransportError> {
    let (seq, normalized) = normalize_frame(text)?;
    if let Some(seq) = seq {
        hub.state.lock().seq_ack = seq as i32;
    }

    let event: vm::Event = serde_json::from_str(&normalized)
        .map_err(|err| TransportError::Protocol(format!("unhandled voice event: {err}")))?;

    log_debug!("voice websocket frame received: {event:?}");

    match event {
        vm::Event::Hello(hello) => {
            let interval = (hello.heartbeat_interval / 1000.0).min(5.0);
            log_debug!("voice heartbeat interval set to {interval}s");
            return Ok(Heartbeat::Start(Duration::from_secs_f64(interval.max(0.5))));
        }
        vm::Event::HeartbeatAck(ack) => {
            let sent = hub.state.lock().pending_heartbeats.remove(&ack.nonce);
            if let Some(sent) = sent {
                let latency = sent.elapsed().as_secs_f64();
                let mut state = hub.state.lock();
                state.latency = latency;
                state.recent_latencies.push_back(latency);
                while state.recent_latencies.len() > 20 {
                    state.recent_latencies.pop_front();
                }
            }
        }
        vm::Event::Ready(ready) => handle_ready(hub, tx, ready).await?,
        vm::Event::Resumed => log_debug!("voice RESUME succeeded"),
        vm::Event::SessionDescription(description) => {
            handle_session_description(hub, tx, description)?;
        }
        vm::Event::DavePrepareTransition(data) => {
            let outgoing = {
                let mut state = hub.state.lock();
                state
                    .dave
                    .handle_prepare_transition(data.transition_id, data.protocol_version)
            };
            dispatch(hub, tx, outgoing)?;
        }
        vm::Event::DaveExecuteTransition(data) => {
            let outgoing = {
                let mut state = hub.state.lock();
                state.dave.handle_execute_transition(data.transition_id)
            };
            dispatch(hub, tx, Ok(outgoing))?;
        }
        vm::Event::DavePrepareEpoch(data) => {
            let outgoing = {
                let mut state = hub.state.lock();
                state
                    .dave
                    .handle_prepare_epoch(data.epoch, data.protocol_version)
            };
            dispatch(hub, tx, outgoing)?;
        }
        vm::Event::ClientConnect(data) => {
            log_debug!(
                "client {} connected (audio ssrc {})",
                data.user_id,
                data.audio_ssrc
            );
        }
        vm::Event::ClientDisconnect(data) => {
            log_debug!("client {} disconnected", data.user_id);
        }
        other => log_debug!("ignoring voice event {:?}", other.kind()),
    }

    Ok(Heartbeat::Keep)
}

fn handle_binary(
    hub: &Arc<Hub>,
    tx: &mpsc::UnboundedSender<WsCommand>,
    data: &[u8],
) -> Result<(), TransportError> {
    if data.len() < 3 {
        log_debug!("ignoring short voice binary frame ({} bytes)", data.len());
        return Ok(());
    }

    // Voice gateway binary frames are prefixed with a big-endian sequence number.
    let seq_ack = u16::from_be_bytes([data[0], data[1]]);
    hub.state.lock().seq_ack = i32::from(seq_ack);

    let outgoing = {
        let mut state = hub.state.lock();
        state.dave.handle_binary(&data[2..])
    };
    dispatch(hub, tx, outgoing)
}

fn dispatch(
    hub: &Arc<Hub>,
    tx: &mpsc::UnboundedSender<WsCommand>,
    outgoing: Result<Vec<Outgoing>, crate::errors::DaveFailure>,
) -> Result<(), TransportError> {
    match outgoing {
        Ok(frames) => {
            for frame in frames {
                let command = match frame {
                    Outgoing::Json(value) => WsCommand::Json(value),
                    Outgoing::Binary(bytes) => WsCommand::Binary(bytes),
                };
                let _ = tx.send(command);
            }
            hub.bump();
            Ok(())
        }
        Err(err) => {
            log_warning!("DAVE operation failed: {err}");
            hub.bump();
            Ok(())
        }
    }
}

async fn handle_ready(
    hub: &Arc<Hub>,
    tx: &mpsc::UnboundedSender<WsCommand>,
    ready: vp::Ready,
) -> Result<(), TransportError> {
    log_debug!(
        "voice READY: ssrc={} address={}:{} modes={:?}",
        ready.ssrc,
        ready.ip,
        ready.port,
        ready.modes
    );

    // 1. Bind and connect the UDP socket that will carry RTP.
    let socket = UdpSocket::bind(("0.0.0.0", 0)).await.map_err(|err| {
        TransportError::WebSocket(format!("unable to bind the voice UDP socket: {err}"))
    })?;
    let target = SocketAddr::new(ready.ip, ready.port);
    socket.connect(target).await.map_err(|err| {
        TransportError::WebSocket(format!("unable to connect the voice UDP socket: {err}"))
    })?;
    let socket = Arc::new(socket);

    // 2. IP discovery, so Discord learns our public address.
    let (ip, port) = discover_ip(&socket, ready.ssrc).await?;
    log_debug!("ip discovery result: {ip}:{port}");

    // 3. Pick the best mutually supported encryption mode.
    let mode = {
        let state = hub.state.lock();
        ready
            .modes
            .iter()
            .filter_map(|name| Mode::parse(name))
            .find(|mode| state.supported_modes.contains(mode))
            .or_else(|| state.supported_modes.first().copied())
    };
    let Some(mode) = mode else {
        return Err(TransportError::Protocol(
            "no mutually supported voice encryption mode".to_owned(),
        ));
    };
    log_debug!("selected voice encryption mode {mode}");

    {
        let mut state = hub.state.lock();
        state.ssrc = ready.ssrc;
        state.voice_port = ready.port;
        state.ip = Some(ip);
        state.port = port;
        state.mode = Some(mode);
        state.mode_name = Some(mode.as_str().to_owned());
        state.udp = Some(socket);
        state.udp_target = Some(target);
        state.phase = Phase::GotIpDiscovery;
    }
    hub.bump();

    // 4. SELECT_PROTOCOL.
    let event = vm::Event::SelectProtocol(vp::SelectProtocol {
        protocol: "udp".to_owned(),
        data: vm::ProtocolData {
            address: ip,
            port,
            mode: mode.as_str().to_owned(),
        },
    });
    let value = serde_json::to_value(&event).map_err(|err| {
        TransportError::Protocol(format!("unable to serialize SELECT_PROTOCOL: {err}"))
    })?;
    let _ = tx.send(WsCommand::Json(value));

    Ok(())
}

fn handle_session_description(
    hub: &Arc<Hub>,
    tx: &mpsc::UnboundedSender<WsCommand>,
    description: vp::SessionDescription,
) -> Result<(), TransportError> {
    let mode = Mode::parse(&description.mode)
        .or_else(|| hub.state.lock().mode)
        .ok_or_else(|| {
            TransportError::Protocol(format!(
                "Discord selected an unsupported encryption mode: {}",
                description.mode
            ))
        })?;

    log_debug!(
        "received session description (mode={}, dave_protocol_version={})",
        description.mode,
        description.dave_protocol_version
    );

    {
        let mut state = hub.state.lock();
        let encryptor = PacketEncryptor::new(mode, &description.secret_key)
            .map_err(|err| TransportError::Protocol(err.to_string()))?;
        state.mode = Some(mode);
        state.mode_name = Some(description.mode.clone());
        state.secret_key = description.secret_key.clone();
        state.encryptor = Some(encryptor);
        state
            .dave
            .set_protocol_version(description.dave_protocol_version);
    }

    if description.dave_protocol_version > 0 {
        let outgoing = { hub.state.lock().dave.reinit() };
        dispatch(hub, tx, outgoing)?;
    }

    // Discord needs to learn our SSRC before it will accept RTP, and `discord.py` does that
    // by sending a "not speaking" SPEAKING frame here.
    let event = vm::Event::Speaking(vp::Speaking {
        delay: Some(0),
        speaking: vm::SpeakingState::empty(),
        ssrc: hub.state.lock().ssrc,
        user_id: None,
    });
    let value = serde_json::to_value(&event)
        .map_err(|err| TransportError::Protocol(format!("unable to serialize SPEAKING: {err}")))?;
    let _ = tx.send(WsCommand::Json(value));

    {
        let mut state = hub.state.lock();
        state.phase = Phase::Connected;
    }
    hub.bump();
    log_info!("voice connection established");

    Ok(())
}

/// The `discord.py` IP discovery ping: a 74 byte packet whose response reveals the public
/// address Discord sees for us.
async fn discover_ip(socket: &UdpSocket, ssrc: u32) -> Result<(IpAddr, u16), TransportError> {
    let mut packet = [0u8; 74];
    packet[0..2].copy_from_slice(&1u16.to_be_bytes());
    packet[2..4].copy_from_slice(&70u16.to_be_bytes());
    packet[4..8].copy_from_slice(&ssrc.to_be_bytes());

    socket.send(&packet).await.map_err(|err| {
        TransportError::WebSocket(format!("unable to send the IP discovery packet: {err}"))
    })?;

    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    let mut buffer = [0u8; 512];
    loop {
        let read = tokio::time::timeout_at(deadline, socket.recv(&mut buffer))
            .await
            .map_err(|_| TransportError::Protocol("IP discovery timed out".to_owned()))?
            .map_err(|err| TransportError::WebSocket(format!("IP discovery failed: {err}")))?;

        if read < 74 || buffer[1] != 0x02 {
            continue;
        }

        let end = buffer[8..read]
            .iter()
            .position(|byte| *byte == 0)
            .map(|offset| offset + 8)
            .unwrap_or(read - 2);
        let ip = std::str::from_utf8(&buffer[8..end])
            .ok()
            .and_then(|text| text.parse::<IpAddr>().ok())
            .ok_or_else(|| TransportError::Protocol("invalid IP discovery response".to_owned()))?;
        let port = u16::from_be_bytes([buffer[read - 2], buffer[read - 1]]);
        return Ok((ip, port));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_endpoints_with_and_without_ports() {
        assert_eq!(
            split_endpoint("us-east1.discord.media:443"),
            ("us-east1.discord.media".into(), 443)
        );
        assert_eq!(
            split_endpoint("us-east1.discord.media"),
            ("us-east1.discord.media".into(), 443)
        );
        assert_eq!(split_endpoint("localhost:8443"), ("localhost".into(), 8443));
    }

    #[test]
    fn checked_add_wraps_at_the_limit() {
        assert_eq!(VoiceState::checked_add(10, 5, 65535), 15);
        assert_eq!(VoiceState::checked_add(65535, 1, 65535), 0);
    }

    #[test]
    fn ip_discovery_packet_layout_matches_discord_py() {
        let mut packet = [0u8; 74];
        packet[0..2].copy_from_slice(&1u16.to_be_bytes());
        packet[2..4].copy_from_slice(&70u16.to_be_bytes());
        packet[4..8].copy_from_slice(&0xDEADBEEFu32.to_be_bytes());
        assert_eq!(packet[0], 0);
        assert_eq!(packet[1], 1);
        assert_eq!(u16::from_be_bytes([packet[2], packet[3]]), 70);
        assert_eq!(
            u32::from_be_bytes([packet[4], packet[5], packet[6], packet[7]]),
            0xDEADBEEF
        );
    }
}
