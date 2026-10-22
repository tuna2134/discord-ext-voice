//! The audio sources, as Python classes.
//!
//! This module is the Python-visible half of [`crate::source`]: `AudioSource`, `PCMAudio`,
//! `FFmpegPCMAudio`, `FFmpegOpusAudio` and `PCMVolumeTransformer` are all defined here, in Rust.
//! Nothing calls an `ffmpeg` binary: `FFmpegPCMAudio` demuxes and decodes in process with
//! `symphonia`, resamples with `rubato`, and `FFmpegOpusAudio` passes already-encoded Opus
//! packets through untouched (or falls back to decoding, letting the player's native encoder
//! produce the packets).
//!
//! The names are kept from `discord.py` so that a bot only has to change the import: the
//! arguments are not. The FFmpeg CLI arguments (`before_options`, `options`, `executable`,
//! `pipe`, `stderr`, `force_cli`) do not exist here, because there is no CLI; pass the media
//! itself and use `seek=` where `-ss` used to go. Unknown keyword arguments are rejected with a
//! message that says so.
//!
//! A source only has to provide `read`, `is_opus` and (optionally) `cleanup`: the player calls
//! exactly those, so a `discord.AudioSource` subclass, or a user's own duck-typed object, is
//! still playable.

use std::collections::HashMap;

use parking_lot::Mutex;
use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::pyclass_init::PyClassInitializer;
#[cfg(debug_assertions)]
use pyo3::types::PyBytes;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyString, PyTuple};

use crate::errors::FFmpegSourceError;
use crate::source::{
    self, DecodeSource, FRAME_BYTES, OpusSource, parse_headers, probe_source, scale_pcm_s16,
};

/// The native base class of the sources this package ships.
///
/// It is a marker/base, not the Python extension point it used to be: the data path
/// (`read`/`is_opus`/`cleanup`) lives in Rust on the concrete source classes, and `play()`
/// rejects a source implemented in Python.
#[pyclass(
    module = "discord_ext_voice._native",
    name = "AudioSource",
    subclass,
    dict
)]
pub struct AudioSource;

#[pymethods]
impl AudioSource {
    /// Accept and ignore any arguments, so that a Python subclass can define its own `__init__`
    /// with the arguments it needs (the base `tp_new` still receives them).
    #[new]
    #[pyo3(signature = (*_args, **_kwargs))]
    fn new(_args: Bound<'_, PyTuple>, _kwargs: Option<Bound<'_, PyDict>>) -> Self {
        Self
    }

    fn __repr__(slf: PyRef<'_, Self>, py: Python<'_>) -> PyResult<String> {
        Ok(format!("<{}>", slf.into_pyobject(py)?.get_type().name()?))
    }
}

/// Raw 16 bit 48kHz stereo PCM, read from a Python file-like object.
#[pyclass(
    module = "discord_ext_voice._native",
    name = "PCMAudio",
    extends = AudioSource,
    subclass
)]
pub struct PcmAudio {
    stream: Mutex<Option<Py<PyAny>>>,
}

#[pymethods]
impl PcmAudio {
    /// Allocate empty; `__init__` fills it in, so a Python subclass may pass arguments of its
    /// own without the base `tp_new` rejecting them.
    #[new]
    #[pyo3(signature = (*_args, **_kwargs), text_signature = "(stream)")]
    fn new(
        _args: Bound<'_, PyTuple>,
        _kwargs: Option<Bound<'_, PyDict>>,
    ) -> PyClassInitializer<Self> {
        PyClassInitializer::from(AudioSource).add_subclass(Self {
            stream: Mutex::new(None),
        })
    }

    #[pyo3(signature = (stream))]
    fn __init__(slf: PyClassGuardMut<'_, Self>, stream: Py<PyAny>) {
        *slf.stream.lock() = Some(stream);
    }

    /// Marks a source whose implementation is native; `is_audio_source` and `play()` require
    /// this (Python subclasses inherit it unless they start from `AudioSource` directly).
    #[classattr]
    const NATIVE_AUDIO_SOURCE: bool = true;

