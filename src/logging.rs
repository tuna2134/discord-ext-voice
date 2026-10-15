//! Bridges Rust-side diagnostics into Python's `logging` module so that a bot owner can see
//! voice handshake progress by configuring a handler for `discord_ext_voice`.
//!
//! Every call acquires the GIL, so these macros are only used on connection lifecycle events
//! and not on the per-frame audio path.

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
