//! In-process audio decoding and resampling with `ac-ffmpeg` (libav*), exposed to Python as
//! `AudioSource`-compatible objects.
//!
//! Two flavours are provided:
//!
//! * [`PyDecodeSource`] decodes and resamples any input to 48kHz stereo `s16` and hands out
//!   exactly one 20ms RTP frame (3840 bytes) per `read()`.
//! * [`PyOpusSource`] demuxes a stream whose audio codec is already Opus and hands out the raw
//!   Opus packets, so no re-encoding is needed (`AudioSource.is_opus() is True`).
//!
//! Both accept either a filesystem path or a Python file-like object, which means a bot never
//! has to shell out to the `ffmpeg` binary for local media.

use std::collections::VecDeque;
use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom};
use std::path::PathBuf;

use ac_ffmpeg::codec::Decoder;
use ac_ffmpeg::codec::audio::AudioDecoder;
use ac_ffmpeg::codec::audio::frame::AudioFrame;
use ac_ffmpeg::format::demuxer::{Demuxer, DemuxerWithStreamInfo, InputFormat};
use ac_ffmpeg::format::io::IO;
use ac_ffmpeg::format::stream::Stream;
use parking_lot::Mutex;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyBytesMethods, PyDict};

use crate::errors::FFmpegError;
use crate::resample::StereoResampler;
use crate::{log_debug, log_info};

/// One 20ms frame of 48kHz stereo signed 16 bit PCM.
pub const FRAME_SAMPLES: usize = 960;
pub const FRAME_CHANNELS: usize = 2;
pub const FRAME_BYTES: usize = FRAME_SAMPLES * FRAME_CHANNELS * 2;
const TARGET_SAMPLE_RATE: u32 = 48_000;

fn ffmpeg_err(message: impl Into<String>) -> PyErr {
    FFmpegError::new_err(message.into())
}

/// A byte source for libav: either a real file or a Python file-like object.
pub enum Input {
    File(File),
    PyFile(PyFile),
}

impl Input {
    fn seekable(&self) -> bool {
        match self {
            Input::File(file) => file.metadata().map(|meta| meta.is_file()).unwrap_or(false),
            Input::PyFile(file) => file.seekable,
        }
    }
}

impl Read for Input {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self {
            Input::File(file) => file.read(buf),
            Input::PyFile(file) => file.read(buf),
        }
    }
}

impl Seek for Input {
    fn seek(&mut self, position: SeekFrom) -> io::Result<u64> {
        match self {
            Input::File(file) => file.seek(position),
            Input::PyFile(file) => file.seek(position),
        }
    }
}

/// Adapter that drives a Python file-like object from libav's IO callbacks.
pub struct PyFile {
    object: Py<PyAny>,
    seekable: bool,
}

impl PyFile {
    fn new(object: Py<PyAny>, seekable: bool) -> Self {
        Self { object, seekable }
    }
}

fn py_io_error(err: PyErr) -> io::Error {
    io::Error::other(err.to_string())
}

impl Read for PyFile {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if buf.is_empty() {
            return Ok(0);
        }
        Python::attach(|py| {
            let object = self.object.bind(py);
            let result = object
                .call_method1("read", (buf.len(),))
                .map_err(py_io_error)?;
            if result.is_none() {
                return Ok(0);
            }
            let data: Vec<u8> = result.extract().map_err(py_io_error)?;
            let copied = data.len().min(buf.len());
            buf[..copied].copy_from_slice(&data[..copied]);
            Ok(copied)
        })
    }
}

impl Seek for PyFile {
    fn seek(&mut self, position: SeekFrom) -> io::Result<u64> {
        if !self.seekable {
            return Err(io::Error::other("the stream is not seekable"));
        }
        Python::attach(|py| {
            let object = self.object.bind(py);
            let (offset, whence) = match position {
                SeekFrom::Start(offset) => (offset as i64, 0),
                SeekFrom::Current(offset) => (offset, 1),
                SeekFrom::End(offset) => (offset, 2),
            };
            let result = object
                .call_method1("seek", (offset, whence))
                .map_err(py_io_error)?;
            if let Ok(position) = result.extract::<u64>() {
                return Ok(position);
            }
            let tell = object.call_method0("tell").map_err(py_io_error)?;
            tell.extract::<u64>().map_err(py_io_error)
        })
    }
}