    fn __repr__(&self) -> String {
        "<discord_ext_voice.PCMAudio>".to_owned()
    }
}

impl PcmAudio {
    fn is_opus_native(&self) -> bool {
        false
    }

    fn cleanup_native(&self, py: Python<'_>) {
        // `PCMAudio` wraps a Python file object; closing it is the resource cleanup. Frames are
        // still read natively by the player, not through this object.
        let stream = self
            .stream
            .lock()
            .as_ref()
            .map(|stream| stream.clone_ref(py));
        if let Some(stream) = stream {
            let _ = stream.bind(py).call_method0("close").map(|_| ());
        }
    }

    /// The native implementation of `read`, used directly by `PCMVolumeTransformer` when the
    /// wrapped source is one of ours (the Python method would only add a dispatch).
    fn read_native(&self, py: Python<'_>) -> PyResult<Vec<u8>> {
        let stream = self
            .stream
            .lock()
            .as_ref()
            .map(|stream| stream.clone_ref(py));
        let Some(stream) = stream else {
            return Ok(Vec::new());
        };
        let data = stream.bind(py).call_method1("read", (FRAME_BYTES,))?;
        let data = data.extract::<Vec<u8>>()?;
        if data.len() != FRAME_BYTES {
            // A partial frame is not sendable, and discord.py's player reads that as the end.
            return Ok(Vec::new());
        }
        Ok(data)
    }
}

/// A PCM source decoded and resampled in process.
#[pyclass(
    module = "discord_ext_voice._native",
    name = "FFmpegPCMAudio",
    extends = AudioSource,
    subclass
)]
pub struct FfmpegPcmAudio {
    reader: Mutex<Option<Reader>>,
}

#[pymethods]
impl FfmpegPcmAudio {
    #[new]
    #[pyo3(signature = (*_args, **_kwargs), text_signature = "(source, *, seek=None, headers=None, **options)")]
    fn new(
        _args: Bound<'_, PyTuple>,
        _kwargs: Option<Bound<'_, PyDict>>,
    ) -> PyClassInitializer<Self> {
        PyClassInitializer::from(AudioSource).add_subclass(Self {
            reader: Mutex::new(None),
        })
    }

    #[pyo3(signature = (source, *, seek=None, headers=None, **options))]
    fn __init__(
        slf: PyClassGuardMut<'_, Self>,
        py: Python<'_>,
        source: &Bound<'_, PyAny>,
        seek: Option<Py<PyAny>>,
        headers: Option<Py<PyAny>>,
        options: Option<Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        reject_cli_options(options)?;
        let reader = build_reader(py, source, seek, headers, false)?;
        *slf.reader.lock() = Some(reader);
        Ok(())
    }

    #[classattr]
    const NATIVE_AUDIO_SOURCE: bool = true;

    /// Open a source without blocking the event loop.
    ///
    /// The constructor (``FFmpegPCMAudio(url)``) probes the input synchronously, which for an
    /// ``http(s)`` URL can take seconds. This classmethod does the same work in a native future,
    /// so a bot command can ``await`` it.
    #[staticmethod]
    #[pyo3(signature = (source, *, seek=None, headers=None, **options))]
    fn open<'py>(
        py: Python<'py>,
        source: Py<PyAny>,
        seek: Option<Py<PyAny>>,
        headers: Option<Py<PyAny>>,
        options: Option<Bound<'_, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        reject_cli_options(options)?;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            // The blocking HTTP client must not be created or dropped on a tokio worker: it
            // builds (and then drops) its own runtime, which panics in an async context. A
            // blocking pool thread is the right place for the probe.
            let reader = tokio::task::spawn_blocking(move || {
                Python::try_attach(|py| build_reader(py, source.bind(py), seek, headers, false))
                    .unwrap_or_else(|| {
                        Err(FFmpegSourceError::new_err(
                            "the interpreter is shutting down",
                        ))
                    })
            })
            .await
            .map_err(|err| FFmpegSourceError::new_err(err.to_string()))??;
            Python::attach(|py| {
                Ok::<_, PyErr>(
                    Py::new(
                        py,
                        PyClassInitializer::from(AudioSource).add_subclass(Self {
                            reader: Mutex::new(Some(reader)),
                        }),
                    )?
                    .into_any(),
                )
            })
        })
    }

    fn __repr__(&self) -> String {
        "<discord_ext_voice.FFmpegPCMAudio (48kHz stereo s16)>".to_owned()
    }
}

