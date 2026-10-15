// pyo3's `#[classattr]` expansion assigns fields after `Default::default()`, which trips this
// lint even though the generated code is correct.
#![allow(clippy::field_reassign_with_default)]

//! Opus encoding built on `audiopus`.
//!
//! The parameter names and accepted values match `discord.opus.Encoder` so that
//! `VoiceClient.play(..., application=..., bitrate=..., fec=..., bandwidth=..., signal_type=...)`
//! behaves identically to the pure-Python implementation.

use audiopus::coder::Encoder as RawEncoder;
use audiopus::packet::Packet;
use audiopus::{Application, Bandwidth, Bitrate, Channels, MutSignals, SampleRate, Signal};

/// Samples per 20ms frame at 48kHz.
pub const SAMPLES_PER_FRAME: usize = 960;
/// Frame length in milliseconds.
pub const FRAME_LENGTH: usize = 20;
/// Sampling rate Discord voice uses.
pub const SAMPLING_RATE: u32 = 48_000;
/// Number of channels Discord voice uses.
pub const CHANNELS: usize = 2;
/// The Opus packet Discord interprets as 20ms of silence.
pub const OPUS_SILENCE: [u8; 3] = [0xF8, 0xFF, 0xFE];

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct EncoderConfig {
    pub application: &'static str,
    pub bitrate: u16,
    pub fec: bool,
    pub expected_packet_loss: f64,
    pub bandwidth: &'static str,
    pub signal_type: &'static str,
}

impl Default for EncoderConfig {
    fn default() -> Self {
        Self {
            application: "audio",
            bitrate: 128,
            fec: true,
            expected_packet_loss: 0.15,
            bandwidth: "full",
            signal_type: "auto",
        }
    }
}

pub struct OpusEncoder {
    inner: RawEncoder,
    buffer: Vec<u8>,
}

impl std::fmt::Debug for OpusEncoder {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OpusEncoder")
            .field("buffer_len", &self.buffer.len())
            .finish_non_exhaustive()
    }
}

impl OpusEncoder {
    pub fn new(config: &EncoderConfig) -> Result<Self, String> {
        let application = match config.application {
            "audio" => Application::Audio,
            "voip" => Application::Voip,
            "lowdelay" => Application::LowDelay,
            other => {
                return Err(format!(
                    "{other} is not a valid application setting. Try one of: audiovoip lowdelay"
                ));
            }
        };

        if !(16..=512).contains(&config.bitrate) {
            return Err(format!(
                "bitrate must be between 16 and 512, not {}",
                config.bitrate
            ));
        }

        if !(config.expected_packet_loss > 0.0 && config.expected_packet_loss <= 1.0) {
            return Err(format!(
                "expected_packet_loss must be a positive number less than or equal to 1, not {}",
                config.expected_packet_loss
            ));
        }

        let bandwidth = match config.bandwidth {
            "narrow" => Bandwidth::Narrowband,
            "medium" => Bandwidth::Mediumband,
            "wide" => Bandwidth::Wideband,
            "superwide" => Bandwidth::Superwideband,
            "full" => Bandwidth::Fullband,
            other => {
                return Err(format!(
                    "{other:?} is not a valid bandwidth setting. Try one of: narrow,medium,wide,superwide,full"
                ));
            }
        };

        let signal = match config.signal_type {
            "auto" => Signal::Auto,
            "voice" => Signal::Voice,
            "music" => Signal::Music,
            other => {
                return Err(format!(
                    "{other:?} is not a valid signal type setting. Try one of: auto,voice,music"
                ));
            }
        };

        let mut inner = RawEncoder::new(SampleRate::Hz48000, Channels::Stereo, application)
            .map_err(opus_err)?;

        inner
            .set_bitrate(Bitrate::BitsPerSecond(i32::from(config.bitrate) * 1024))
            .map_err(opus_err)?;
        inner.set_inband_fec(config.fec).map_err(opus_err)?;
        if config.fec {
            let percent = (config.expected_packet_loss * 100.0).clamp(0.0, 100.0) as u8;
            inner.set_packet_loss_perc(percent).map_err(opus_err)?;
        }
        inner.set_bandwidth(bandwidth).map_err(opus_err)?;
        inner.set_signal(signal).map_err(opus_err)?;

        Ok(Self {
            inner,
            // Opus never produces more than 1275 bytes per frame (120ms), 4000 is a safe
            // upper bound for a 20ms stereo frame at 512kbps.
            buffer: vec![0u8; 4000],
        })
    }

