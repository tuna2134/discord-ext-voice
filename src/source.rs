//! In-process audio decoding, demuxing and resampling, exposed to Python as `AudioSource`
//! compatible objects.
//!
//! Nothing here needs FFmpeg: containers and codecs come from `symphonia` (pure Rust), the
//! sample-rate conversion that `libswresample` used to do comes from `rubato`, and Opus — the one
//! codec symphonia has no decoder for — is decoded by `oprs`, the same pure-Rust libopus port the
//! encoder uses.
//!
//! Two flavours are provided:
//!
//! * [`PyDecodeSource`] decodes and resamples any input to 48kHz stereo `s16` and hands out
//!   exactly one 20ms RTP frame (3840 bytes) per `read()`.
//! * [`PyOpusSource`] demuxes a stream whose audio codec is already Opus and hands out the raw
//!   Opus packets, so no re-encoding is needed (`AudioSource.is_opus() is True`).
//!
//! Both accept a filesystem path, a Python file-like object or an `http(s)` URL (streamed with
//! `reqwest`, with resumable reads), so a bot never shells out for its media.

use std::collections::{HashMap, VecDeque};
use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom};
use std::path::PathBuf;
use std::time::Duration;

use parking_lot::Mutex;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};
use rubato::audioadapter_buffers::direct::InterleavedSlice;
use rubato::{
    Async, FixedAsync, Indexing, Resampler, SincInterpolationParameters, SincInterpolationType,
    WindowFunction, calculate_cutoff,
};
use symphonia::core::audio::Channels;
use symphonia::core::codecs::CodecParameters;
use symphonia::core::codecs::audio::well_known::CODEC_ID_OPUS;
use symphonia::core::codecs::audio::{
    AudioCodecId, AudioCodecParameters, AudioDecoder, AudioDecoderOptions,
};
use symphonia::core::errors::Error as SymphoniaError;
use symphonia::core::formats::probe::Hint;
use symphonia::core::formats::{FormatOptions, FormatReader, SeekMode, SeekTo, TrackType};
use symphonia::core::io::{MediaSource, MediaSourceStream, MediaSourceStreamOptions};
use symphonia::core::meta::MetadataOptions;
use symphonia::core::packet::Packet;
use symphonia::core::units::Time;

use crate::errors::FFmpegError;
use crate::{log_debug, log_info};

/// One 20ms frame of 48kHz stereo signed 16 bit PCM.
pub const FRAME_SAMPLES: usize = 960;
pub const FRAME_CHANNELS: usize = 2;
pub const FRAME_BYTES: usize = FRAME_SAMPLES * FRAME_CHANNELS * 2;
const TARGET_SAMPLE_RATE: u32 = 48_000;
/// Opus always decodes to 48kHz, whatever the stream's original rate was.
const OPUS_RATE: u32 = TARGET_SAMPLE_RATE;
/// Frames per resampler chunk: short enough to keep the filter's delay inaudible, long enough to
/// be cheap.
const RESAMPLE_CHUNK: usize = 1024;
/// Taps in the resampler's sinc filter.
const SINC_LEN: usize = 128;

/// Build the Python exception for an internal error message.
///
/// The decoding entry points run with the GIL released, so everything below them reports
/// failures as strings and only the Python-facing boundary turns them into an exception.
fn ffmpeg_err(message: impl Into<String>) -> PyErr {
    FFmpegError::new_err(message.into())
}

/// How much of a forward seek to do by reading instead of asking the server again.
const SEEK_SKIP_LIMIT: u64 = 1 << 20;

/// How many consecutive read failures to recover from before giving up. A dropped connection is
/// normal for a long stream; a server that refuses every request is not.
const MAX_RESUMES: u32 = 3;

/// A byte source for the demuxer: a real file, a Python file-like object, or an HTTP(S) URL.
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