/// An Opus source: raw packets when the input already carries them, PCM otherwise.
#[pyclass(
    module = "discord_ext_voice._native",
    name = "FFmpegOpusAudio",
    extends = AudioSource,
    subclass
)]
pub struct FfmpegOpusAudio {
    reader: Mutex<Option<Reader>>,
}

#[pymethods]
impl FfmpegOpusAudio {
    #[new]
    #[pyo3(signature = (*_args, **_kwargs), text_signature = "(source, *, seek=None, headers=None, **options)")]
    fn new(
        _args: Bound<'_, PyTuple>,
        _kwargs: Option<Bound<'_, PyDict>>,
    ) -> PyClassInitializer<Self> {
        PyClassInitializer::from(AudioSource).add_subclass(Self {
            reader: Mutex::new(None),
        })
    }

    #[pyo3(signature = (source, *, seek=None, headers=None, **options))]
    fn __init__(
        slf: PyClassGuardMut<'_, Self>,
        py: Python<'_>,
        source: &Bound<'_, PyAny>,
        seek: Option<Py<PyAny>>,
        headers: Option<Py<PyAny>>,
        options: Option<Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        reject_cli_options(options)?;
        let reader = build_reader(py, source, seek, headers, true)?;
        *slf.reader.lock() = Some(reader);
        Ok(())
    }

    #[classattr]
    const NATIVE_AUDIO_SOURCE: bool = true;

    /// Open an Opus/PCM source without blocking the event loop.
    ///
    /// See :meth:`FFmpegPCMAudio.open`.
    #[staticmethod]
    #[pyo3(signature = (source, *, seek=None, headers=None, **options))]
    fn open<'py>(
        py: Python<'py>,
        source: Py<PyAny>,
        seek: Option<Py<PyAny>>,
        headers: Option<Py<PyAny>>,
        options: Option<Bound<'_, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        reject_cli_options(options)?;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            // The blocking HTTP client must not be created or dropped on a tokio worker: it
            // builds (and then drops) its own runtime, which panics in an async context. A
            // blocking pool thread is the right place for the probe.
            let reader = tokio::task::spawn_blocking(move || {
                Python::try_attach(|py| build_reader(py, source.bind(py), seek, headers, true))
                    .unwrap_or_else(|| {
                        Err(FFmpegSourceError::new_err(
                            "the interpreter is shutting down",
                        ))
                    })
            })
            .await
            .map_err(|err| FFmpegSourceError::new_err(err.to_string()))??;
            Python::attach(|py| {
                Ok::<_, PyErr>(
                    Py::new(
                        py,
                        PyClassInitializer::from(AudioSource).add_subclass(Self {
                            reader: Mutex::new(Some(reader)),
                        }),
                    )?
                    .into_any(),
                )
            })
        })
    }

    /// The codec name the demuxer reports, or `None` when the input cannot be read.
    #[staticmethod]
    fn probe_codec(py: Python<'_>, source: Py<PyAny>) -> Option<String> {
        let input = source::open_input(source.bind(py), &Default::default()).ok()?;
        let probe = py.detach(|| probe_source(input)).ok()??;
        Some(probe.0)
    }

    /// Probe `source` for its codec without decoding it.
    ///
    /// Returns `(codec, bitrate)`. There is no `ffprobe` process and no single bitrate in a
    /// transport stream, so the bitrate is always `None`.
    #[staticmethod]
    #[pyo3(signature = (source, headers=None))]
    fn probe<'py>(
        py: Python<'py>,
        source: Py<PyAny>,
        headers: Option<Py<PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let headers = validate_headers(py, source.bind(py), headers)?;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let probe = tokio::task::spawn_blocking(move || {
                Python::try_attach(|py| {
                    let input = source::open_input(source.bind(py), &headers)
                        .map_err(FFmpegSourceError::new_err)?;
                    py.detach(|| probe_source(input))
                        .map_err(FFmpegSourceError::new_err)
                })
                .unwrap_or_else(|| {
                    Err(FFmpegSourceError::new_err(
                        "the interpreter is shutting down",
                    ))
                })
            })
            .await
            .map_err(|err| FFmpegSourceError::new_err(err.to_string()))??;
            Ok::<_, PyErr>((probe.map(|probe| probe.0), Option::<u32>::None))
        })
    }

    fn __repr__(&self) -> String {
        "<discord_ext_voice.FFmpegOpusAudio>".to_owned()
    }
}