    /// Encode interleaved 48kHz stereo `i16` samples.
    pub fn encode(&mut self, pcm: &[i16]) -> Result<Vec<u8>, String> {
        let written = self.inner.encode(pcm, &mut self.buffer).map_err(opus_err)?;
        Ok(self.buffer[..written].to_vec())
    }

    /// Encode exactly one 20ms frame, ignoring any samples beyond
    /// [`SAMPLES_PER_FRAME`] the way `discord.opus.Encoder.encode` does.
    pub fn encode_frame(&mut self, pcm: &[i16]) -> Result<Vec<u8>, String> {
        let wanted = SAMPLES_PER_FRAME * CHANNELS;
        let slice = if pcm.len() > wanted {
            &pcm[..wanted]
        } else {
            pcm
        };
        self.encode(slice)
    }
}

fn opus_err(err: audiopus::Error) -> String {
    format!("Opus error: {err}")
}

/// Validate encoder parameters the way `discord.opus.Encoder.__init__` does: with
/// :class:`ValueError` rather than an Opus failure.
pub fn validate_config(config: &EncoderConfig) -> PyResult<()> {
    if !(16..=512).contains(&config.bitrate) {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "bitrate must be between 16 and 512, not {}",
            config.bitrate
        )));
    }
    if !(config.expected_packet_loss > 0.0 && config.expected_packet_loss <= 1.0) {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "expected_packet_loss must be a positive number less than or equal to 1, not {}",
            config.expected_packet_loss
        )));
    }
    Ok(())
}

/// Decode a raw Opus packet into interleaved 48kHz stereo `i16` samples.
pub struct OpusDecoder {
    inner: audiopus::coder::Decoder,
    buffer: Vec<i16>,
}

impl OpusDecoder {
    pub fn new() -> Result<Self, String> {
        let inner = audiopus::coder::Decoder::new(SampleRate::Hz48000, Channels::Stereo)
            .map_err(opus_err)?;
        Ok(Self {
            inner,
            buffer: vec![0i16; SAMPLES_PER_FRAME * CHANNELS],
        })
    }

    pub fn decode(&mut self, packet: &[u8]) -> Result<Vec<i16>, String> {
        let input = Packet::try_from(packet).map_err(opus_err)?;
        let signals = MutSignals::try_from(&mut self.buffer).map_err(opus_err)?;
        let written = self
            .inner
            .decode(Some(input), signals, false)
            .map_err(opus_err)?;
        Ok(self.buffer[..written * CHANNELS].to_vec())
    }

    /// Decode a lost packet by asking Opus to interpolate.
    pub fn decode_plc(&mut self) -> Result<Vec<i16>, String> {
        let signals = MutSignals::try_from(&mut self.buffer).map_err(opus_err)?;
        let written = self.inner.decode(None, signals, false).map_err(opus_err)?;
        Ok(self.buffer[..written * CHANNELS].to_vec())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_silence_to_a_small_packet() {
        let mut encoder = OpusEncoder::new(&EncoderConfig::default()).unwrap();
        let pcm = vec![0i16; SAMPLES_PER_FRAME * CHANNELS];
        let packet = encoder.encode(&pcm).unwrap();
        assert!(!packet.is_empty());
        assert!(packet.len() < 200, "silence should compress well");
    }

    #[test]
    fn rejects_invalid_parameters() {
        let mut config = EncoderConfig::default();
        config.bitrate = 8;
        assert!(OpusEncoder::new(&config).is_err());

        let mut config = EncoderConfig::default();
        config.bandwidth = "ultra";
        assert!(OpusEncoder::new(&config).is_err());

        let mut config = EncoderConfig::default();
        config.application = "speech";
        assert!(OpusEncoder::new(&config).is_err());
    }

    #[test]
    fn decode_roundtrip_produces_samples() {
        let mut encoder = OpusEncoder::new(&EncoderConfig::default()).unwrap();
        let pcm: Vec<i16> = (0..SAMPLES_PER_FRAME * CHANNELS)
            .map(|i| ((i as f32 * 0.05).sin() * 8000.0) as i16)
            .collect();
        let packet = encoder.encode(&pcm).unwrap();

        let mut decoder = OpusDecoder::new().unwrap();
        let decoded = decoder.decode(&packet).unwrap();
        assert_eq!(decoded.len(), SAMPLES_PER_FRAME * CHANNELS);
    }
}

// ---------------------------------------------------------------------------------------
// Python bindings
// ---------------------------------------------------------------------------------------

use parking_lot::Mutex;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::errors::OpusError;

fn intern_application(value: &str) -> PyResult<&'static str> {
    Ok(match value {
        "audio" => "audio",
        "voip" => "voip",
        "lowdelay" => "lowdelay",
        other => {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "{other} is not a valid application setting. Try one of: audiovoip lowdelay"
            )));
        }
    })
}