/// An HTTP(S) response as a seekable byte stream for the demuxer.
///
/// `read` hands the demuxer what the server has sent so far, so playback starts before the file
/// has been downloaded. `seek` reads forward when the target is close, and otherwise asks the
/// server for the rest of the file with a `Range` header — falling back to restarting and
/// skipping when the server ignores ranges. Containers whose index sits at the end (MP4) need
/// that.
///
/// The connection is opened on the first read, so constructing a source never blocks on the
/// network, and a connection that dies mid-stream is picked up again from the byte it reached: a
/// throttling CDN closing an idle connection is normal, while the demuxer reads the end of the
/// input as the end of the file.
pub struct HttpStream {
    url: String,
    client: reqwest::blocking::Client,
    response: Option<reqwest::blocking::Response>,
    /// The offset of the next byte the response will hand out.
    position: u64,
    /// `Content-Length` of the whole stream, when the server reports one.
    length: Option<u64>,
    /// How many reads in a row had to be resumed. Reset by the first read that delivers.
    resumes: u32,
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
            resumes: 0,
        })
    }

    /// The response to read from, requesting it when there is none.
    ///
    /// The first request is a plain `GET`; after a dropped connection the stream continues from
    /// `position`, which [`HttpStream::request_from`] turns into a `Range` request — or, for a
    /// server that ignores ranges, a restart that is skipped forward.
    fn response(&mut self) -> io::Result<&mut reqwest::blocking::Response> {
        if self.response.is_none() {
            let offset = self.position;
            if !self.request_from(offset)? && offset > 0 {
                self.skip(offset)?;
            }
        }
        Ok(self.response.as_mut().expect("just connected"))
    }

    /// Pick the stream up again at `position`, counting the attempt.
    fn resume(&mut self) -> io::Result<()> {
        self.resumes += 1;
        self.response = None;
        self.response().map(|_| ())
    }

    /// Whether the response stopped short of the length the server advertised.
    fn truncated(&self) -> bool {
        matches!(self.length, Some(length) if self.position < length)
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
        loop {
            let outcome = self.response()?.read(buf);

            match outcome {
                // A response that ended before the length it advertised is a truncated one, not
                // the end of the stream.
                Ok(0) if self.truncated() && self.resumes < MAX_RESUMES => {
                    if self.resume().is_err() {
                        return Ok(0);
                    }
                }
                Ok(read) => {
                    self.position += read as u64;
                    self.resumes = 0;
                    return Ok(read);
                }
                // A dropped connection is not the end of the stream either: ask for the rest
                // from the byte we reached and carry on.
                Err(err) if self.position > 0 && self.resumes < MAX_RESUMES => {
                    if self.resume().is_err() {
                        return Err(err);
                    }
                }
                Err(err) => return Err(err),
            }
        }
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

/// Adapter that drives a Python file-like object from the demuxer's IO callbacks.
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

/// Turn a Python object into demuxer input.
///
/// Only the Python object is touched here: opening an HTTP(S) URL is left to the first read, and
/// the probing happens in [`probe_input`], which the callers run without the GIL.
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

/// The audio track that gets played, as the container reports it.
struct AudioTrack {
    id: u32,
    params: AudioCodecParameters,
    /// The container's duration, when it reports one.
    duration: Option<f64>,
}

/// Let symphonia read from our inputs: a file, a Python file object or a resumable HTTP stream.
impl MediaSource for Input {
    fn is_seekable(&self) -> bool {
        Input::seekable(self)
    }

    fn byte_len(&self) -> Option<u64> {
        match self {
            Input::File(file) => file.metadata().ok().map(|meta| meta.len()),
            Input::PyFile(_) => None,
            Input::Http(stream) => stream.length,
        }
    }
}

/// Probe `input` for its container and audio track.
fn probe_input(input: Input) -> Result<(Box<dyn FormatReader>, AudioTrack), String> {
    let stream = MediaSourceStream::new(Box::new(input), MediaSourceStreamOptions::default());
    let format = symphonia::default::get_probe()
        .probe(
            &Hint::new(),
            stream,
            FormatOptions::default(),
            MetadataOptions::default(),
        )
        .map_err(|err| format!("the input could not be identified: {err}"))?;

    let track = format
        .default_track(TrackType::Audio)
        .ok_or_else(|| "the input contains no audio stream".to_owned())?;

    let id = track.id;
    let num_frames = track.num_frames;
    let params = match track.codec_params.as_ref() {
        Some(CodecParameters::Audio(params)) => params.clone(),
        _ => return Err("the input's audio track carries no codec information".to_owned()),
    };

    let media = format.media_info();
    let duration = media
        .duration
        .zip(media.time_base)
        .and_then(|(duration, base)| base.calc_duration(duration))
        .map(|time| time.as_secs_f64())
        .or_else(|| {
            let frames = num_frames?;
            let rate = params.sample_rate?;
            (rate > 0).then(|| frames as f64 / f64::from(rate))
        });

    Ok((
        format,
        AudioTrack {
            id,
            params,
            duration,
        },
    ))
}

/// The codec's name, spelled the way `ffmpeg` used to report it.
fn codec_name(codec: AudioCodecId) -> &'static str {
    use symphonia::core::codecs::audio::well_known::{
        CODEC_ID_AAC, CODEC_ID_ALAC, CODEC_ID_FLAC, CODEC_ID_MP3, CODEC_ID_OPUS,
        CODEC_ID_PCM_F32LE, CODEC_ID_PCM_F64LE, CODEC_ID_PCM_S8, CODEC_ID_PCM_S16BE,
        CODEC_ID_PCM_S16LE, CODEC_ID_PCM_S24BE, CODEC_ID_PCM_S24LE, CODEC_ID_PCM_S32BE,
        CODEC_ID_PCM_S32LE, CODEC_ID_PCM_U8, CODEC_ID_VORBIS,
    };

    if codec == CODEC_ID_PCM_S16LE {
        "pcm_s16le"
    } else if codec == CODEC_ID_PCM_S16BE {
        "pcm_s16be"
    } else if codec == CODEC_ID_PCM_S24LE {
        "pcm_s24le"
    } else if codec == CODEC_ID_PCM_S24BE {
        "pcm_s24be"
    } else if codec == CODEC_ID_PCM_S32LE {
        "pcm_s32le"
    } else if codec == CODEC_ID_PCM_S32BE {
        "pcm_s32be"
    } else if codec == CODEC_ID_PCM_S8 {
        "pcm_s8"
    } else if codec == CODEC_ID_PCM_U8 {
        "pcm_u8"
    } else if codec == CODEC_ID_PCM_F32LE {
        "pcm_f32le"
    } else if codec == CODEC_ID_PCM_F64LE {
        "pcm_f64le"
    } else if codec == CODEC_ID_MP3 {
        "mp3"
    } else if codec == CODEC_ID_AAC {
        "aac"
    } else if codec == CODEC_ID_FLAC {
        "flac"
    } else if codec == CODEC_ID_VORBIS {
        "vorbis"
    } else if codec == CODEC_ID_OPUS {
        "opus"
    } else if codec == CODEC_ID_ALAC {
        "alac"
    } else {
        "unknown"
    }
}

