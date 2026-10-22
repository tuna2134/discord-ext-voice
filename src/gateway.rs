//! The voice websocket handle.
//!
//! Rust owns the socket: the TCP/TLS/websocket handshake, the heartbeat, IP discovery, the
//! encryption negotiation, the DAVE/MLS session and every frame are handled by the native
//! connection. There is no socket to drive from Python, so this class only exposes the two
//! things `VoiceClient` still asks of a websocket: its latency and its `close`.
//!
//! It is this package's own class, named after `discord.py`'s `DiscordVoiceWebSocket`; nothing
//! of that aiohttp-based implementation is reused.

use pyo3::prelude::*;
use pyo3::types::PyAny;

use crate::connection::VoiceConnection;

#[pyclass(module = "discord_ext_voice._native", name = "DiscordVoiceWebSocket")]
pub struct VoiceWebSocket {
    connection: Py<VoiceConnection>,
    resume: bool,
}

#[pymethods]
impl VoiceWebSocket {
    #[new]
    #[pyo3(signature = (connection, resume=false))]
    fn new(connection: Py<VoiceConnection>, resume: bool) -> Self {
        Self { connection, resume }
    }

    /// Whether this connection's handshake sends RESUME rather than IDENTIFY.
    #[getter]
    fn resume(&self) -> bool {
        self.resume
    }

    /// Latency between a HEARTBEAT and a HEARTBEAT_ACK, in seconds.
    #[getter]
    fn latency(&self, py: Python<'_>) -> f64 {
        self.connection.borrow(py).current_latency()
    }

    /// The close code the socket ended with, if it has.
    #[getter]
    fn close_code(&self, py: Python<'_>) -> Option<u16> {
        self.connection.bind(py).borrow().close_code_raw()
    }

    /// Close the voice websocket.
    #[pyo3(signature = (code=1000))]
    fn close<'py>(&self, py: Python<'py>, code: u16) -> PyResult<Bound<'py, PyAny>> {
        self.connection
            .bind(py)
            .borrow()
            .close_future(py, Some(code))
    }

    fn __repr__(&self) -> String {
        format!(
            "<discord_ext_voice.DiscordVoiceWebSocket resume={}>",
            self.resume
        )
    }
}
