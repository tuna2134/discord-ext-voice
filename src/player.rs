//! The playback clock.
//!
//! `discord.player.AudioPlayer` is a Python `threading.Thread` that reads an `AudioSource`,
//! encodes it and hands each 20ms frame to the transport. This module is that loop written in
//! Rust: it owns the clock, the pause/resume/stop signals, the 20ms pacing and the `SPEAKING`
//! announcements, and calls into Python only for the source itself (`read`/`is_opus`/`cleanup`)
//! and for the `after` callback.
//!
//! The GIL is never held while a native lock is waited for, and the source lock is released
//! before `read()` runs, so a Python thread that takes either can always make progress — see
//! `crate::logging` for the deadlock this ordering avoids.
//!
//! `discord_ext_voice.player.AudioPlayer` wraps this object; `discord.player` is not imported at
//! all.

use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use parking_lot::{Condvar, Mutex};
use pyo3::exceptions::PyBaseException;
use pyo3::prelude::*;
use voice_model as vm;

use crate::connection::{self, SendError, VoiceConnection};
use crate::transport::{self, Hub, wait_duration};

/// The gap between two frames. `discord.player.AudioPlayer.DELAY` is the same number, derived
/// from the same 20ms frame length.
pub const FRAME_DELAY: f64 = crate::opus::FRAME_LENGTH as f64 / 1000.0;

/// Frames of Opus silence sent while paused and on the way out, as discord.py does.
const SILENCE_FRAMES: usize = 5;

/// How long a disconnected player sleeps between polls of the connection state.
const RECONNECT_POLL: Duration = Duration::from_millis(50);

/// The `SPEAKING` states the player announces.
const SPEAKING_VOICE: vm::SpeakingState = vm::SpeakingState::MICROPHONE;
const SPEAKING_NONE: vm::SpeakingState = vm::SpeakingState::empty();

/// An error on its way to `after`, with the message used when there is no callback to hand it to.
struct PlayError {
    message: String,
    value: Py<PyBaseException>,
}

impl PlayError {
    fn from_pyerr(py: Python<'_>, err: PyErr) -> Self {
        Self {
            message: err.to_string(),
            value: err.into_value(py),
        }
    }

    fn from_send(py: Python<'_>, err: SendError) -> Self {
        Self::from_pyerr(py, err.into_pyerr())
    }
}

/// Why the playback loop ended.
enum Outcome {
    /// The source ran out: the voice state is announced as off, then silence.
    Exhausted,
    /// An error (a source that raised, a dead socket): `after` gets it.
    Failed(PlayError),
    /// The connection did not come back, or the interpreter is going away.
    Aborted,
}

/// The playback state the Python side flips from another thread.
#[derive(Default)]
struct Signals {
    /// `True` between `pause()` and `resume()`.
    paused: bool,
    /// `True` once `stop()` was called or the loop finished.
    ended: bool,
    /// Set by `resume()`/`set_source()`: restart the 20ms clock rather than trying to catch up
    /// with the time spent paused.
    restart_clock: bool,
}

/// Everything the playback thread needs, shared with the Python object.
struct PlayerInner {
    hub: Arc<Hub>,
    /// The source being played. Swapped by `set_source` while the thread runs.
    source: Mutex<Py<PyAny>>,
    /// The callback invoked once, with the error or `None`.
    after: Option<Py<PyAny>>,
    /// How long to wait for a reconnection before giving up.
    timeout: Duration,
    signals: Mutex<Signals>,
    wake: Condvar,
    thread: Mutex<Option<JoinHandle<()>>>,
}

impl PlayerInner {
    fn is_connected(&self) -> bool {
        self.hub.state.lock().phase.is_connected()
    }

    /// Wait for the connection to come back, up to `timeout`. Polls instead of awaiting: this
    /// runs on the playback thread, which is deliberately not a tokio task.
    fn wait_until_connected(&self) -> bool {
        let deadline = Instant::now() + self.timeout;
        loop {
            if self.is_connected() {
                return true;
            }
            if self.signals.lock().ended || Instant::now() >= deadline {
                return self.is_connected();
            }
            thread::sleep(RECONNECT_POLL);
        }
    }

    fn send_speaking(&self, speaking: vm::SpeakingState) {
        transport::send_speaking(&self.hub, speaking);
    }

    /// Send `count` frames of Opus silence. Any failure is inconsequential, so it is dropped.
    fn send_silence(&self, count: usize) {
        for _ in 0..count {
            if connection::send_audio_frame(&self.hub, &crate::opus::OPUS_SILENCE, false).is_err() {
                return;
            }
        }
    }
}

