// pyo3's `#[classattr]` expansion assigns fields after `Default::default()`, which trips this
// lint even though the generated code is correct.
#![allow(clippy::field_reassign_with_default)]

//! Opus encoding and decoding built on `oprs`, a pure-Rust port of libopus.
//!
//! The parameter names and accepted values match `discord.opus.Encoder` so that
//! `VoiceClient.play(..., application=..., bitrate=..., fec=..., bandwidth=..., signal_type=...)`
//! behaves identically to the pure-Python implementation.

use oprs::coder::Encoder as RawEncoder;
use oprs::packet::Packet;
use oprs::{Application, Bandwidth, Bitrate, Channels, MutSignals, SampleRate, Signal};

/// Samples per 20ms frame at 48kHz.
pub const SAMPLES_PER_FRAME: usize = 960;
/// Frame length in milliseconds.
pub const FRAME_LENGTH: usize = 20;
/// The three byte packet Discord reads as "nothing is playing": an Opus DTX frame. Sent as is
/// (never encoded) to fill the jitter buffer while paused, and on the way out.
pub const OPUS_SILENCE: [u8; 3] = [0xf8, 0xff, 0xfe];
/// Sampling rate Discord voice uses.
pub const SAMPLING_RATE: u32 = 48_000;
/// Number of channels Discord voice uses.
pub const CHANNELS: usize = 2;
#[derive(Debug, Clone, PartialEq)]
pub struct EncoderConfig {
    pub application: String,
    pub bitrate: u16,
    pub fec: bool,
    pub expected_packet_loss: f64,
    pub bandwidth: String,
    pub signal_type: String,
}

impl Default for EncoderConfig {
    fn default() -> Self {
        Self {
            application: "audio".to_owned(),
            bitrate: 128,
            fec: true,
            expected_packet_loss: 0.15,
            bandwidth: "full".to_owned(),
            signal_type: "auto".to_owned(),
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
        let application = match config.application.as_str() {
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

        let bandwidth = match config.bandwidth.as_str() {
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

        let signal = match config.signal_type.as_str() {
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

fn opus_err(err: oprs::Error) -> String {
    format!("Opus error: {err}")
}

/// Turn interleaved 16 bit little endian PCM bytes into samples.
pub fn bytes_to_samples(data: &[u8]) -> Vec<i16> {
    data.chunks_exact(2)
        .map(|chunk| i16::from_le_bytes([chunk[0], chunk[1]]))
        .collect()
}

/// Decode a raw Opus packet into interleaved 48kHz stereo `i16` samples.
pub struct OpusDecoder {
    inner: oprs::coder::Decoder,
    buffer: Vec<i16>,
}

impl OpusDecoder {
    pub fn new() -> Result<Self, String> {
        let inner =
            oprs::coder::Decoder::new(SampleRate::Hz48000, Channels::Stereo).map_err(opus_err)?;
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
        config.bandwidth = "ultra".to_owned();
        assert!(OpusEncoder::new(&config).is_err());

        let mut config = EncoderConfig::default();
        config.application = "speech".to_owned();
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
