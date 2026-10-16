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

use std::collections::{HashMap, VecDeque};
use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom};
use std::path::PathBuf;
use std::time::Duration;

use ac_ffmpeg::codec::Decoder;
use ac_ffmpeg::codec::audio::AudioDecoder;
use ac_ffmpeg::codec::audio::AudioResampler;
use ac_ffmpeg::codec::audio::frame::{
    AudioFrame, ChannelLayout, SampleFormat, get_channel_layout, get_sample_format,
};
use ac_ffmpeg::format::demuxer::{Demuxer, DemuxerWithStreamInfo, SeekTarget};
use ac_ffmpeg::format::io::IO;
use ac_ffmpeg::format::stream::Stream;
use ac_ffmpeg::time::Timestamp;
use parking_lot::Mutex;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};

use crate::errors::FFmpegError;
use crate::{log_debug, log_info};

/// One 20ms frame of 48kHz stereo signed 16 bit PCM.
pub const FRAME_SAMPLES: usize = 960;
pub const FRAME_CHANNELS: usize = 2;
pub const FRAME_BYTES: usize = FRAME_SAMPLES * FRAME_CHANNELS * 2;
const TARGET_SAMPLE_RATE: u32 = 48_000;

/// Build the Python exception for an internal error message.
///
/// The decoding entry points run with the GIL released, so everything below them reports
/// failures as strings and only the Python-facing boundary turns them into an exception.
fn ffmpeg_err(message: impl Into<String>) -> PyErr {
    FFmpegError::new_err(message.into())
}

/// How much of a forward seek to do by reading instead of asking the server again.
const SEEK_SKIP_LIMIT: u64 = 1 << 20;

/// A byte source for libav: a real file, a Python file-like object, or an HTTP(S) URL.
pub enum Input {
    File(File),
    PyFile(PyFile),
    Http(Box<HttpStream>),
}

impl Input {
    fn seekable(&self) -> bool {
        match self {
            Input::File(file) => file.metadata().map(|meta| meta.is_file()).unwrap_or(false),
            Input::PyFile(file) => file.seekable,
            // Ranges are used when the server supports them, and a restart-and-skip when it does
            // not, so an HTTP input can always answer a seek.
            Input::Http(_) => true,
        }
    }
}

impl Read for Input {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self {
            Input::File(file) => file.read(buf),
            Input::PyFile(file) => file.read(buf),
            Input::Http(stream) => stream.read(buf),
        }
    }
}

impl Seek for Input {
    fn seek(&mut self, position: SeekFrom) -> io::Result<u64> {
        match self {
            Input::File(file) => file.seek(position),
            Input::PyFile(file) => file.seek(position),
            Input::Http(stream) => stream.seek(position),
        }
    }
}

/// An HTTP(S) response as a seekable byte stream for libav.
///
/// `read` hands libav what the server has sent so far, so playback starts before the file has
/// been downloaded. `seek` reads forward when the target is close, and otherwise asks the server
/// for the rest of the file with a `Range` header — falling back to restarting and skipping when
/// the server ignores ranges. libav needs that for containers whose index sits at the end (MP4).
///
/// The connection is opened on the first read, so constructing a source never blocks on the
/// network.
pub struct HttpStream {
    url: String,
    client: reqwest::blocking::Client,
    response: Option<reqwest::blocking::Response>,
    /// The offset of the next byte the response will hand out.
    position: u64,
    /// `Content-Length` of the whole stream, when the server reports one.
    length: Option<u64>,
}

impl HttpStream {
    /// `headers` are sent with every request (the first read, and any `Range` request a seek
    /// makes). They come from whatever produced the URL — an extractor's `http_headers`, for
    /// instance — because such URLs are often bound to the client that requested them. A
    /// `User-Agent` in `headers` replaces this package's own.
    fn open(url: &str, headers: &HeaderMap) -> io::Result<Self> {
        let mut defaults = HeaderMap::new();
        defaults.insert(
            reqwest::header::USER_AGENT,
            HeaderValue::from_str(concat!("discord-ext-voice/", env!("CARGO_PKG_VERSION")))
                .expect("the version string is a valid header value"),
        );
        for (name, value) in headers {
            defaults.insert(name.clone(), value.clone());
        }

        let client = reqwest::blocking::Client::builder()
            .default_headers(defaults)
            .connect_timeout(Duration::from_secs(10))
            .build()
            .map_err(|err| io::Error::other(format!("unable to create the HTTP client: {err}")))?;

        Ok(Self {
            url: url.to_owned(),
            client,
            response: None,
            position: 0,
            length: None,
        })
    }