impl FfmpegPcmAudio {
    fn read_native(&self, py: Python<'_>) -> PyResult<Vec<u8>> {
        let mut reader = self.reader.lock();
        let reader = reader
            .as_mut()
            .ok_or_else(|| PyValueError::new_err("the source has not been initialized"))?;
        py.detach(|| reader.read())
            .map_err(FFmpegSourceError::new_err)
    }

    fn is_opus_native(&self) -> bool {
        false
    }

    fn cleanup_native(&self, py: Python<'_>) {
        py.detach(|| {
            if let Some(reader) = self.reader.lock().as_mut() {
                reader.cleanup();
            }
        });
    }
}

impl Drop for FfmpegPcmAudio {
    fn drop(&mut self) {
        if let Some(reader) = self.reader.lock().as_mut() {
            reader.cleanup();
        }
    }
}

impl FfmpegOpusAudio {
    fn read_native(&self, py: Python<'_>) -> PyResult<Vec<u8>> {
        let mut reader = self.reader.lock();
        let reader = reader
            .as_mut()
            .ok_or_else(|| PyValueError::new_err("the source has not been initialized"))?;
        py.detach(|| reader.read())
            .map_err(FFmpegSourceError::new_err)
    }

    fn is_opus_native(&self) -> bool {
        self.reader
            .lock()
            .as_ref()
            .map(Reader::is_opus)
            .unwrap_or(false)
    }

    fn cleanup_native(&self, py: Python<'_>) {
        py.detach(|| {
            if let Some(reader) = self.reader.lock().as_mut() {
                reader.cleanup();
            }
        });
    }
}

impl Drop for FfmpegOpusAudio {
    fn drop(&mut self) {
        if let Some(reader) = self.reader.lock().as_mut() {
            reader.cleanup();
        }
    }
}

/// A volume transformer for PCM sources, replacing `audioop.mul`.
///
/// This is a native `#[pyclass]`: the scaling itself is [`scale_pcm_s16`], and the wrapped
/// source is held as a `Py<PyAny>` only because it may be a user's Python source.
///
/// Python subclasses it (discord.py's own `yt-dlp` example does) and passes its own constructor
/// arguments, so the native `tp_new` accepts and ignores them; the actual initialisation happens
/// in the Rust `__init__` that the subclass calls through `super().__init__(source, volume)`.
#[pyclass(
    module = "discord_ext_voice._native",
    name = "PCMVolumeTransformer",
    extends = AudioSource,
    subclass,
    dict
)]
pub struct PcmVolumeTransformer {
    original: Mutex<Option<Py<PyAny>>>,
    volume: Mutex<f64>,
}