/// Turn a Python object into libav input.
fn open_input(source: &Bound<'_, PyAny>) -> PyResult<Input> {
    if let Ok(path) = source.extract::<PathBuf>() {
        let file = File::open(&path)
            .map_err(|err| ffmpeg_err(format!("unable to open {}: {err}", path.display())))?;
        return Ok(Input::File(file));
    }

    if source.hasattr("read").unwrap_or(false) {
        let seekable = source
            .call_method0("seekable")
            .ok()
            .and_then(|value| value.is_truthy().ok())
            .unwrap_or(false);
        return Ok(Input::PyFile(PyFile::new(
            source.clone().unbind(),
            seekable,
        )));
    }

    Err(ffmpeg_err(
        "the audio source must be a file path or a file-like object with a read() method",
    ))
}

struct DemuxedInput {
    demuxer: DemuxerWithStreamInfo<Input>,
    stream_index: usize,
}

impl DemuxedInput {
    fn stream(&self) -> &Stream {
        &self.demuxer.streams()[self.stream_index]
    }
}

fn demux(
    source: &Bound<'_, PyAny>,
    format: Option<&str>,
    options: Option<&Bound<'_, PyDict>>,
) -> PyResult<DemuxedInput> {
    let input = open_input(source)?;
    let seekable = input.seekable();

    let io = if seekable {
        IO::from_seekable_read_stream(input)
    } else {
        IO::from_read_stream(input)
    };

    let mut builder = Demuxer::builder();
    if let Some(format) = format
        && let Some(input_format) = InputFormat::find_by_name(format)
    {
        builder = builder.input_format(Some(input_format));
    }
    if let Some(options) = options {
        for (key, value) in options.iter() {
            let key: String = key.extract()?;
            let value: String = value.str()?.to_string_lossy().into_owned();
            builder = builder.set_option(key.as_str(), value);
        }
    }

    let demuxer = builder
        .build(io)
        .map_err(|err| ffmpeg_err(format!("FFmpeg could not open the input: {err}")))?;
    let demuxer = demuxer.find_stream_info(None).map_err(|(_, err)| {
        ffmpeg_err(format!("FFmpeg could not read stream information: {err}"))
    })?;

    let stream_index = demuxer
        .streams()
        .iter()
        .position(|candidate| candidate.codec_parameters().is_audio_codec())
        .ok_or_else(|| ffmpeg_err("the input contains no audio stream"))?;

    log_debug!("demuxed input with audio stream at index {stream_index}");

    Ok(DemuxedInput {
        demuxer,
        stream_index,
    })
}

/// A decoding audio source: libav input -> decoder -> resampler -> 20ms PCM frames.
pub struct DecodeSource {
    demuxer: Demuxer<Input>,
    stream_index: usize,
    decoder: AudioDecoder,
    resampler: Option<StereoResampler>,
    pending: VecDeque<u8>,
    /// Samples per channel that still need to be discarded (`-ss`).
    skip_samples: u64,
    /// Samples per channel left before the source ends (`-t`).
    remaining_samples: Option<u64>,
    input_done: bool,
    flushed: bool,
    finished: bool,
}

// SAFETY: libav contexts are plain C structs with no thread affinity. `DecodeSource` is only
// ever touched while its enclosing `parking_lot::Mutex` is held, so it is never used from two
// threads at once. `ac-ffmpeg` itself asserts the same property for its decoder and resampler
// types.
unsafe impl Send for DecodeSource {}

impl DecodeSource {
    fn open(
        source: &Bound<'_, PyAny>,
        format: Option<&str>,
        options: Option<&Bound<'_, PyDict>>,
        start_time: Option<f64>,
        end_time: Option<f64>,
    ) -> PyResult<Self> {
        let demuxed = demux(source, format, options)?;
        let stream_index = demuxed.stream_index;

        let decoder = AudioDecoder::from_stream(demuxed.stream())
            .and_then(|builder| builder.build())
            .map_err(|err| ffmpeg_err(format!("FFmpeg could not open the audio decoder: {err}")))?;

        let demuxer = demuxed.demuxer.into_demuxer();

        // Offsets are honoured by discarding decoded samples, which keeps the source usable
        // for non-seekable inputs (pipes, sockets, ...) as well.
        let skip_samples = start_time.map(|s| (s.max(0.0) * f64::from(TARGET_SAMPLE_RATE)) as u64);
        let remaining_samples = match (start_time, end_time) {
            (_, Some(end)) => {
                let end_samples = (end.max(0.0) * f64::from(TARGET_SAMPLE_RATE)) as u64;
                let start_samples = start_time
                    .map(|s| (s.max(0.0) * f64::from(TARGET_SAMPLE_RATE)) as u64)
                    .unwrap_or(0);
                Some(end_samples.saturating_sub(start_samples))
            }
            _ => None,
        };

        if let Some(offset) = start_time
            && offset > 0.0
        {
            log_debug!("skipping {offset}s of decoded audio");
        }

        Ok(Self {
            demuxer,
            stream_index,
            decoder,
            resampler: None,
            pending: VecDeque::new(),
            skip_samples: skip_samples.unwrap_or(0),
            remaining_samples,
            input_done: false,
            flushed: false,
            finished: false,
        })
    }