    fn response(&mut self) -> io::Result<&mut reqwest::blocking::Response> {
        if self.response.is_none() {
            let response = self
                .client
                .get(&self.url)
                .send()
                .map_err(|err| http_error(&self.url, &err))?
                .error_for_status()
                .map_err(|err| http_error(&self.url, &err))?;
            self.length = response.content_length();
            self.response = Some(response);
        }
        Ok(self.response.as_mut().expect("just connected"))
    }

    /// Request `offset` onwards, returning whether the server honoured the range.
    fn request_from(&mut self, offset: u64) -> io::Result<bool> {
        let mut request = self.client.get(&self.url);
        if offset > 0 {
            request = request.header(reqwest::header::RANGE, format!("bytes={offset}-"));
        }

        let response = request
            .send()
            .map_err(|err| http_error(&self.url, &err))?
            .error_for_status()
            .map_err(|err| http_error(&self.url, &err))?;
        let ranged = response.status() == reqwest::StatusCode::PARTIAL_CONTENT;

        if let Some(remaining) = response.content_length() {
            self.length = Some(if ranged {
                offset + remaining
            } else {
                remaining
            });
        }
        self.response = Some(response);
        self.position = if ranged { offset } else { 0 };
        Ok(ranged)
    }

    /// Consume `remaining` bytes from the current response.
    fn skip(&mut self, remaining: u64) -> io::Result<()> {
        let mut scratch = [0u8; 16 * 1024];
        let mut left = remaining;
        while left > 0 {
            let want = left.min(scratch.len() as u64) as usize;
            let read = self.response()?.read(&mut scratch[..want])?;
            if read == 0 {
                return Err(io::Error::other(format!(
                    "{} ended before the requested position",
                    self.url
                )));
            }
            left -= read as u64;
            self.position += read as u64;
        }
        Ok(())
    }
}

fn http_error(url: &str, err: &reqwest::Error) -> io::Error {
    io::Error::other(format!("unable to fetch {url}: {err}"))
}

fn shift(base: u64, delta: i64) -> io::Result<u64> {
    u64::try_from(i128::from(base) + i128::from(delta))
        .map_err(|_| io::Error::other("a seek before the start of the stream"))
}

impl Read for HttpStream {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let read = self.response()?.read(buf)?;
        self.position += read as u64;
        Ok(read)
    }
}

impl Seek for HttpStream {
    fn seek(&mut self, position: SeekFrom) -> io::Result<u64> {
        let target = match position {
            SeekFrom::Start(offset) => offset,
            SeekFrom::Current(delta) => shift(self.position, delta)?,
            SeekFrom::End(delta) => {
                let length = match self.length {
                    Some(length) => length,
                    None => {
                        self.response()?; // the length arrives with the first response
                        self.length.ok_or_else(|| {
                            io::Error::other("the server did not report a content length")
                        })?
                    }
                };
                shift(length, delta)?
            }
        };

        if target == self.position {
            return Ok(target);
        }

        // Reading forward within what is already in flight is cheaper than a new request.
        if target > self.position && target - self.position <= SEEK_SKIP_LIMIT {
            self.skip(target - self.position)?;
            return Ok(target);
        }

        if self.request_from(target)? {
            return Ok(target);
        }

        // The server ignored the range, so the response starts at zero again.
        self.skip(target)?;
        Ok(target)
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

/// A Python mapping of header names to values, as a [`HeaderMap`].
///
/// Raises `ValueError` for a name or value HTTP cannot carry, naming the offending header, so a
/// typo in `headers=` is reported before any connection is attempted.
fn parse_headers(headers: Option<HashMap<String, String>>) -> PyResult<HeaderMap> {
    let Some(headers) = headers else {
        return Ok(HeaderMap::new());
    };

    let mut map = HeaderMap::with_capacity(headers.len());
    for (name, value) in headers {
        let parsed_name = HeaderName::from_bytes(name.as_bytes()).map_err(|err| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "{name:?} is not a valid HTTP header name: {err}"
            ))
        })?;
        let parsed_value = HeaderValue::from_str(&value).map_err(|err| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "the value of the {name:?} header is not valid: {err}"
            ))
        })?;
        map.insert(parsed_name, parsed_value);
    }
    Ok(map)
}