/// The decoder for one track.
///
/// Symphonia decodes everything except Opus, which it has no decoder for; those packets go to
/// `oprs` (a pure-Rust libopus port), the same codec the encoder uses.
enum TrackDecoder {
    Symphonia(Box<dyn AudioDecoder>),
    Opus(crate::opus::OpusDecoder),
}

impl TrackDecoder {
    fn new(params: &AudioCodecParameters) -> Result<Self, String> {
        if params.codec == CODEC_ID_OPUS {
            return Ok(Self::Opus(crate::opus::OpusDecoder::new()?));
        }

        let decoder = symphonia::default::get_codecs()
            .make_audio_decoder(params, &AudioDecoderOptions::default())
            .map_err(|err| {
                format!(
                    "no decoder for the input's audio codec ({}): {err}",
                    codec_name(params.codec)
                )
            })?;
        Ok(Self::Symphonia(decoder))
    }

    /// Drop the decoder's state, as a seek requires.
    fn reset(&mut self) -> Result<(), String> {
        match self {
            Self::Symphonia(decoder) => {
                decoder.reset();
                Ok(())
            }
            // The decoder carries state between packets, so a fresh one is the reset.
            Self::Opus(_) => {
                *self = Self::Opus(crate::opus::OpusDecoder::new()?);
                Ok(())
            }
        }
    }
}

/// Where decoded samples go before they are handed out.
enum Pipeline {
    /// The source is already 48kHz stereo: only the sample format changes.
    Direct,
    /// A different rate: `rubato` converts it.
    Resample(Box<dyn Resampler<f32>>),
}

