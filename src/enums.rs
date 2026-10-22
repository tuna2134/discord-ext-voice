//! The small enums the Python layer dispatches on.
//!
//! The connection flow is a state machine, and every decision it makes is one of four values:
//! the phase it moved to, what a `VOICE_STATE_UPDATE` means, what a `VOICE_SERVER_UPDATE` means
//! and what a websocket close code means. All four cross the boundary as real Python `IntEnum`
//! classes, built here from the same tables the Rust code uses, so the numbers cannot drift and
//! a caller sees names instead of magic integers.
//!
//! They are built at module-init time (the functional `enum.IntEnum` API) and kept in
//! [`PyOnceLock`]s: constructing a member later on only costs one Python call.

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyAny;

// ---- the phase ladder ----------------------------------------------------------------

/// `ConnectionFlowState`: discord.py's names and values for the phases of a voice connection.
const FLOW_STATES: &[(&str, u8)] = &[
    ("disconnected", 0),
    ("set_guild_voice_state", 1),
    ("got_voice_state_update", 2),
    ("got_voice_server_update", 3),
    ("got_both_voice_updates", 4),
    ("websocket_connected", 5),
    ("got_websocket_ready", 6),
    ("got_ip_discovery", 7),
    ("connected", 8),
];

/// `VoiceStateAction`: what a `VOICE_STATE_UPDATE` tells the client to do.
const VOICE_STATE_ACTIONS: &[(&str, u8)] = &[
    ("ignore", 0),
    ("disconnect", 1),
    ("update_channel", 2),
    ("move_during_handshake", 3),
];

/// `VoiceServerAction`: what a `VOICE_SERVER_UPDATE` tells the client to do.
const VOICE_SERVER_ACTIONS: &[(&str, u8)] = &[
    ("ignore", 0),
    ("close_old_websocket", 1),
    ("reconnect_flow", 2),
];

/// `CloseAction`: what the websocket poller does with a close code.
const CLOSE_ACTIONS: &[(&str, u8)] = &[
    ("normal", 0),
    ("quiet", 1),
    ("by_discord", 2),
    ("potential_reconnect", 3),
    ("rate_limited", 4),
    ("resume", 5),
    ("reconnect", 6),
    ("raise_", 7),
];

pub static FLOW_STATE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
pub static VOICE_STATE_ACTION: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
pub static VOICE_SERVER_ACTION: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
pub static CLOSE_ACTION: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// Build the four enum classes and add them to the native module.
pub fn install(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    let flow = build(py, "ConnectionFlowState", FLOW_STATES)?;
    let voice_state = build(py, "VoiceStateAction", VOICE_STATE_ACTIONS)?;
    let voice_server = build(py, "VoiceServerAction", VOICE_SERVER_ACTIONS)?;
    let close = build(py, "CloseAction", CLOSE_ACTIONS)?;

    module.add("ConnectionFlowState", flow.bind(py).clone())?;
    module.add("VoiceStateAction", voice_state.bind(py).clone())?;
    module.add("VoiceServerAction", voice_server.bind(py).clone())?;
    module.add("CloseAction", close.bind(py).clone())?;

    // Module init runs once, and it is the only writer of these cells.
    let _ = FLOW_STATE.set(py, flow);
    let _ = VOICE_STATE_ACTION.set(py, voice_state);
    let _ = VOICE_SERVER_ACTION.set(py, voice_server);
    let _ = CLOSE_ACTION.set(py, close);
    Ok(())
}

/// Turn `value` into a member of the enum built into `cell`.
///
/// Before module init (only reachable from a Rust-only test) the raw integer is returned, which
/// keeps this infallible for callers.
pub fn member(py: Python<'_>, cell: &PyOnceLock<Py<PyAny>>, value: u8) -> PyResult<Py<PyAny>> {
    match cell.get(py) {
        Some(class) => Ok(class.bind(py).call1((value,))?.unbind()),
        None => Ok(value.into_pyobject(py)?.into_any().unbind()),
    }
}

fn build(py: Python<'_>, name: &str, members: &[(&str, u8)]) -> PyResult<Py<PyAny>> {
    let enum_module = py.import("enum")?;
    let int_enum = enum_module.getattr("IntEnum")?;
    // The functional API accepts a list of `(name, value)` pairs. Building from Rust means the
    // public enum is exactly as iterable and int-comparable as a hand-written one.
    let class = int_enum.call1((name, members))?;
    class.setattr("__module__", "discord_ext_voice")?;
    Ok(class.unbind())
}