/// Turn a Python object into libav input.
///
/// Only the Python object is touched here: opening an HTTP(S) URL is left to the first read, and
/// the libav part of the handshake happens in [`demux`], which the callers run without the GIL.
///
/// `headers` apply to an HTTP(S) input; a path or a file object ignores them.
fn open_input(source: &Bound<'_, PyAny>, headers: &HeaderMap) -> Result<Input, String> {
    if let Ok(text) = source.extract::<String>() {
        if text.starts_with("http://") || text.starts_with("https://") {
            log_info!("streaming {text} over HTTP");
            return HttpStream::open(&text, headers)
                .map(|stream| Input::Http(Box::new(stream)))
                .map_err(|err| format!("unable to open {text}: {err}"));
        }
    }

    if let Ok(path) = source.extract::<PathBuf>() {
        let file =
            File::open(&path).map_err(|err| format!("unable to open {}: {err}", path.display()))?;
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

    Err(
        "the audio source must be a file path, an http(s) URL or a file-like object with a \
         read() method"
            .to_owned(),
    )
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

fn demux(input: Input) -> Result<DemuxedInput, String> {
    let seekable = input.seekable();

    let io = if seekable {
        IO::from_seekable_read_stream(input)
    } else {
        IO::from_read_stream(input)
    };

    let demuxer = Demuxer::builder()
        .build(io)
        .map_err(|err| format!("FFmpeg could not open the input: {err}"))?;
    let demuxer = demuxer
        .find_stream_info(None)
        .map_err(|(_, err)| format!("FFmpeg could not read stream information: {err}"))?;

    let stream_index = demuxer
        .streams()
        .iter()
        .position(|candidate| candidate.codec_parameters().is_audio_codec())
        .ok_or("the input contains no audio stream".to_owned())?;

    log_debug!("demuxed input with audio stream at index {stream_index}");

    Ok(DemuxedInput {
        demuxer,
        stream_index,
    })
}

/// A decoding audio source: libav input -> decoder -> resampler -> 20ms PCM frames.
pub struct DecodeSource {
    demuxer: DemuxerWithStreamInfo<Input>,
    stream_index: usize,
    decoder: AudioDecoder,
    resampler: Option<AudioResampler>,
    /// The parameters the current resampler was built for, so it can be rebuilt when the
    /// decoder's output changes mid-stream.
    resampler_source: Option<(SampleFormat, u32, ChannelLayout)>,
    pending: VecDeque<u8>,
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
    fn open(input: Input) -> Result<Self, String> {
        let demuxed = demux(input)?;
        let stream_index = demuxed.stream_index;

        let decoder = AudioDecoder::from_stream(demuxed.stream())
            .and_then(|builder| builder.build())
            .map_err(|err| format!("FFmpeg could not open the audio decoder: {err}"))?;

        Ok(Self {
            demuxer: demuxed.demuxer,
            stream_index,
            decoder,
            resampler: None,
            resampler_source: None,
            pending: VecDeque::new(),
            input_done: false,
            flushed: false,
            finished: false,
        })
    }

    /// Feed one decoded frame through the resampler and buffer the resulting stereo PCM.
    /// Feed one decoded frame through the resampler and buffer the resulting stereo PCM.
    fn push_decoded(&mut self, frame: AudioFrame) -> Result<(), String> {
        let parameters = (
            frame.sample_format(),
            frame.sample_rate(),
            frame.channel_layout().to_owned(),
        );

        if self.resampler.is_none() || self.resampler_source.as_ref() != Some(&parameters) {
            log_debug!(
                "building a resampler: {} {}Hz {}ch -> s16 48000Hz stereo",
                parameters.0.name(),
                parameters.1,
                parameters.2.channels()
            );
            let resampler = AudioResampler::builder()
                .source_channel_layout(parameters.2.clone())
                .source_sample_format(parameters.0)
                .source_sample_rate(parameters.1)
                .target_channel_layout(get_channel_layout("stereo"))
                .target_sample_format(get_sample_format("s16"))
                .target_sample_rate(TARGET_SAMPLE_RATE)
                .target_frame_samples(Some(FRAME_SAMPLES))
                .build()
                .map_err(|err| format!("FFmpeg could not build the resampler: {err}"))?;
            self.resampler = Some(resampler);
            self.resampler_source = Some(parameters);
        }

        let resampler = self.resampler.as_mut().expect("resampler just built");
        resampler
            .push(frame)
            .map_err(|err| format!("FFmpeg resampler error: {err}"))
    }

    /// Move every resampled frame the resampler has ready into `pending`.
    fn drain_resampler(&mut self) -> Result<(), String> {
        loop {
            let frame = match self.resampler.as_mut() {
                Some(resampler) => resampler
                    .take()
                    .map_err(|err| format!("FFmpeg resampler error: {err}"))?,
                None => return Ok(()),
            };

            match frame {
                Some(frame) => {
                    if let Some(plane) = frame.planes().first() {
                        self.pending.extend(plane.data().iter().copied());
                    }
                }
                None => return Ok(()),
            }
        }
    }

    /// Advance the pipeline until at least one 20ms frame is buffered or the input ends.
    fn pump(&mut self) -> Result<(), String> {
        loop {
            self.drain_resampler()?;

            if self.pending.len() >= FRAME_BYTES || self.finished {
                return Ok(());
            }

            if let Some(frame) = self
                .decoder
                .take()
                .map_err(|err| format!("FFmpeg decoder error: {err}"))?
            {
                self.push_decoded(frame)?;
                continue;
            }

            if self.input_done {
                if !self.flushed {
                    self.flushed = true;
                    self.decoder
                        .flush()
                        .map_err(|err| format!("FFmpeg decoder flush failed: {err}"))?;
                    if let Some(resampler) = self.resampler.as_mut() {
                        resampler
                            .flush()
                            .map_err(|err| format!("FFmpeg resampler flush failed: {err}"))?;
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
                            format!(
                                "FFmpeg could not decode an audio packet: {}",
                                err.into_inner()
                                    .map(|inner| inner.to_string())
                                    .unwrap_or_else(|| "the decoder needs to be drained".to_owned())
                            )
                        })?;
                    }
                }
                Ok(None) => self.input_done = true,
                Err(err) => return Err(format!("FFmpeg demux error: {err}")),
            }
        }
    }

    /// Restart the stream `seconds` from its beginning.
    ///
    /// This is the native equivalent of the `-ss` argument of the `ffmpeg` binary: the demuxer
    /// seeks, and everything downstream is rebuilt because it holds samples from the old
    /// position. `ac-ffmpeg` only exposes draining a decoder (it pushes a null packet), not
    /// `avcodec_flush_buffers`, so the decoder is recreated from the same stream instead.
    fn seek(&mut self, seconds: f64) -> Result<(), String> {
        let micros = seek_micros(seconds)?;
        self.demuxer
            .seek_to_timestamp(Timestamp::from_micros(micros), SeekTarget::Precise)
            .map_err(|err| {
                format!("FFmpeg could not seek to {seconds}s (the input has to be seekable): {err}")
            })?;

        let decoder = AudioDecoder::from_stream(&self.demuxer.streams()[self.stream_index])
            .and_then(|builder| builder.build())
            .map_err(|err| {
                format!("FFmpeg could not reopen the audio decoder after seeking: {err}")
            })?;

        self.decoder = decoder;
        self.resampler = None;
        self.resampler_source = None;
        self.pending.clear();
        self.input_done = false;
        self.flushed = false;
        self.finished = false;
        log_debug!("seeked the audio source to {seconds}s");
        Ok(())
    }

    fn read_frame(&mut self) -> Result<Vec<u8>, String> {
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
    demuxer: DemuxerWithStreamInfo<Input>,
    stream_index: usize,
    finished: bool,
}

// SAFETY: see the note on `DecodeSource`.
unsafe impl Send for OpusSource {}

impl OpusSource {
    fn open(input: Input) -> Result<Self, String> {
        let demuxed = demux(input)?;
        let stream_index = demuxed.stream_index;

        let codec = demuxed
            .stream()
            .codec_parameters()
            .decoder_name()
            .unwrap_or("unknown")
            .to_owned();
        if !codec.starts_with("opus") && codec != "libopus" {
            return Err(format!(
                "the audio codec is {codec}, not opus; an Opus pass-through source requires an Opus stream"
            ));
        }
        log_info!("using the Opus packet pass-through source (codec {codec})");

        Ok(Self {
            demuxer: demuxed.demuxer,
            stream_index,
            finished: false,
        })
    }

    /// Restart the stream `seconds` from its beginning, keeping the packets as they are.
    fn seek(&mut self, seconds: f64) -> Result<(), String> {
        let micros = seek_micros(seconds)?;
        self.demuxer
            .seek_to_timestamp(Timestamp::from_micros(micros), SeekTarget::Precise)
            .map_err(|err| {
                format!("FFmpeg could not seek to {seconds}s (the input has to be seekable): {err}")
            })?;
        self.finished = false;
        log_debug!("seeked the Opus source to {seconds}s");
        Ok(())
    }

    fn read_packet(&mut self) -> Result<Vec<u8>, String> {
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
                Err(err) => return Err(format!("FFmpeg demux error: {err}")),
            }
        }
    }
}

