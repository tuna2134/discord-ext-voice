//! Bridges Rust-side diagnostics into Python's `logging` module so that a bot owner can see
//! voice handshake progress by configuring a handler for `discord_ext_voice`.
//!
//! Every call hands the record to Python and therefore needs the GIL — `try_attach` is not a
//! non-blocking acquire, it only skips a shut-down interpreter.
//!
//! **Never log while holding a native lock.** A Python thread can hold the GIL and wait for that
//! same lock — the audio path does exactly that (`send_audio_packet` runs with the GIL held and
//! takes the connection lock) — so a lock-holding task that waits for the GIL deadlocks the
//! connection. Records produced under a lock are buffered instead (`DaveState::take_logs`, and
//! `transport::with_state` which flushes them once the guard is dropped).

use std::sync::OnceLock;

use pyo3::prelude::*;

static LOGGER: OnceLock<Py<PyAny>> = OnceLock::new();

fn logger(py: Python<'_>) -> Option<Py<PyAny>> {
    if let Some(logger) = LOGGER.get() {
        return Some(logger.clone_ref(py));
    }

    let logging = py.import("logging").ok()?;
    let logger = logging
        .call_method1("getLogger", ("discord_ext_voice",))
        .ok()?;
    let logger = logger.unbind();
    let _ = LOGGER.set(logger.clone_ref(py));
    Some(logger)
}

/// Emit a log record to the `discord_ext_voice` Python logger.
///
/// Never panics: if the interpreter is not initialised (which happens in `cargo test`) the
/// record is silently dropped.
pub fn log(level: &str, message: &str) {
    let _ = Python::try_attach(|py| {
        let Some(logger) = logger(py) else {
            return;
        };
        if let Err(err) = logger.bind(py).call_method1(level, (message,)) {
            // Never let logging failures take down the voice connection.
            err.write_unraisable(py, None);
        }
    });
}

macro_rules! log_debug {
    ($($arg:tt)*) => {
        $crate::logging::log("debug", &format!($($arg)*))
    };
}

macro_rules! log_info {
    ($($arg:tt)*) => {
        $crate::logging::log("info", &format!($($arg)*))
    };
}

macro_rules! log_warning {
    ($($arg:tt)*) => {
        $crate::logging::log("warning", &format!($($arg)*))
    };
}

macro_rules! log_error {
    ($($arg:tt)*) => {
        $crate::logging::log("error", &format!($($arg)*))
    };
}

pub(crate) use {log_debug, log_error, log_info, log_warning};