    /// Feed one decoded frame through the resampler and buffer the resulting stereo PCM.
    fn push_decoded(&mut self, frame: AudioFrame) -> Result<(), PyErr> {
        let format_name = frame.sample_format().name();
        let sample_rate = frame.sample_rate();
        let channels = frame.channel_layout().channels();

        let rebuild = match self.resampler.as_ref() {
            None => true,
            Some(resampler) => !resampler.matches(format_name, sample_rate, channels),
        };
        if rebuild {
            log_debug!(
                "building a resampler: {format_name} {sample_rate}Hz {channels}ch -> s16 48000Hz stereo"
            );
            self.resampler = Some(
                StereoResampler::new(format_name, sample_rate, channels, TARGET_SAMPLE_RATE)
                    .map_err(ffmpeg_err)?,
            );
        }

        let samples = frame.samples();
        let planes = frame.planes();
        let borrowed: Vec<&[u8]> = planes.iter().map(|plane| plane.data()).collect();
        let mut converted = Vec::new();

        let Some(resampler) = self.resampler.as_mut() else {
            return Ok(());
        };
        resampler
            .convert(&borrowed, samples, &mut converted)
            .map_err(ffmpeg_err)?;

        if converted.is_empty() {
            return Ok(());
        }
        self.queue_pcm(&converted);
        Ok(())
    }

    /// Apply the `-ss`/`-t` window to freshly converted stereo PCM.
    fn queue_pcm(&mut self, data: &[u8]) {
        let bytes_per_sample = (FRAME_CHANNELS * 2) as u64;
        let mut data = data;

        if self.skip_samples > 0 {
            let skip_bytes = (self.skip_samples * bytes_per_sample).min(data.len() as u64) as usize;
            self.skip_samples -= skip_bytes as u64 / bytes_per_sample;
            data = &data[skip_bytes..];
        }

        if data.is_empty() {
            return;
        }

        if let Some(remaining) = self.remaining_samples.as_mut() {
            let allowed_bytes = (*remaining * bytes_per_sample).min(data.len() as u64) as usize;
            *remaining -= allowed_bytes as u64 / bytes_per_sample;
            data = &data[..allowed_bytes];
            if data.is_empty() {
                self.finished = true;
                return;
            }
        }

        self.pending.extend(data.iter().copied());
    }

    /// Advance the pipeline until at least one 20ms frame is buffered or the input ends.
    fn pump(&mut self) -> Result<(), PyErr> {
        loop {
            if self.pending.len() >= FRAME_BYTES || self.finished {
                return Ok(());
            }

            if let Some(frame) = self
                .decoder
                .take()
                .map_err(|err| ffmpeg_err(format!("FFmpeg decoder error: {err}")))?
            {
                self.push_decoded(frame)?;
                continue;
            }

            if self.input_done {
                if !self.flushed {
                    self.flushed = true;
                    self.decoder
                        .flush()
                        .map_err(|err| ffmpeg_err(format!("FFmpeg decoder flush failed: {err}")))?;
                    if let Some(resampler) = self.resampler.as_mut() {
                        let mut converted = Vec::new();
                        resampler.flush(&mut converted).map_err(ffmpeg_err)?;
                        self.queue_pcm(&converted);
                    }
                    continue;
                }
                self.finished = true;
                return Ok(());
            }

            match self.demuxer.take() {
                Ok(Some(packet)) => {
                    if packet.stream_index() == self.stream_index {
                        self.decoder.try_push(packet).map_err(|err| {
                            ffmpeg_err(format!(
                                "FFmpeg could not decode an audio packet: {}",
                                err.into_inner()
                                    .map(|inner| inner.to_string())
                                    .unwrap_or_else(|| "the decoder needs to be drained".to_owned())
                            ))
                        })?;
                    }
                }
                Ok(None) => self.input_done = true,
                Err(err) => return Err(ffmpeg_err(format!("FFmpeg demux error: {err}"))),
            }
        }
    }