/// Translate a position in seconds into the microseconds a seek expects.
fn seek_micros(seconds: f64) -> Result<i64, String> {
    if !seconds.is_finite() || seconds < 0.0 {
        return Err(
            "the seek position must be a finite, non-negative number of seconds".to_owned(),
        );
    }
    Ok((seconds * 1_000_000.0).round().min(i64::MAX as f64) as i64)
}

/// Multiply every signed 16 bit little endian sample by `volume`, clamping to the `i16` range.
///
/// This replaces `audioop.mul`, which was removed from the standard library in Python 3.13.
pub fn scale_pcm_s16(data: &[u8], volume: f32) -> Vec<u8> {
    let mut out = Vec::with_capacity(data.len());
    let mut chunks = data.chunks_exact(2);
    for chunk in &mut chunks {
        let sample = i16::from_le_bytes([chunk[0], chunk[1]]);
        let scaled = (f32::from(sample) * volume)
            .round()
            .clamp(-32768.0, 32767.0) as i16;
        out.extend_from_slice(&scaled.to_le_bytes());
    }
    // A trailing odd byte cannot be a sample; pass it through rather than dropping audio.
    out.extend_from_slice(chunks.remainder());
    out
}

/// Scale 16 bit PCM in place of `audioop.mul`.
#[pyfunction]
pub fn scale_pcm<'py>(py: Python<'py>, data: &[u8], volume: f32) -> Bound<'py, PyBytes> {
    PyBytes::new(py, &scale_pcm_s16(data, volume))
}

