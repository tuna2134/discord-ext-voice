//! `discord_ext_voice._native` — the Rust core of a `discord.py`-compatible voice library.
//!
//! Layering:
//!
//! * [`transport`] — Discord voice websocket (tokio TcpStream -> tokio-rustls -> tokio-tungstenite)
//!   and the RTP UDP socket, including IP discovery and the heartbeat loop.
//! * [`crypto`] — the four Discord RTP encryption modes (`xsalsa20_poly1305*`,
//!   `aead_xchacha20_poly1305_rtpsize`).
//! * [`dave`] — the DAVE/MLS end-to-end encryption session built on `davey`.
//! * [`opus`] — Opus encode/decode built on `oprs`, a pure-Rust port of libopus.
//! * [`source`] — in-process demuxing and decoding built on `symphonia`, resampling on `rubato`
//!   and Opus on `oprs`.
//! * [`sources`] — the Python audio-source classes built on [`source`].
//! * [`player`] — the playback clock, reading an `AudioSource` and pacing 20ms frames.
//! * [`flow`] — the connection flow's decisions: phase transitions, close codes, retries.
//! * [`connection`] — the `VoiceConnection` object tying the transport and the flow together.
//! * [`gateway`] — the tiny websocket facade `VoiceClient` holds.
//! * [`enums`] — the `IntEnum`s the Python side dispatches on.
//!
//! The protocol payload types come from the `voice-model` crate (published as
//! `serenity-voice-model` after its upstream rename).

use pyo3::prelude::*;
use pyo3::types::PyBytes;

mod connection;
mod crypto;
mod dave;
mod enums;
mod errors;
mod flow;
mod gateway;
mod logging;
mod opus;
mod player;
mod source;
mod sources;
mod transport;

pub(crate) use logging::{log_debug, log_error, log_info, log_warning};

/// Select the process-wide rustls crypto provider.
///
/// rustls cannot choose when more than one provider is compiled in (the dependency graph decides
/// that, not this crate), and `reqwest` — built with `rustls-no-provider`, see Cargo.toml — asks
/// for the process default instead of shipping one. Ring is the provider this crate enables, so
/// install it once, before anything can build a TLS client. A provider someone else installed
/// first is left alone.
fn install_crypto_provider() {
    let _ = rustls::crypto::ring::default_provider().install_default();
}

/// The native extension module.
#[pymodule]
fn _native(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    install_crypto_provider();

    module.add_class::<connection::VoiceConnection>()?;
    // The connection flow lives on the connection itself; the second name is kept because
    // `VoiceConnectionState` is the public name of the flow.
    module.add(
        "VoiceConnectionState",
        py.get_type::<connection::VoiceConnection>(),
    )?;
    module.add_class::<gateway::VoiceWebSocket>()?;
    module.add_class::<sources::AudioSource>()?;
    module.add_class::<sources::PcmAudio>()?;
    module.add_class::<sources::FfmpegPcmAudio>()?;
    module.add_class::<sources::FfmpegOpusAudio>()?;
    module.add_class::<sources::PcmVolumeTransformer>()?;
    // The raw readers stay available for tests and for code that wants to drive a demuxer
    // directly; the public source classes are built on the same internals.
    module.add_class::<source::PyDecodeSource>()?;
    module.add_class::<source::PyOpusSource>()?;
    module.add_class::<player::PyAudioPlayer>()?;

    module.add_function(wrap_pyfunction!(sources::is_audio_source, module)?)?;
    module.add_function(wrap_pyfunction!(sources::probe_async, module)?)?;
    module.add_function(wrap_pyfunction!(source::probe, module)?)?;
    module.add_function(wrap_pyfunction!(source::scale_pcm, module)?)?;
    module.add_function(wrap_pyfunction!(flow::retry_wait, module)?)?;
    module.add_function(wrap_pyfunction!(player::set_read_ahead, module)?)?;
    module.add_function(wrap_pyfunction!(player::set_read_stall_timeout, module)?)?;

    // Constants the Python layer reads.
    module.add("OPUS_SILENCE", PyBytes::new(py, &opus::OPUS_SILENCE))?;
    module.add("SAMPLES_PER_FRAME", opus::SAMPLES_PER_FRAME)?;
    module.add("FRAME_LENGTH", opus::FRAME_LENGTH)?;
    module.add("SAMPLING_RATE", opus::SAMPLING_RATE)?;
    module.add("CHANNELS", opus::CHANNELS)?;
    module.add("SAMPLE_SIZE", opus::CHANNELS * 2)?;
    module.add("FRAME_SIZE", opus::SAMPLES_PER_FRAME * opus::CHANNELS * 2)?;
    module.add(
        "DAVE_PROTOCOL_VERSION",
        dave::DaveState::max_protocol_version(),
    )?;
    module.add("SUPPORTED_MODES", connection::supported_modes())?;
    module.add("MAX_ATTEMPTS", flow::MAX_ATTEMPTS)?;
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
    module.add(
        "FFmpegSourceError",
        py.get_type::<errors::FFmpegSourceError>(),
    )?;

    // Dispatched on by name from Python.
    enums::install(py, module)?;

    Ok(())
}
