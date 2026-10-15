//! Python exception types exposed by the native module.
//!
//! The hierarchy mirrors the errors that `discord.py`'s voice implementation can raise so
//! that callers can catch the same names whether they run on the pure-Python or the native
//! backend.

use pyo3::create_exception;
use pyo3::exceptions::PyException;

create_exception!(
    _native,
    VoiceError,
    PyException,
    "Base class for every error raised by discord_ext_voice."
);
create_exception!(
    _native,
    NotConnected,
    VoiceError,
    "Raised when an operation requires a voice connection that is not established."
);
create_exception!(
    _native,
    AlreadyConnected,
    VoiceError,
    "Raised when connecting to a voice channel that is already connected."
);
create_exception!(
    _native,
    HandshakeError,
    VoiceError,
    "Raised when the voice handshake could not be completed in time."
);
create_exception!(
    _native,
    ConnectionClosedError,
    VoiceError,
    "Raised when the voice websocket was closed by Discord."
);
create_exception!(
    _native,
    ProtocolError,
    VoiceError,
    "Raised when Discord sent a payload that does not match the voice gateway schema."
);
create_exception!(
    _native,
    EncryptionError,
    VoiceError,
    "Raised when an RTP packet could not be encrypted or decrypted."
);
create_exception!(
    _native,
    DaveError,
    VoiceError,
    "Raised when the DAVE end-to-end encryption session failed."
);
create_exception!(
    _native,
    OpusError,
    VoiceError,
    "Raised when the Opus codec reported an error."
);
create_exception!(
    _native,
    FFmpegError,
    VoiceError,
    "Raised when FFmpeg failed to open, demux or decode an audio source."
);

#[derive(Debug, thiserror::Error)]
pub enum CryptoError {
    #[error("invalid secret key length: expected 32 bytes, got {0}")]
    InvalidKeyLength(usize),
    #[error("the packet could not be encrypted")]
    Encrypt,
    #[error("the packet could not be decrypted")]
    Decrypt,
}

#[derive(Debug, thiserror::Error)]
pub enum DaveFailure {
    #[error("davey error: {0}")]
    Davey(String),
}

#[derive(Debug, thiserror::Error)]
pub enum TransportError {
    #[error("websocket error: {0}")]
    WebSocket(String),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("protocol error: {0}")]
    Protocol(String),
}