/// Decodes any input to 48kHz stereo signed 16 bit PCM, in 20ms frames.
pub struct DecodeSource {
    format: Box<dyn FormatReader>,
    track_id: u32,
    /// Whether the input can seek at all. Symphonia can satisfy a small seek from what it has
    /// buffered, but a source that is not seekable stays one: that is the documented contract.
    seekable: bool,
    decoder: TrackDecoder,
    pipeline: Pipeline,
    /// The rate the current resampler was built for.
    pipeline_rate: u32,
    /// Interleaved stereo `f32` waiting for a full resampler chunk.
    staging: Vec<f32>,
    /// Interleaved 48kHz stereo `s16`, ready to be handed out.
    pending: VecDeque<u8>,
    /// Whether the container has produced its last packet.
    input_done: bool,
    finished: bool,
}

// SAFETY: the decoder is used behind the `Mutex` this type lives in and is never shared: one
// thread at a time drives `DecodeSource`.
unsafe impl Send for DecodeSource {}

impl DecodeSource {
    fn open(input: Input) -> Result<Self, String> {
        let seekable = input.seekable();
        let (format, track) = probe_input(input)?;
        let decoder = TrackDecoder::new(&track.params)?;
        log_debug!(
            "decoding {} {}Hz {}ch",
            codec_name(track.params.codec),
            track.params.sample_rate.unwrap_or(0),
            track
                .params
                .channels
                .as_ref()
                .map(Channels::count)
                .unwrap_or(0)
        );

        Ok(Self {
            format,
            track_id: track.id,
            seekable,
            decoder,
            pipeline: Pipeline::Direct,
            pipeline_rate: TARGET_SAMPLE_RATE,
            staging: Vec::new(),
            pending: VecDeque::new(),
            input_done: false,
            finished: false,
        })
    }

    /// Decode packets until a 20ms frame is buffered, or the input ends.
    fn pump(&mut self) -> Result<(), String> {
        while self.pending.len() < FRAME_BYTES && !self.finished {
            if self.input_done {
                // Let the resampler produce the tail of what is left, then stop.
                self.resample(true)?;
                self.finished = true;
                return Ok(());
            }

            match self.format.next_packet() {
                Ok(Some(packet)) => {
                    if packet.track_id == self.track_id {
                        self.decode_packet(&packet)?;
                    }
                }
                Ok(None) => self.input_done = true,
                Err(SymphoniaError::ResetRequired) => {
                    return Err("the container changed its track layout mid-stream".to_owned());
                }
                Err(err) => return Err(format!("the container could not be read: {err}")),
            }
        }
        Ok(())
    }

    /// Decode one packet into `pending`, through the resampler when there is one.
    fn decode_packet(&mut self, packet: &Packet) -> Result<(), String> {
        let (samples, rate, channels) = match &mut self.decoder {
            TrackDecoder::Symphonia(decoder) => match decoder.decode(packet) {
                Ok(buffer) => {
                    let spec = buffer.spec().clone();
                    let channels = spec.channels().count();
                    let mut samples = Vec::with_capacity(buffer.frames() * channels.max(1));
                    buffer.copy_to_vec_interleaved::<f32>(&mut samples);
                    (samples, spec.rate(), channels)
                }
                // A malformed packet is skipped, as symphonia documents: decoding continues with
                // the next one.
                Err(SymphoniaError::DecodeError(reason)) => {
                    log_debug!("skipping an undecodable packet: {reason}");
                    return Ok(());
                }
                Err(SymphoniaError::IoError(err)) => {
                    log_debug!("skipping a packet the stream cut short: {err}");
                    return Ok(());
                }
                Err(err) => return Err(format!("the audio decoder failed: {err}")),
            },
            TrackDecoder::Opus(decoder) => {
                let decoded = decoder.decode(&packet.data)?;
                let samples = decoded
                    .iter()
                    .map(|sample| f32::from(*sample) / 32768.0)
                    .collect();
                (samples, OPUS_RATE, FRAME_CHANNELS)
            }
        };

        self.push_samples(&samples, rate, channels)
    }