/// The native playback clock.
///
/// `discord_ext_voice.player.AudioPlayer` is the documented wrapper; the methods mirror
/// `discord.player.AudioPlayer` so that either can be used the same way.
#[pyclass(module = "discord_ext_voice._native", name = "AudioPlayer")]
pub struct PyAudioPlayer {
    inner: Arc<PlayerInner>,
}

#[pymethods]
impl PyAudioPlayer {
    #[new]
    #[pyo3(signature = (connection, source, *, after=None, timeout=30.0))]
    fn new(
        py: Python<'_>,
        connection: PyRef<'_, VoiceConnection>,
        source: Py<PyAny>,
        after: Option<Py<PyAny>>,
        timeout: f64,
    ) -> PyResult<Self> {
        if let Some(after) = after.as_ref()
            && !after.bind(py).is_callable()
        {
            return Err(pyo3::exceptions::PyTypeError::new_err(
                "Expected a callable for the \"after\" parameter.",
            ));
        }

        Ok(Self {
            inner: Arc::new(PlayerInner {
                hub: connection.hub(),
                source: Mutex::new(source),
                after,
                timeout: wait_duration(timeout),
                signals: Mutex::new(Signals::default()),
                wake: Condvar::new(),
                thread: Mutex::new(None),
            }),
        })
    }

    /// Start playback. Like a `threading.Thread`, a player starts once: use `set_source` to
    /// change what a running player plays.
    fn start(&self) -> PyResult<()> {
        let mut slot = self.inner.thread.lock();
        if let Some(handle) = slot.as_ref()
            && !handle.is_finished()
        {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "playback has already been started",
            ));
        }

        {
            let mut signals = self.inner.signals.lock();
            if signals.ended {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(
                    "playback has already been started",
                ));
            }
            signals.paused = false;
        }

        let inner = Arc::clone(&self.inner);
        let handle = thread::Builder::new()
            .name(format!(
                "audio-player:{:x}",
                Arc::as_ptr(&self.inner) as usize
            ))
            .spawn(move || run(inner))
            .map_err(|err| pyo3::exceptions::PyRuntimeError::new_err(err.to_string()))?;
        *slot = Some(handle);
        Ok(())
    }

    /// Stop playing and announce that this client is no longer speaking.
    fn stop(&self) {
        {
            let mut signals = self.inner.signals.lock();
            signals.ended = true;
            signals.paused = false;
        }
        self.inner.wake.notify_all();
        self.inner.send_speaking(SPEAKING_NONE);
    }

    /// Pause playback: the jitter buffer is kept fed with silence.
    #[pyo3(signature = (update_speaking=true))]
    fn pause(&self, update_speaking: bool) {
        {
            let mut signals = self.inner.signals.lock();
            signals.paused = true;
        }
        self.inner.wake.notify_all();
        if update_speaking {
            self.inner.send_speaking(SPEAKING_NONE);
        }
    }

    /// Resume a paused player, restarting its clock.
    #[pyo3(signature = (update_speaking=true))]
    fn resume(&self, update_speaking: bool) {
        {
            let mut signals = self.inner.signals.lock();
            signals.paused = false;
            signals.restart_clock = true;
        }
        self.inner.wake.notify_all();
        if update_speaking {
            self.inner.send_speaking(SPEAKING_VOICE);
        }
    }

    fn is_playing(&self) -> bool {
        let signals = self.inner.signals.lock();
        !signals.ended && !signals.paused
    }

    fn is_paused(&self) -> bool {
        let signals = self.inner.signals.lock();
        !signals.ended && signals.paused
    }

    /// Whether the playback thread is still running.
    fn is_alive(&self) -> bool {
        let slot = self.inner.thread.lock();
        match slot.as_ref() {
            Some(handle) => !handle.is_finished(),
            None => false,
        }
    }

    /// Swap the source and restart the clock, without touching the speaking state.
    fn set_source(&self, source: Py<PyAny>) {
        {
            let mut signals = self.inner.signals.lock();
            signals.paused = true;
        }
        self.inner.wake.notify_all();

        *self.inner.source.lock() = source;

        {
            let mut signals = self.inner.signals.lock();
            signals.paused = false;
            signals.restart_clock = true;
        }
        self.inner.wake.notify_all();
    }

    #[getter]
    fn source(&self, py: Python<'_>) -> Py<PyAny> {
        self.inner.source.lock().clone_ref(py)
    }

    /// Wait for the playback thread to finish, at most `timeout` seconds.
    #[pyo3(signature = (timeout=None))]
    fn join(&self, py: Python<'_>, timeout: Option<f64>) -> PyResult<()> {
        let deadline = timeout.map(|seconds| Instant::now() + wait_duration(seconds));
        while self.is_alive() {
            if deadline.is_some_and(|deadline| Instant::now() >= deadline) {
                return Ok(());
            }
            // Sleeping with the GIL held would block every other thread of the interpreter.
            py.detach(|| thread::sleep(Duration::from_millis(1)));
        }

        let handle = self.inner.thread.lock().take();
        if let Some(handle) = handle {
            py.detach(move || {
                let _ = handle.join();
            });
        }
        Ok(())
    }

    #[classattr]
    const DELAY: f64 = FRAME_DELAY;

    fn __repr__(&self) -> String {
        let signals = self.inner.signals.lock();
        format!(
            "<discord_ext_voice.AudioPlayer playing={} paused={} ended={}>",
            !signals.ended && !signals.paused,
            signals.paused,
            signals.ended
        )
    }
}