#[pymethods]
impl PcmVolumeTransformer {
    /// The object is built empty and filled in by `__init__`: a Python subclass's constructor
    /// may pass arguments of its own (`data=...` in the yt-dlp example) that this class must
    /// not reject.
    #[new]
    #[pyo3(signature = (*_args, **_kwargs), text_signature = "(original, volume=1.0)")]
    fn new(
        _args: Bound<'_, PyTuple>,
        _kwargs: Option<Bound<'_, PyDict>>,
    ) -> PyClassInitializer<Self> {
        PyClassInitializer::from(AudioSource).add_subclass(Self {
            original: Mutex::new(None),
            volume: Mutex::new(1.0),
        })
    }

    #[pyo3(signature = (original, volume=1.0))]
    fn __init__(
        slf: PyClassGuardMut<'_, Self>,
        py: Python<'_>,
        original: Py<PyAny>,
        volume: f64,
    ) -> PyResult<()> {
        let bound = original.bind(py);
        // This transformer is a Rust data path: the wrapped source has to be one of the native
        // source types, so `read`/`is_opus`/`cleanup` never cross into Python.
        let Some(opus) = native_is_opus(py, bound) else {
            let name = bound.get_type().name()?;
            return Err(PyTypeError::new_err(format!(
                "PCMVolumeTransformer requires a native audio source (PCMAudio, FFmpegPCMAudio, \
                 FFmpegOpusAudio or another PCMVolumeTransformer), not {name}"
            )));
        };
        if opus {
            return Err(discord_client_exception(
                py,
                "AudioSource must not be Opus encoded.",
            ));
        }

        *slf.original.lock() = Some(original);
        *slf.volume.lock() = volume.max(0.0);
        Ok(())
    }

    #[classattr]
    const NATIVE_AUDIO_SOURCE: bool = true;

    /// The wrapped source, as discord.py's `PCMVolumeTransformer.original` attribute.
    #[getter]
    fn original(&self, py: Python<'_>) -> Py<PyAny> {
        self.original
            .lock()
            .as_ref()
            .map(|source| source.clone_ref(py))
            .unwrap_or_else(|| py.None())
    }

    #[getter]
    fn volume(&self) -> f64 {
        *self.volume.lock()
    }

    #[setter]
    fn set_volume(&self, value: f64) {
        *self.volume.lock() = value.max(0.0);
    }

    fn __repr__(slf: PyRef<'_, Self>, py: Python<'_>) -> PyResult<String> {
        let volume = *slf.volume.lock();
        let original = slf
            .original
            .lock()
            .as_ref()
            .map(|source| source.clone_ref(py));
        let name = slf.into_pyobject(py)?.get_type().name()?.to_string();
        match original {
            Some(original) => Ok(format!(
                "<{name} volume={volume} original={}>",
                original.bind(py).repr()?
            )),
            None => Ok(format!("<{name} (uninitialized)>")),
        }
    }
}

impl PcmVolumeTransformer {
    fn is_opus_native(&self) -> bool {
        false
    }

    fn cleanup_native(&self, py: Python<'_>) {
        let original = self
            .original
            .lock()
            .as_ref()
            .map(|source| source.clone_ref(py));
        if let Some(original) = original {
            // The constructor guarantees a native source, so this never needs Python.
            if native_cleanup(py, original.bind(py)).is_none() {
                crate::log_debug!("the wrapped source is not a native audio source");
            }
        }
    }

    fn read_native(&self, py: Python<'_>) -> PyResult<Vec<u8>> {
        let original = self
            .original
            .lock()
            .as_ref()
            .map(|source| source.clone_ref(py));
        let Some(original) = original else {
            return Err(PyValueError::new_err(
                "PCMVolumeTransformer has not been initialized with a source",
            ));
        };

        // Dispatch to the native `read` when the wrapped source is exactly one of ours. A
        // Python subclass may override `read`, so only an exact type takes the fast path.
        let data = native_read(py, original.bind(py)).ok_or_else(|| {
            PyRuntimeError::new_err("the wrapped source is not a native audio source")
        })??;

        let volume = *self.volume.lock();
        // At 100% the transformation is the identity: hand the decoded `Vec` on unchanged.
        if volume == 1.0 {
            return Ok(data);
        }
        Ok(scale_pcm_s16(&data, volume.min(2.0) as f32))
    }
}