    /// Convert decoded samples to 48kHz stereo `s16` and buffer them.
    fn push_samples(&mut self, samples: &[f32], rate: u32, channels: usize) -> Result<(), String> {
        if channels == 0 {
            return Ok(());
        }

        // Stereo first: mono is duplicated, anything wider keeps its first two channels.
        let stereo: Vec<f32> = match channels {
            1 => samples
                .iter()
                .flat_map(|sample| [*sample, *sample])
                .collect(),
            2 => samples.to_vec(),
            _ => samples
                .chunks_exact(channels)
                .flat_map(|frame| [frame[0], frame[1]])
                .collect(),
        };

        self.ensure_pipeline(rate)?;
        match self.pipeline {
            Pipeline::Direct => {
                self.push_s16(&stereo);
                Ok(())
            }
            Pipeline::Resample(_) => {
                self.staging.extend_from_slice(&stereo);
                self.resample(false)
            }
        }
    }

    /// Build (or replace) the pipeline for a source at `rate`.
    fn ensure_pipeline(&mut self, rate: u32) -> Result<(), String> {
        if rate == TARGET_SAMPLE_RATE {
            if !matches!(self.pipeline, Pipeline::Direct) {
                log_debug!("no resampling needed");
                self.pipeline = Pipeline::Direct;
                self.staging.clear();
            }
            self.pipeline_rate = TARGET_SAMPLE_RATE;
            return Ok(());
        }

        if matches!(self.pipeline, Pipeline::Resample(_)) && self.pipeline_rate == rate {
            return Ok(());
        }

        let parameters = SincInterpolationParameters {
            sinc_len: SINC_LEN,
            f_cutoff: Some(calculate_cutoff::<f32>(
                SINC_LEN,
                WindowFunction::BlackmanHarris2,
            )),
            oversampling_factor: 128,
            interpolation: SincInterpolationType::Linear,
            window: WindowFunction::BlackmanHarris2,
        };
        let resampler = Async::<f32>::new_sinc(
            f64::from(TARGET_SAMPLE_RATE) / f64::from(rate),
            1.1,
            &parameters,
            RESAMPLE_CHUNK,
            FRAME_CHANNELS,
            FixedAsync::Input,
        )
        .map_err(|err| format!("unable to build the resampler for {rate}Hz: {err}"))?;

        log_debug!("resampling {rate}Hz to {TARGET_SAMPLE_RATE}Hz");
        self.pipeline = Pipeline::Resample(Box::new(resampler));
        self.pipeline_rate = rate;
        self.staging.clear();
        Ok(())
    }

    /// Move whatever the resampler can produce into `pending`.
    ///
    /// `flushing` feeds the last, short chunk (padded with silence by the resampler) so that the
    /// tail of the input is not dropped.
    fn resample(&mut self, flushing: bool) -> Result<(), String> {
        // The resampler is moved out so that `self` stays available while it runs.
        let Pipeline::Resample(mut resampler) =
            std::mem::replace(&mut self.pipeline, Pipeline::Direct)
        else {
            return Ok(());
        };

        let result = self.resample_with(&mut *resampler, flushing);
        self.pipeline = Pipeline::Resample(resampler);
        result
    }

    fn resample_with(
        &mut self,
        resampler: &mut dyn Resampler<f32>,
        flushing: bool,
    ) -> Result<(), String> {
        loop {
            let available = self.staging.len() / FRAME_CHANNELS;
            if available == 0 {
                return Ok(());
            }

            let wanted = resampler.input_frames_next();
            let take = available.min(wanted);
            let partial = take < wanted;
            if partial && !flushing {
                return Ok(()); // wait until a whole chunk has been decoded
            }

            let slice = &self.staging[..take * FRAME_CHANNELS];
            let input = InterleavedSlice::new(slice, FRAME_CHANNELS, take)
                .map_err(|err| format!("unable to hand samples to the resampler: {err}"))?;
            let indexing = Indexing {
                partial_len: partial.then_some(take),
                ..Indexing::default()
            };
            let output = resampler
                .process(&input, Some(&indexing))
                .map_err(|err| format!("the resampler failed: {err}"))?;

            self.staging.drain(..take * FRAME_CHANNELS);
            let data = output.take_data();
            self.push_s16(&data);

            if partial {
                return Ok(());
            }
        }
    }