/// The playback loop, on its own thread, until the source is exhausted, an error occurs, the
/// connection disappears for longer than the timeout, or `stop()` is called.
fn run(inner: Arc<PlayerInner>) {
    let outcome = play(&inner);

    {
        let mut signals = inner.signals.lock();
        signals.ended = true;
        signals.paused = false;
    }
    inner.wake.notify_all();

    match &outcome {
        Outcome::Exhausted => {
            if inner.is_connected() {
                inner.send_silence(SILENCE_FRAMES);
            }
        }
        Outcome::Failed(_) => inner.send_speaking(SPEAKING_NONE),
        Outcome::Aborted => {}
    }

    finish(&inner, &outcome);
}

fn play(inner: &Arc<PlayerInner>) -> Outcome {
    let mut started = Instant::now();
    let mut loops: u64 = 0;

    inner.send_speaking(SPEAKING_VOICE);

    loop {
        // Paused, or stopped, between two frames?
        {
            let mut signals = inner.signals.lock();
            if signals.ended {
                return Outcome::Exhausted;
            }
            if signals.paused {
                drop(signals);
                inner.send_silence(SILENCE_FRAMES);
                let mut signals = inner.signals.lock();
                while signals.paused && !signals.ended {
                    inner.wake.wait(&mut signals);
                }
                if signals.ended {
                    return Outcome::Exhausted;
                }
                // `resume` has already re-announced the voice state.
                drop(signals);
                started = Instant::now();
                loops = 0;
                continue;
            }
            if signals.restart_clock {
                signals.restart_clock = false;
                started = Instant::now();
                loops = 0;
            }
        }

        let Some(frame) = read_frame(inner) else {
            return Outcome::Aborted;
        };
        let frame = match frame {
            Ok(frame) => frame,
            Err(error) => return Outcome::Failed(error),
        };

        if frame.data.is_empty() {
            if let Some(error) = frame.current_error {
                return Outcome::Failed(error);
            }
            // `discord.py` calls `stop()` here, which announces the end of the voice state.
            inner.send_speaking(SPEAKING_NONE);
            return Outcome::Exhausted;
        }

        if !inner.is_connected() {
            crate::log_debug!("playback: not connected, waiting for the voice connection");
            if !inner.wait_until_connected() {
                crate::log_debug!("playback: aborting, the voice connection did not come back");
                return Outcome::Aborted;
            }
            crate::log_debug!("playback: reconnected, resuming");
            inner.send_speaking(SPEAKING_VOICE);
            started = Instant::now();
            loops = 0;
        }

        // Encoded, encrypted and sent with no Python involvement at all: the GIL is only taken to
        // turn a failure into the exception `after` receives.
        let result = connection::send_audio_frame(&inner.hub, &frame.data, !frame.opus);
        if let Err(err) = result {
            // The error has to be a Python object for `after`; if the interpreter is already
            // going away there is nobody left to tell, so stop quietly.
            let Some(error) = Python::try_attach(|py| PlayError::from_send(py, err)) else {
                return Outcome::Aborted;
            };
            return Outcome::Failed(error);
        }

        loops += 1;
        let next = started + Duration::from_secs_f64(FRAME_DELAY * loops as f64);
        thread::sleep(pace_delay(next, Instant::now()));
    }
}

/// How long to wait before sending the next frame, whose target time is `next`.
///
/// This is `discord.py`'s formula: normally the frame delay, less the time the frame's own
/// processing took, so the 20ms spacing absorbs jitter instead of accumulating it. A player that
/// has fallen more than one frame behind sends the next frame immediately.
fn pace_delay(next: Instant, now: Instant) -> Duration {
    match next.checked_duration_since(now) {
        Some(remaining) => Duration::from_secs_f64(FRAME_DELAY + remaining.as_secs_f64()),
        None => {
            Duration::from_secs_f64((FRAME_DELAY - now.duration_since(next).as_secs_f64()).max(0.0))
        }
    }
}