/// Call the native `read` of a native source type.
pub(crate) fn native_read(py: Python<'_>, source: &Bound<'_, PyAny>) -> Option<PyResult<Vec<u8>>> {
    macro_rules! dispatch {
        ($ty:ty) => {
            if source.is_instance_of::<$ty>() {
                return Some(source.cast::<$ty>().ok()?.borrow().read_native(py));
            }
        };
    }
    dispatch!(PcmAudio);
    dispatch!(FfmpegPcmAudio);
    dispatch!(FfmpegOpusAudio);
    dispatch!(PcmVolumeTransformer);
    None
}

/// Call the native `is_opus` of a native source type.
pub(crate) fn native_is_opus(py: Python<'_>, source: &Bound<'_, PyAny>) -> Option<bool> {
    let _ = py;
    macro_rules! dispatch {
        ($ty:ty) => {
            if source.is_instance_of::<$ty>() {
                return Some(source.cast::<$ty>().ok()?.borrow().is_opus_native());
            }
        };
    }
    dispatch!(PcmAudio);
    dispatch!(FfmpegPcmAudio);
    dispatch!(FfmpegOpusAudio);
    dispatch!(PcmVolumeTransformer);
    None
}

/// Call the native `cleanup` of a native source type.
pub(crate) fn native_cleanup(py: Python<'_>, source: &Bound<'_, PyAny>) -> Option<()> {
    macro_rules! dispatch {
        ($ty:ty) => {
            if source.is_instance_of::<$ty>() {
                source.cast::<$ty>().ok()?.borrow().cleanup_native(py);
                return Some(());
            }
        };
    }
    dispatch!(PcmAudio);
    dispatch!(FfmpegPcmAudio);
    dispatch!(FfmpegOpusAudio);
    dispatch!(PcmVolumeTransformer);
    None
}

/// Read one frame from a native source, for the test suite only.
///
/// This is compiled out of release wheels: the production API has no Python-side `read`.
#[cfg(debug_assertions)]
#[pyfunction]
pub fn read_frame<'py>(
    py: Python<'py>,
    source: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyBytes>> {
    match native_read(py, source) {
        Some(result) => Ok(PyBytes::new(py, &result?)),
        None => Err(PyTypeError::new_err(
            "not a native audio source; use PCMAudio, FFmpegPCMAudio, FFmpegOpusAudio or PCMVolumeTransformer",
        )),
    }
}

/// `source.is_opus()` without a Python method; used by tests and examples.
#[pyfunction]
pub fn is_audio_opus(py: Python<'_>, source: &Bound<'_, PyAny>) -> bool {
    native_is_opus(py, source).unwrap_or(false)
}

/// Release a native source's resources, for the test suite only.
#[cfg(debug_assertions)]
#[pyfunction]
pub fn cleanup_audio_source(py: Python<'_>, source: &Bound<'_, PyAny>) -> PyResult<()> {
    if native_cleanup(py, source).is_none() {
        return Err(PyTypeError::new_err("not a native audio source"));
    }
    Ok(())
}

impl Drop for PcmVolumeTransformer {
    fn drop(&mut self) {
        let original = self.original.get_mut().take();
        if let Some(original) = original {
            // The wrapped source is native by construction, so this does not enter Python.
            let _ = Python::try_attach(|py| {
                if native_cleanup(py, original.bind(py)).is_none() {
                    crate::log_debug!("the wrapped source is not a native audio source");
                }
            });
        }
    }
}