    fn push_s16(&mut self, samples: &[f32]) {
        for sample in samples {
            let value = (sample.clamp(-1.0, 1.0) * f32::from(i16::MAX)).round() as i16;
            self.pending.extend(value.to_le_bytes());
        }
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
        let mut frame: Vec<u8> = self.pending.drain(..take).collect();
        if frame.len() < FRAME_BYTES {
            // The last frame of a source is padded rather than dropped: every frame the player
            // sends has to be 20ms.
            frame.resize(FRAME_BYTES, 0);
        }
        Ok(frame)
    }

    /// Restart the stream `seconds` from its beginning.
    fn seek(&mut self, seconds: f64) -> Result<(), String> {
        let micros = seek_micros(seconds)?;
        if !self.seekable {
            return Err(not_seekable(seconds));
        }
        self.format
            .seek(
                SeekMode::Accurate,
                SeekTo::Time {
                    time: Time::from_micros(micros),
                    track_id: Some(self.track_id),
                },
            )
            .map_err(|err| {
                format!(
                    "the input could not be seeked to {seconds}s (it has to be seekable): {err}"
                )
            })?;

        self.decoder.reset()?;
        self.staging.clear();
        self.pending.clear();
        self.input_done = false;
        self.finished = false;
        if let Pipeline::Resample(resampler) = &mut self.pipeline {
            resampler.reset();
        }
        log_debug!("seeked the audio source to {seconds}s");
        Ok(())
    }
}

/// A pass-through source that yields the raw Opus packets of an already-encoded stream.
pub struct OpusSource {
    format: Box<dyn FormatReader>,
    track_id: u32,
    seekable: bool,
    finished: bool,
}

impl OpusSource {
    fn open(input: Input) -> Result<Self, String> {
        let seekable = input.seekable();
        let (format, track) = probe_input(input)?;
        if track.params.codec != CODEC_ID_OPUS {
            return Err(format!(
                "the input's audio codec is {}, not Opus",
                codec_name(track.params.codec)
            ));
        }

        log_debug!("passing Opus packets through");
        Ok(Self {
            format,
            track_id: track.id,
            seekable,
            finished: false,
        })
    }

    fn read_packet(&mut self) -> Result<Vec<u8>, String> {
        if self.finished {
            return Ok(Vec::new());
        }

        loop {
            match self.format.next_packet() {
                Ok(Some(packet)) if packet.track_id == self.track_id && !packet.data.is_empty() => {
                    return Ok(packet.data.to_vec());
                }
                Ok(Some(_)) => {}
                Ok(None) => {
                    self.finished = true;
                    return Ok(Vec::new());
                }
                Err(SymphoniaError::DecodeError(reason)) => {
                    log_debug!("skipping an undecodable packet: {reason}");
                }
                Err(err) => return Err(format!("the container could not be read: {err}")),
            }
        }
    }

    fn seek(&mut self, seconds: f64) -> Result<(), String> {
        let micros = seek_micros(seconds)?;
        if !self.seekable {
            return Err(not_seekable(seconds));
        }
        self.format
            .seek(
                SeekMode::Accurate,
                SeekTo::Time {
                    time: Time::from_micros(micros),
                    track_id: Some(self.track_id),
                },
            )
            .map_err(|err| {
                format!(
                    "the input could not be seeked to {seconds}s (it has to be seekable): {err}"
                )
            })?;

        self.finished = false;
        log_debug!("seeked the Opus source to {seconds}s");
        Ok(())
    }
}

/// The error a seek reports when the input cannot seek at all.
fn not_seekable(seconds: f64) -> String {
    format!(
        "the input could not be seeked to {seconds}s (it has to be seekable): the input is not seekable"
    )
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

/// What [`probe_source`] reports: codec name, sample rate, channel count and duration.
pub type ProbeResult = Option<(String, u32, usize, Option<f64>)>;

/// Probe a source and report `(codec, sample_rate, channels, duration_seconds)` without decoding
/// anything.
pub fn probe_source(input: Input) -> Result<ProbeResult, String> {
    let (_format, track) = probe_input(input)?;
    Ok(Some((
        codec_name(track.params.codec).to_owned(),
        track.params.sample_rate.unwrap_or(0),
        track
            .params
            .channels
            .as_ref()
            .map(Channels::count)
            .unwrap_or(0),
        track.duration,
    )))
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
            guard.staging.clear();
            // Drop the resampler and the decoder with it: cleanup is what a source does when it
            // is finished with.
            guard.pipeline = Pipeline::Direct;
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