fn intern_bandwidth(value: &str) -> PyResult<&'static str> {
    Ok(match value {
        "narrow" => "narrow",
        "medium" => "medium",
        "wide" => "wide",
        "superwide" => "superwide",
        "full" => "full",
        other => {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "{other:?} is not a valid bandwidth setting. Try one of: narrow,medium,wide,superwide,full"
            )));
        }
    })
}

fn intern_signal(value: &str) -> PyResult<&'static str> {
    Ok(match value {
        "auto" => "auto",
        "voice" => "voice",
        "music" => "music",
        other => {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "{other:?} is not a valid signal type setting. Try one of: auto,voice,music"
            )));
        }
    })
}

/// Interleaved little-endian `s16` bytes -> `i16` samples.
pub fn bytes_to_samples(data: &[u8]) -> Vec<i16> {
    data.chunks_exact(2)
        .map(|chunk| i16::from_le_bytes([chunk[0], chunk[1]]))
        .collect()
}

/// A standalone Opus encoder, the native counterpart of `discord.opus.Encoder`.
#[pyclass(module = "discord_ext_voice._native", name = "OpusEncoder")]
pub struct PyOpusEncoder {
    inner: Mutex<OpusEncoder>,
}

#[pymethods]
impl PyOpusEncoder {
    #[new]
    #[pyo3(signature = (application="audio", bitrate=128, fec=true, expected_packet_loss=0.15, bandwidth="full", signal_type="auto"))]
    fn new(
        application: &str,
        bitrate: u16,
        fec: bool,
        expected_packet_loss: f64,
        bandwidth: &str,
        signal_type: &str,
    ) -> PyResult<Self> {
        let config = EncoderConfig {
            application: intern_application(application)?,
            bitrate,
            fec,
            expected_packet_loss,
            bandwidth: intern_bandwidth(bandwidth)?,
            signal_type: intern_signal(signal_type)?,
        };
        validate_config(&config)?;
        Ok(Self {
            inner: Mutex::new(OpusEncoder::new(&config).map_err(OpusError::new_err)?),
        })
    }

