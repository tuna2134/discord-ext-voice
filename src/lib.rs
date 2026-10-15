//! `discord_ext_voice._native` — the Rust core of a `discord.py`-compatible voice library.
//!
//! Layering:
//!
//! * [`transport`] — Discord voice websocket (tokio TcpStream -> tokio-rustls -> tokio-tungstenite)
//!   and the RTP UDP socket, including IP discovery and the heartbeat loop.
//! * [`crypto`] — the four Discord RTP encryption modes (`xsalsa20_poly1305*`,
//!   `aead_xchacha20_poly1305_rtpsize`).
//! * [`dave`] — the DAVE/MLS end-to-end encryption session built on `davey`.
//! * [`opus`] — Opus encode/decode built on `audiopus`.
//! * [`source`] — in-process FFmpeg decoding/resampling built on `ac-ffmpeg`.
//! * [`connection`] — the `VoiceConnection` Python object tying it all together.
//!
//! The protocol payload types come from the `voice-model` crate (published as
//! `serenity-voice-model` after its upstream rename).

use pyo3::prelude::*;
use pyo3::types::PyBytes;

mod connection;
mod crypto;
mod dave;
mod errors;
mod logging;
mod opus;
mod resample;
mod source;
mod transport;

pub(crate) use logging::{log_debug, log_error, log_info, log_warning};

/// The native extension module.
#[pymodule]
fn _native(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<connection::VoiceConnection>()?;
    module.add_class::<source::PyDecodeSource>()?;
    module.add_class::<source::PyOpusSource>()?;
    module.add_class::<opus::PyOpusEncoder>()?;
    module.add_class::<opus::PyOpusDecoder>()?;
    module.add_function(wrap_pyfunction!(source::probe, module)?)?;

    // Constants mirroring `discord.opus` and `discord.voice_client`.
    module.add(
        "DAVE_PROTOCOL_VERSION",
        dave::DaveState::max_protocol_version(),
    )?;
    module.add("SAMPLES_PER_FRAME", opus::SAMPLES_PER_FRAME)?;
    module.add("FRAME_LENGTH", opus::FRAME_LENGTH)?;
    module.add("SAMPLING_RATE", opus::SAMPLING_RATE)?;
    module.add("CHANNELS", opus::CHANNELS)?;
    module.add("OPUS_SILENCE", PyBytes::new(py, &opus::OPUS_SILENCE))?;
    module.add("SUPPORTED_MODES", connection::supported_modes())?;
    module.add("HAS_DAVE", true)?;
    module.add("HAS_NACL", true)?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;

    // Exception types.
    module.add("VoiceError", py.get_type::<errors::VoiceError>())?;
    module.add("NotConnected", py.get_type::<errors::NotConnected>())?;
    module.add(
        "AlreadyConnected",
        py.get_type::<errors::AlreadyConnected>(),
    )?;
    module.add("HandshakeError", py.get_type::<errors::HandshakeError>())?;
    module.add(
        "ConnectionClosedError",
        py.get_type::<errors::ConnectionClosedError>(),
    )?;
    module.add("ProtocolError", py.get_type::<errors::ProtocolError>())?;
    module.add("EncryptionError", py.get_type::<errors::EncryptionError>())?;
    module.add("DaveError", py.get_type::<errors::DaveError>())?;
    module.add("OpusError", py.get_type::<errors::OpusError>())?;
    module.add("FFmpegError", py.get_type::<errors::FFmpegError>())?;

    Ok(())
}