/// One frame read from the Python source.
struct Frame {
    data: Vec<u8>,
    /// Whether `data` is an Opus packet rather than PCM.
    opus: bool,
    /// A foreign source that failed reports it through `_current_error` instead of raising.
    current_error: Option<PlayError>,
}

/// Read one frame, or `None` when the interpreter is gone.
fn read_frame(inner: &Arc<PlayerInner>) -> Option<Result<Frame, PlayError>> {
    Python::try_attach(|py| {
        // Cloned out of the lock, which is therefore free while `read` runs: `set_source` from
        // another thread never waits for a slow decode.
        let source = inner.source.lock().clone_ref(py);
        let source = source.bind(py);

        let data = match source.call_method0("read") {
            Ok(data) => data,
            Err(err) => return Err(PlayError::from_pyerr(py, err)),
        };
        let data = match data.extract::<Vec<u8>>() {
            Ok(data) => data,
            Err(err) => return Err(PlayError::from_pyerr(py, err)),
        };

        let opus = source
            .call_method0("is_opus")
            .and_then(|value| value.is_truthy())
            .unwrap_or(false);

        let current_error = if data.is_empty() {
            read_current_error(source)
        } else {
            None
        };

        Ok(Frame {
            data,
            opus,
            current_error,
        })
    })
}

/// `getattr(source, '_current_error', None)`, as an exception instance.
fn read_current_error(source: &Bound<'_, PyAny>) -> Option<PlayError> {
    let error = source.getattr("_current_error").ok()?;
    if !error.is_truthy().unwrap_or(false) {
        return None;
    }
    let message = error
        .str()
        .map(|text| text.to_string_lossy().into_owned())
        .unwrap_or_else(|_| "the audio source failed".to_owned());
    Some(PlayError {
        message,
        value: error.cast_into::<PyBaseException>().ok()?.unbind(),
    })
}

/// The `finally` block of `discord.player.AudioPlayer.run`: call `after`, then clean the source
/// up. The GIL is taken once and dropped again; if the interpreter is already gone there is
/// nothing left to do.
fn finish(inner: &Arc<PlayerInner>, outcome: &Outcome) {
    let _ = Python::try_attach(|py| {
        let error = match outcome {
            Outcome::Failed(error) => Some(error),
            _ => None,
        };

        match inner.after.as_ref() {
            Some(after) => {
                let argument = match error {
                    Some(error) => error.value.clone_ref(py).into_any(),
                    None => py.None(),
                };
                if let Err(err) = after.bind(py).call1((argument,)) {
                    crate::log_error!("calling the after function of a playback failed: {err}");
                }
            }
            None => {
                if let Some(error) = error {
                    crate::log_error!("exception in the audio player thread: {}", error.message);
                }
            }
        }

        // `AudioSource.cleanup` has to survive being called twice (our own sources clean up in
        // `__del__` as well), and a foreign source may not have it at all.
        let source = inner.source.lock().clone_ref(py);
        if let Err(err) = source.bind(py).call_method0("cleanup")
            && !err.is_instance_of::<pyo3::exceptions::PyAttributeError>(py)
        {
            crate::log_debug!("calling cleanup on the audio source failed: {err}");
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The clock is the one value the Python layer reads back as `AudioPlayer.DELAY`.
    #[test]
    fn the_frame_delay_is_the_20ms_frame() {
        assert!((FRAME_DELAY - 0.02).abs() < f64::EPSILON);
        assert!(
            (FRAME_DELAY - crate::opus::SAMPLES_PER_FRAME as f64 / 48000.0).abs() < f64::EPSILON
        );
    }

    /// The silence packet is Opus' DTX frame; the wire format is Discord's, not ours.
    #[test]
    fn silence_is_the_opus_dtx_frame() {
        assert_eq!(crate::opus::OPUS_SILENCE, [0xf8, 0xff, 0xfe]);
    }

    #[test]
    fn the_pace_keeps_the_frame_spacing() {
        let now = Instant::now();
        // Behind schedule: the processing time is absorbed, leaving the remainder of the frame.
        let next = now + Duration::from_millis(10);
        assert!((pace_delay(next, now).as_secs_f64() - 0.03).abs() < 1e-9);

        // On schedule: one whole frame.
        assert!((pace_delay(now, now).as_secs_f64() - FRAME_DELAY).abs() < 1e-9);

        // Slightly late: only the remainder of the frame is waited out.
        let next = now - Duration::from_millis(10);
        assert!((pace_delay(next, now).as_secs_f64() - 0.01).abs() < 1e-9);

        // More than a frame late: send immediately rather than sleeping a negative time.
        let next = now - Duration::from_millis(500);
        assert_eq!(pace_delay(next, now), Duration::ZERO);
    }
}