    fn read_frame(&mut self) -> Result<Vec<u8>, PyErr> {
        if self.finished && self.pending.is_empty() {
            return Ok(Vec::new());
        }

        while self.pending.len() < FRAME_BYTES && !self.finished {
            self.pump()?;
        }

        if self.pending.is_empty() {
            self.finished = true;
            return Ok(Vec::new());
        }

        let take = self.pending.len().min(FRAME_BYTES);
        Ok(self.pending.drain(..take).collect())
    }
}

/// A pass-through source that yields the raw Opus packets of an already-encoded stream.
pub struct OpusSource {
    demuxer: Demuxer<Input>,
    stream_index: usize,
    finished: bool,
}

// SAFETY: see the note on `DecodeSource`.
unsafe impl Send for OpusSource {}

impl OpusSource {
    fn open(
        source: &Bound<'_, PyAny>,
        format: Option<&str>,
        options: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        let demuxed = demux(source, format, options)?;
        let stream_index = demuxed.stream_index;

        let codec = demuxed
            .stream()
            .codec_parameters()
            .decoder_name()
            .unwrap_or("unknown")
            .to_owned();
        if !codec.starts_with("opus") && codec != "libopus" {
            return Err(ffmpeg_err(format!(
                "the audio codec is {codec}, not opus; an Opus pass-through source requires an Opus stream"
            )));
        }
        log_info!("using the Opus packet pass-through source (codec {codec})");

        Ok(Self {
            demuxer: demuxed.demuxer.into_demuxer(),
            stream_index,
            finished: false,
        })
    }

    fn read_packet(&mut self) -> Result<Vec<u8>, PyErr> {
        if self.finished {
            return Ok(Vec::new());
        }
        loop {
            match self.demuxer.take() {
                Ok(Some(packet)) => {
                    if packet.stream_index() == self.stream_index && !packet.data().is_empty() {
                        return Ok(packet.data().to_vec());
                    }
                }
                Ok(None) => {
                    self.finished = true;
                    return Ok(Vec::new());
                }
                Err(err) => return Err(ffmpeg_err(format!("FFmpeg demux error: {err}"))),
            }
        }
    }
}

/// What [`probe_source`] reports: codec name, sample rate, channel count and duration.
pub type ProbeResult = Option<(String, u32, usize, Option<f64>)>;

/// Probe a source and report `(codec, sample_rate, channels, duration_seconds)` without
/// decoding anything.
pub fn probe_source(source: &Bound<'_, PyAny>, format: Option<&str>) -> PyResult<ProbeResult> {
    let demuxed = demux(source, format, None)?;
    let stream = demuxed.stream();

    let parameters = stream.codec_parameters();
    let codec = parameters.decoder_name().unwrap_or("unknown").to_owned();
    let audio = parameters.as_audio_codec_parameters();
    let sample_rate = audio.map(|params| params.sample_rate()).unwrap_or(0);
    let channels = audio
        .map(|params| params.channel_layout().channels() as usize)
        .unwrap_or(0);

    let duration = stream.duration();
    let time_base = stream.time_base();
    let seconds = if duration.is_null() {
        None
    } else {
        let raw = duration.timestamp() as f64;
        let (num, den) = (f64::from(time_base.num()), f64::from(time_base.den()));
        let value = if den == 0.0 { 0.0 } else { raw * num / den };
        (value > 0.0).then_some(value)
    };

    Ok(Some((codec, sample_rate, channels, seconds)))
}

/// Python-visible decoding audio source.
#[pyclass(module = "discord_ext_voice._native", name = "DecodeSource")]
pub struct PyDecodeSource {
    inner: Mutex<DecodeSource>,
}

#[pymethods]
impl PyDecodeSource {
    #[new]
    #[pyo3(signature = (source, *, format=None, start_time=None, end_time=None, options=None))]
    fn new(
        source: &Bound<'_, PyAny>,
        format: Option<String>,
        start_time: Option<f64>,
        end_time: Option<f64>,
        options: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        let inner = DecodeSource::open(source, format.as_deref(), options, start_time, end_time)?;
        Ok(Self {
            inner: Mutex::new(inner),
        })
    }

    /// Read the next 20ms frame of 48kHz stereo signed 16 bit PCM.
    fn read<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let data = self.inner.lock().read_frame()?;
        Ok(PyBytes::new(py, &data))
    }

    fn is_opus(&self) -> bool {
        false
    }

    /// Release the decoder, demuxer and any FFmpeg resources immediately.
    fn cleanup(&self, py: Python<'_>) {
        py.detach(|| {
            let mut guard = self.inner.lock();
            guard.finished = true;
            guard.pending.clear();
            guard.resampler = None;
        });
    }

    fn __repr__(&self) -> String {
        "<discord_ext_voice.DecodeSource (48kHz stereo s16)>".to_owned()
    }
}