/// Probe a source without blocking the event loop.
///
/// Same result as the synchronous :func:`discord_ext_voice.probe`, but the demuxer runs on a
/// blocking thread. Useful before ``voice.play()`` when the input is an ``http(s)`` URL.
#[pyfunction]
#[pyo3(signature = (source, headers=None))]
pub fn probe_async<'py>(
    py: Python<'py>,
    source: Py<PyAny>,
    headers: Option<Py<PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let headers = validate_headers(py, source.bind(py), headers)?;
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        tokio::task::spawn_blocking(move || {
            Python::try_attach(|py| {
                let input = source::open_input(source.bind(py), &headers)
                    .map_err(FFmpegSourceError::new_err)?;
                py.detach(|| probe_source(input))
                    .map_err(FFmpegSourceError::new_err)
            })
            .unwrap_or_else(|| {
                Err(FFmpegSourceError::new_err(
                    "the interpreter is shutting down",
                ))
            })
        })
        .await
        .map_err(|err| FFmpegSourceError::new_err(err.to_string()))?
    })
}

/// Whether `candidate` is a native audio source that `play()` accepts.
///
/// The marker is a class attribute on the concrete native sources, so a Python subclass of
/// `PCMAudio` / `FFmpegPCMAudio` / `FFmpegOpusAudio` / `PCMVolumeTransformer` inherits it, while a
/// plain `AudioSource` subclass (an implementation written in Python) does not.
#[pyfunction]
pub fn is_audio_source(candidate: &Bound<'_, PyAny>) -> bool {
    candidate
        .getattr("NATIVE_AUDIO_SOURCE")
        .and_then(|marker| marker.is_truthy())
        .unwrap_or(false)
}

/// The reader behind an [`FfmpegPcmAudio`] or [`FfmpegOpusAudio`].
enum Reader {
    Decode(DecodeSource),
    Opus(OpusSource),
}

impl Reader {
    fn read(&mut self) -> Result<Vec<u8>, String> {
        match self {
            Self::Decode(reader) => reader.read_frame(),
            Self::Opus(reader) => reader.read_packet(),
        }
    }

    fn seek(&mut self, py: Python<'_>, seconds: f64) -> PyResult<()> {
        match self {
            Self::Decode(reader) => py
                .detach(|| reader.seek(seconds))
                .map_err(FFmpegSourceError::new_err),
            Self::Opus(reader) => py
                .detach(|| reader.seek(seconds))
                .map_err(FFmpegSourceError::new_err),
        }
    }

    fn is_opus(&self) -> bool {
        matches!(self, Self::Opus(_))
    }

    fn cleanup(&mut self) {
        match self {
            Self::Decode(reader) => reader.cleanup(),
            Self::Opus(reader) => reader.cleanup(),
        }
    }
}

/// Open the reader for a source, trying Opus pass-through first when the input can be reopened.
fn build_reader(
    py: Python<'_>,
    source: &Bound<'_, PyAny>,
    seek: Option<Py<PyAny>>,
    headers: Option<Py<PyAny>>,
    opus_first: bool,
) -> PyResult<Reader> {
    let seconds = validate_seek(py, seek)?;
    let headers = validate_headers(py, source, headers)?;

    let mut reader = None;
    if opus_first && (is_url(source) || is_path(source)) {
        // Opening the input twice is the price of the fallback, and it is only paid for a
        // path/URL: a file object would be consumed by the probe.
        if let Ok(opus) = open_opus(py, source, &headers) {
            reader = Some(Reader::Opus(opus));
        }
    }

    let mut reader = match reader {
        Some(reader) => reader,
        None => Reader::Decode(open_decode(py, source, &headers)?),
    };

    if let Some(seconds) = seconds {
        if let Err(err) = reader.seek(py, seconds) {
            reader.cleanup();
            return Err(err);
        }
    }
    Ok(reader)
}

fn open_decode(
    py: Python<'_>,
    source: &Bound<'_, PyAny>,
    headers: &reqwest::header::HeaderMap,
) -> PyResult<DecodeSource> {
    let input = source::open_input(source, headers).map_err(FFmpegSourceError::new_err)?;
    py.detach(|| DecodeSource::open(input))
        .map_err(FFmpegSourceError::new_err)
}

fn open_opus(
    py: Python<'_>,
    source: &Bound<'_, PyAny>,
    headers: &reqwest::header::HeaderMap,
) -> PyResult<OpusSource> {
    let input = source::open_input(source, headers).map_err(FFmpegSourceError::new_err)?;
    py.detach(|| OpusSource::open(input))
        .map_err(FFmpegSourceError::new_err)
}