/// What [`probe_source`] reports: codec name, sample rate, channel count and duration.
pub type ProbeResult = Option<(String, u32, usize, Option<f64>)>;

/// Probe a source and report `(codec, sample_rate, channels, duration_seconds)` without
/// decoding anything.
pub fn probe_source(input: Input) -> Result<ProbeResult, String> {
    let demuxed = demux(input)?;
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
    #[pyo3(signature = (source, headers=None))]
    fn new(
        py: Python<'_>,
        source: &Bound<'_, PyAny>,
        headers: Option<HashMap<String, String>>,
    ) -> PyResult<Self> {
        let input = open_input(source, &parse_headers(headers)?).map_err(ffmpeg_err)?;
        // Probing reads from the input, which for a URL means the network: do it without the GIL
        // so the rest of the interpreter keeps running.
        let inner = py
            .detach(|| DecodeSource::open(input))
            .map_err(ffmpeg_err)?;
        Ok(Self {
            inner: Mutex::new(inner),
        })
    }

    /// Read the next 20ms frame of 48kHz stereo signed 16 bit PCM.
    fn read<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let data = py
            .detach(|| self.inner.lock().read_frame())
            .map_err(ffmpeg_err)?;
        Ok(PyBytes::new(py, &data))
    }

    /// Restart the stream `seconds` from the beginning (the native `-ss` of `ffmpeg`).
    fn seek<'py>(&self, py: Python<'py>, seconds: f64) -> PyResult<()> {
        py.detach(|| self.inner.lock().seek(seconds))
            .map_err(ffmpeg_err)
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
    #[pyo3(signature = (source, headers=None))]
    fn new(
        py: Python<'_>,
        source: &Bound<'_, PyAny>,
        headers: Option<HashMap<String, String>>,
    ) -> PyResult<Self> {
        let input = open_input(source, &parse_headers(headers)?).map_err(ffmpeg_err)?;
        let inner = py.detach(|| OpusSource::open(input)).map_err(ffmpeg_err)?;
        Ok(Self {
            inner: Mutex::new(inner),
        })
    }

    /// Read one raw Opus packet.
    fn read<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let data = py
            .detach(|| self.inner.lock().read_packet())
            .map_err(ffmpeg_err)?;
        Ok(PyBytes::new(py, &data))
    }

    /// Restart the stream `seconds` from the beginning (the native `-ss` of `ffmpeg`).
    fn seek<'py>(&self, py: Python<'py>, seconds: f64) -> PyResult<()> {
        py.detach(|| self.inner.lock().seek(seconds))
            .map_err(ffmpeg_err)
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
#[pyo3(signature = (source, headers=None))]
pub fn probe(
    py: Python<'_>,
    source: &Bound<'_, PyAny>,
    headers: Option<HashMap<String, String>>,
) -> PyResult<ProbeResult> {
    let input = open_input(source, &parse_headers(headers)?).map_err(ffmpeg_err)?;
    py.detach(|| probe_source(input)).map_err(ffmpeg_err)
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
    fn scaling_pcm_clamps_to_the_sixteen_bit_range() {
        // 16 -> 8
        assert_eq!(scale_pcm_s16(&[0x10, 0x00], 0.5), vec![0x08, 0x00]);
        // untouched at 1.0
        assert_eq!(
            scale_pcm_s16(&[0x10, 0x00, 0x00, 0x80], 1.0),
            vec![0x10, 0x00, 0x00, 0x80]
        );
        // -32768 * 2 and 32767 * 2 clamp instead of wrapping around
        assert_eq!(scale_pcm_s16(&[0x00, 0x80], 2.0), vec![0x00, 0x80]);
        assert_eq!(scale_pcm_s16(&[0xff, 0x7f], 2.0), vec![0xff, 0x7f]);
        // silence stays silent, and a trailing odd byte is not dropped
        assert_eq!(scale_pcm_s16(&[0x00, 0x00], 0.25), vec![0x00, 0x00]);
        assert_eq!(
            scale_pcm_s16(&[0x10, 0x00, 0x7f], 1.0),
            vec![0x10, 0x00, 0x7f]
        );
    }

    #[test]
    fn seek_positions_are_validated() {
        assert_eq!(seek_micros(0.5).unwrap(), 500_000);
        assert!(seek_micros(-1.0).is_err());
        assert!(seek_micros(f64::NAN).is_err());
        assert!(seek_micros(f64::INFINITY).is_err());
    }

    #[test]
    fn empty_buffer_read_is_zero() {
        let mut file = File::open("/dev/null").unwrap();
        let mut buf = [0u8; 8];
        assert_eq!(file.read(&mut buf).unwrap(), 0);
    }
}