    /// Encode 48kHz stereo `s16` PCM bytes into one Opus frame.
    ///
    /// `frame_size` matches `discord.opus.Encoder.encode(pcm, frame_size)` and defaults to
    /// the 960 samples of a 20ms frame.
    #[pyo3(signature = (pcm, frame_size=SAMPLES_PER_FRAME))]
    fn encode<'py>(
        &self,
        py: Python<'py>,
        pcm: &[u8],
        frame_size: usize,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let mut samples = bytes_to_samples(pcm);
        samples.truncate(frame_size * CHANNELS);
        let encoded = self
            .inner
            .lock()
            .encode(&samples)
            .map_err(OpusError::new_err)?;
        Ok(PyBytes::new(py, &encoded))
    }

    fn set_bitrate(&self, kbps: u16) -> PyResult<()> {
        let bitrate = Bitrate::BitsPerSecond(i32::from(kbps.clamp(16, 512)) * 1024);
        self.inner
            .lock()
            .inner
            .set_bitrate(bitrate)
            .map_err(|err| OpusError::new_err(opus_err(err)))
    }

    fn set_fec(&self, enabled: bool) -> PyResult<()> {
        self.inner
            .lock()
            .inner
            .set_inband_fec(enabled)
            .map_err(|err| OpusError::new_err(opus_err(err)))
    }

    fn set_expected_packet_loss_percent(&self, percentage: f64) -> PyResult<()> {
        let percent = (percentage * 100.0).clamp(0.0, 100.0) as u8;
        self.inner
            .lock()
            .inner
            .set_packet_loss_perc(percent)
            .map_err(|err| OpusError::new_err(opus_err(err)))
    }

    fn set_bandwidth(&self, bandwidth: &str) -> PyResult<()> {
        let value = match bandwidth {
            "narrow" => Bandwidth::Narrowband,
            "medium" => Bandwidth::Mediumband,
            "wide" => Bandwidth::Wideband,
            "superwide" => Bandwidth::Superwideband,
            "full" => Bandwidth::Fullband,
            other => {
                return Err(pyo3::exceptions::PyKeyError::new_err(format!(
                    "{other:?} is not a valid bandwidth setting"
                )));
            }
        };
        self.inner
            .lock()
            .inner
            .set_bandwidth(value)
            .map_err(|err| OpusError::new_err(opus_err(err)))
    }

    fn set_signal_type(&self, signal_type: &str) -> PyResult<()> {
        let value = match signal_type {
            "auto" => Signal::Auto,
            "voice" => Signal::Voice,
            "music" => Signal::Music,
            other => {
                return Err(pyo3::exceptions::PyKeyError::new_err(format!(
                    "{other:?} is not a valid signal type setting"
                )));
            }
        };
        self.inner
            .lock()
            .inner
            .set_signal(value)
            .map_err(|err| OpusError::new_err(opus_err(err)))
    }

    #[classattr]
    #[allow(clippy::redundant_field_names)]
    const SAMPLES_PER_FRAME: usize = SAMPLES_PER_FRAME;

    #[classattr]
    const FRAME_LENGTH: usize = FRAME_LENGTH;

    #[classattr]
    const SAMPLING_RATE: u32 = SAMPLING_RATE;

    #[classattr]
    const CHANNELS: usize = CHANNELS;

    #[classattr]
    const SAMPLE_SIZE: usize = CHANNELS * 2;

    #[classattr]
    const FRAME_SIZE: usize = SAMPLES_PER_FRAME * CHANNELS * 2;

    fn __repr__(&self) -> String {
        "<discord_ext_voice.opus.Encoder (libopus via audiopus)>".to_owned()
    }
}

impl PyOpusEncoder {
    /// Used by `VoiceConnection.send_audio_packet` when the caller passes its encoder in.
    pub fn encode_pcm(&self, pcm: &[u8]) -> Result<Vec<u8>, String> {
        let samples = bytes_to_samples(pcm);
        self.inner.lock().encode_frame(&samples)
    }
}

/// A standalone Opus decoder.
#[pyclass(module = "discord_ext_voice._native", name = "OpusDecoder")]
pub struct PyOpusDecoder {
    inner: Mutex<OpusDecoder>,
}

#[pymethods]
impl PyOpusDecoder {
    #[new]
    fn new() -> PyResult<Self> {
        Ok(Self {
            inner: Mutex::new(OpusDecoder::new().map_err(OpusError::new_err)?),
        })
    }

    /// Decode a raw Opus packet into 48kHz stereo `s16` PCM bytes.
    fn decode<'py>(&self, py: Python<'py>, packet: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
        let samples = self
            .inner
            .lock()
            .decode(packet)
            .map_err(OpusError::new_err)?;
        let mut bytes = Vec::with_capacity(samples.len() * 2);
        for sample in samples {
            bytes.extend_from_slice(&sample.to_le_bytes());
        }
        Ok(PyBytes::new(py, &bytes))
    }

    /// Conceal a lost packet.
    fn decode_plc<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let samples = self.inner.lock().decode_plc().map_err(OpusError::new_err)?;
        let mut bytes = Vec::with_capacity(samples.len() * 2);
        for sample in samples {
            bytes.extend_from_slice(&sample.to_le_bytes());
        }
        Ok(PyBytes::new(py, &bytes))
    }

    fn __repr__(&self) -> String {
        "<discord_ext_voice.opus.Decoder (libopus via audiopus)>".to_owned()
    }
}
