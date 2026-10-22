//! Bridges Rust-side diagnostics into Python's `logging` module so that a bot owner can see
//! voice handshake progress by configuring a handler for `discord_ext_voice`.
//!
//! Every emitted record hands a string to Python and therefore needs the GIL. The hot audio path
//! must not pay for records nobody asked for, so the level is cached in an atomic: a disabled
//! record costs one atomic load and no formatting, and the effective Python level is re-read
//! every couple of seconds so runtime `setLevel` calls are still picked up.
//!
//! **Never log while holding a native lock.** A Python thread can hold the GIL and wait for that
//! same lock — the audio path does exactly that (`send_audio_packet` runs with the GIL held and
//! takes the connection lock) — so a lock-holding task that waits for the GIL deadlocks the
//! connection. Records produced under a lock are buffered instead (`DaveState::take_logs`, and
//! `transport::with_state` which flushes them once the guard is dropped).

use std::fmt;
use std::sync::OnceLock;
use std::sync::atomic::{AtomicU8, Ordering};
use std::time::{Duration, Instant};

use parking_lot::Mutex;
use pyo3::prelude::*;

/// How often the Python logger's effective level is re-read, so a `setLevel` at runtime is
/// honoured without a GIL round trip for every record.
const LEVEL_REFRESH: Duration = Duration::from_secs(2);

/// The cached effective level: `0` until it has been read once.
static LEVEL: AtomicU8 = AtomicU8::new(0);
static LEVEL_CHECKED: Mutex<Option<Instant>> = Mutex::new(None);

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

/// `logging.DEBUG`/`INFO`/... for a level name.
fn level_number(level: &str) -> u8 {
    match level {
        "debug" => 10,
        "info" => 20,
        "warning" => 30,
        "error" => 40,
        _ => 20,
    }
}

/// Whether a record at `level` would be emitted.
///
/// The check is cheap (one atomic load) until the cache has to be refreshed. A failed refresh
/// leaves the previous value in place; when the interpreter is gone the eventual attach in
/// [`emit`] is skipped too.
fn enabled(level: u8) -> bool {
    let refresh = {
        let mut checked = LEVEL_CHECKED.lock();
        match *checked {
            Some(previous) if previous.elapsed() < LEVEL_REFRESH => false,
            _ => {
                *checked = Some(Instant::now());
                true
            }
        }
    };

    if refresh && let Some(effective) = query_level() {
        LEVEL.store(effective, Ordering::Relaxed);
    }

    let effective = LEVEL.load(Ordering::Relaxed);
    effective == 0 || level >= effective
}

fn query_level() -> Option<u8> {
    Python::try_attach(|py| {
        let logger = logger(py)?;
        logger
            .bind(py)
            .call_method0("getEffectiveLevel")
            .and_then(|level| level.extract::<u8>())
            .ok()
    })
    .flatten()
}

/// Emit a record whose level has already been checked.
fn emit(level: &str, message: &str) {
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

/// Emit a record whose message has already been formatted.
///
/// Used by the buffered-log paths, which have a string in hand after releasing their lock.
pub fn log(level: &str, message: &str) {
    if enabled(level_number(level)) {
        emit(level, message);
    }
}

/// Emit a lazily formatted record.
///
/// `format_args!` does not allocate until it is actually formatted, so a disabled record costs
/// nothing beyond the atomic level check.
pub fn log_args(level: &str, args: fmt::Arguments<'_>) {
    if enabled(level_number(level)) {
        emit(level, &args.to_string());
    }
}

macro_rules! log_debug {
    ($($arg:tt)*) => {
        $crate::logging::log_args("debug", format_args!($($arg)*))
    };
}

macro_rules! log_info {
    ($($arg:tt)*) => {
        $crate::logging::log_args("info", format_args!($($arg)*))
    };
}

macro_rules! log_warning {
    ($($arg:tt)*) => {
        $crate::logging::log_args("warning", format_args!($($arg)*))
    };
}

macro_rules! log_error {
    ($($arg:tt)*) => {
        $crate::logging::log_args("error", format_args!($($arg)*))
    };
}

pub(crate) use {log_debug, log_error, log_info, log_warning};