/// Python-visible raw Opus packet source.
#[pyclass(module = "discord_ext_voice._native", name = "OpusSource")]
pub struct PyOpusSource {
    inner: Mutex<OpusSource>,
}

#[pymethods]
impl PyOpusSource {
    #[new]
    #[pyo3(signature = (source, *, format=None, options=None))]
    fn new(
        source: &Bound<'_, PyAny>,
        format: Option<String>,
        options: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        let inner = OpusSource::open(source, format.as_deref(), options)?;
        Ok(Self {
            inner: Mutex::new(inner),
        })
    }

    /// Read one raw Opus packet.
    fn read<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let data = self.inner.lock().read_packet()?;
        Ok(PyBytes::new(py, &data))
    }

    fn is_opus(&self) -> bool {
        true
    }

    fn cleanup(&self, py: Python<'_>) {
        py.detach(|| {
            self.inner.lock().finished = true;
        });
    }

    fn __repr__(&self) -> String {
        "<discord_ext_voice.OpusSource (raw Opus packets)>".to_owned()
    }
}

/// Probe a media source without decoding it.
#[pyfunction]
#[pyo3(signature = (source, *, format=None))]
pub fn probe(source: &Bound<'_, PyAny>, format: Option<String>) -> PyResult<ProbeResult> {
    probe_source(source, format.as_deref())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_geometry_matches_discord() {
        assert_eq!(FRAME_SAMPLES, 960);
        assert_eq!(FRAME_BYTES, 3840);
    }

    #[test]
    fn empty_buffer_read_is_zero() {
        let mut file = File::open("/dev/null").unwrap();
        let mut buf = [0u8; 8];
        assert_eq!(file.read(&mut buf).unwrap(), 0);
    }
}

/// Applies a volume multiplier to another `AudioSource`, replacing `discord.py`'s
/// `PCMVolumeTransformer` (which relied on the removed `audioop` module).
#[pyclass(module = "discord_ext_voice._native", name = "VolumeTransformer")]
pub struct PyVolumeTransformer {
    original: Py<PyAny>,
    volume: Mutex<f64>,
}

#[pymethods]
impl PyVolumeTransformer {
    #[new]
    #[pyo3(signature = (original, volume=1.0))]
    fn new(original: Py<PyAny>, volume: f64) -> Self {
        Self {
            original,
            volume: Mutex::new(volume.max(0.0)),
        }
    }

    #[getter]
    fn volume(&self) -> f64 {
        *self.volume.lock()
    }

    #[setter]
    fn set_volume(&self, value: f64) {
        *self.volume.lock() = value.max(0.0);
    }

    /// Read from the wrapped source and scale every sample.
    fn read<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let original = self.original.bind(py);
        let data = original.call_method0("read")?;
        let bytes: Vec<u8> = if let Ok(bytes) = data.cast::<PyBytes>() {
            bytes.as_bytes().to_vec()
        } else {
            data.extract()?
        };

        // `discord.py` clamps the applied factor at 2.0 and `audioop.mul` saturates.
        let factor = self.volume.lock().min(2.0);
        if (factor - 1.0).abs() < f64::EPSILON {
            return Ok(PyBytes::new(py, &bytes));
        }

        let mut scaled = Vec::with_capacity(bytes.len());
        for chunk in bytes.chunks_exact(2) {
            let sample = i16::from_le_bytes([chunk[0], chunk[1]]);
            let value =
                (f64::from(sample) * factor).clamp(f64::from(i16::MIN), f64::from(i16::MAX));
            scaled.extend_from_slice(&(value as i16).to_le_bytes());
        }
        if bytes.len() % 2 == 1 {
            scaled.push(bytes[bytes.len() - 1]);
        }
        Ok(PyBytes::new(py, &scaled))
    }

    fn is_opus(&self, py: Python<'_>) -> PyResult<bool> {
        self.original.bind(py).call_method0("is_opus")?.extract()
    }

    fn cleanup(&self, py: Python<'_>) -> PyResult<()> {
        let original = self.original.bind(py);
        if original.hasattr("cleanup")? {
            original.call_method0("cleanup")?;
        }
        Ok(())
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        Ok(format!(
            "<discord_ext_voice.VolumeTransformer volume={} original={:?}>",
            self.volume(),
            self.original.bind(py)
        ))
    }
}