fn reject_cli_options(options: Option<Bound<'_, PyDict>>) -> PyResult<()> {
    let Some(options) = options else {
        return Ok(());
    };
    if options.is_empty() {
        return Ok(());
    }

    let mut names: Vec<String> = options
        .keys()
        .iter()
        .filter_map(|key| key.extract::<String>().ok())
        .collect();
    names.sort();
    let plural = if names.len() > 1 { "s" } else { "" };
    Err(PyTypeError::new_err(format!(
        "unsupported argument{plural} {}: this package decodes in process with symphonia and \
         never runs an ffmpeg binary, so the FFmpeg CLI arguments of discord.py (before_options, \
         options, executable, pipe, stderr, force_cli) do not exist here. Pass the media directly \
         and use seek= instead of -ss.",
        names.join(", ")
    )))
}

/// Validate `seek=` before any native resource is opened.
fn validate_seek(py: Python<'_>, seek: Option<Py<PyAny>>) -> PyResult<Option<f64>> {
    let Some(seek) = seek else {
        return Ok(None);
    };
    let seek = seek.bind(py);
    if seek.is_none() {
        return Ok(None);
    }

    let numeric = !seek.is_instance_of::<PyBool>()
        && (seek.is_instance_of::<PyInt>() || seek.is_instance_of::<PyFloat>());
    match seek.extract::<f64>() {
        Ok(seconds) if numeric && seconds.is_finite() && seconds >= 0.0 => Ok(Some(seconds)),
        _ => Err(PyValueError::new_err(
            "the seek position must be a finite, non-negative number of seconds",
        )),
    }
}

/// Validate `headers=` and turn it into the header map the HTTP reader sends.
fn validate_headers(
    py: Python<'_>,
    source: &Bound<'_, PyAny>,
    headers: Option<Py<PyAny>>,
) -> PyResult<reqwest::header::HeaderMap> {
    let Some(headers) = headers else {
        return Ok(Default::default());
    };
    let headers = headers.bind(py);
    if headers.is_none() {
        return Ok(Default::default());
    }

    if !is_url(source) {
        return Err(PyTypeError::new_err(
            "headers= only applies to an http(s) URL: a local file or a file object is read \
             directly and would ignore them",
        ));
    }

    if !headers.hasattr("items").unwrap_or(false) {
        return Err(PyTypeError::new_err(format!(
            "headers must be a mapping of header names to values, not {}",
            headers.get_type().name()?
        )));
    }

    let mut validated = HashMap::new();
    for item in headers.call_method0("items")?.try_iter()? {
        let item = item?;
        let (name, value): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item.extract()?;
        let (Ok(name), Ok(value)) = (name.extract::<String>(), value.extract::<String>()) else {
            return Err(PyTypeError::new_err(
                "headers must be a mapping of str to str",
            ));
        };
        validated.insert(name, value);
    }
    parse_headers(Some(validated))
}

fn is_url(source: &Bound<'_, PyAny>) -> bool {
    source
        .extract::<String>()
        .map(|text| text.starts_with("http://") || text.starts_with("https://"))
        .unwrap_or(false)
}

fn is_path(source: &Bound<'_, PyAny>) -> bool {
    !is_url(source)
        && (source.is_instance_of::<PyString>() || source.hasattr("__fspath__").unwrap_or(false))
}

/// Raise discord.py's own `ClientException`, which is what a caller catches.
fn discord_client_exception(py: Python<'_>, message: &str) -> PyErr {
    let Ok(module) = py.import("discord.errors") else {
        return PyValueError::new_err(message.to_owned());
    };
    let Ok(class) = module.getattr("ClientException") else {
        return PyValueError::new_err(message.to_owned());
    };
    match class.call1((message,)) {
        Ok(instance) => PyErr::from_value(instance),
        Err(err) => err,
    }
}
