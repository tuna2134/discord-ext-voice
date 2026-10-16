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
//! * [`player`] — the playback clock, reading an `AudioSource` and pacing 20ms frames.
//! * [`flow`] — the connection flow's decisions: phase transitions, close codes, retries.
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
mod flow;
mod logging;
mod opus;
mod player;
mod source;
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
    module.add_class::<source::PyDecodeSource>()?;
    module.add_class::<source::PyOpusSource>()?;
    module.add_class::<opus::PyOpusEncoder>()?;
    module.add_class::<opus::PyOpusDecoder>()?;
    module.add_class::<player::PyAudioPlayer>()?;
    module.add_class::<flow::PyConnectionFlow>()?;
    module.add_function(wrap_pyfunction!(source::probe, module)?)?;
    module.add_function(wrap_pyfunction!(source::scale_pcm, module)?)?;

    // Constants the Python layer reads. Frame geometry lives on `OpusEncoder`.
    module.add("OPUS_SILENCE", PyBytes::new(py, &opus::OPUS_SILENCE))?;
    module.add(
        "DAVE_PROTOCOL_VERSION",
        dave::DaveState::max_protocol_version(),
    )?;
    module.add("SUPPORTED_MODES", connection::supported_modes())?;
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
