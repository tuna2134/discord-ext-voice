//! Discord voice transport encryption.
//!
//! Discord negotiates one of four RTP payload encryption modes. Three of them are the classic
//! NaCl `secretbox` (XSalsa20-Poly1305) variants that use the 12 byte RTP header (or a random
//! 24 byte value) as the nonce, and the modern default is
//! `aead_xchacha20_poly1305_rtpsize`, which authenticates the RTP header as additional data and
//! appends a 4 byte truncated nonce to the packet.
//!
//! The layouts below are byte-for-byte identical to the ones produced by the `PyNaCl` based
//! implementation in `discord.py` so that a bot can switch between the two freely.

// Two AEAD crates, two `aead` versions: `chacha20poly1305` 0.11 uses aead 0.6, whose in-place
// trait is `AeadInOut`, while `xsalsa20poly1305` 0.9 (the last release) still uses aead 0.5 and
// its `AeadInPlace`. Both traits are imported anonymously: each cipher type implements exactly
// one of them, so a method call resolves to the right one.
use chacha20poly1305::aead::AeadInOut as _;
use chacha20poly1305::aead::KeyInit as _;
use chacha20poly1305::{Key as ChachaKey, XChaCha20Poly1305, XNonce};
use rand::Rng;
use xsalsa20poly1305::AeadInPlace as _;
use xsalsa20poly1305::KeyInit as _;
use xsalsa20poly1305::{Key as SalsaKey, Nonce as SalsaNonce, XSalsa20Poly1305};

use crate::errors::CryptoError;

/// The encryption modes understood by this library, in the same order of preference that
/// `discord.py` advertises them.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    AeadXChaCha20Poly1305Rtpsize,
    XSalsa20Poly1305Lite,
    XSalsa20Poly1305Suffix,
    XSalsa20Poly1305,
}

impl Mode {
    /// Wire representation used by `SELECT_PROTOCOL` / `SESSION_DESCRIPTION`.
    pub const fn as_str(self) -> &'static str {
        match self {
            Mode::AeadXChaCha20Poly1305Rtpsize => "aead_xchacha20_poly1305_rtpsize",
            Mode::XSalsa20Poly1305Lite => "xsalsa20_poly1305_lite",
            Mode::XSalsa20Poly1305Suffix => "xsalsa20_poly1305_suffix",
            Mode::XSalsa20Poly1305 => "xsalsa20_poly1305",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        Some(match value {
            "aead_xchacha20_poly1305_rtpsize" => Mode::AeadXChaCha20Poly1305Rtpsize,
            "xsalsa20_poly1305_lite" => Mode::XSalsa20Poly1305Lite,
            "xsalsa20_poly1305_suffix" => Mode::XSalsa20Poly1305Suffix,
            "xsalsa20_poly1305" => Mode::XSalsa20Poly1305,
            _ => return None,
        })
    }

    /// The modes this library supports, most preferred first.
    pub const fn supported() -> [Mode; 4] {
        [
            Mode::AeadXChaCha20Poly1305Rtpsize,
            Mode::XSalsa20Poly1305Lite,
            Mode::XSalsa20Poly1305Suffix,
            Mode::XSalsa20Poly1305,
        ]
    }
}

impl std::fmt::Display for Mode {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Stateful RTP payload encryptor, tracking the incrementing nonce used by the `lite` and
/// `rtpsize` modes.
pub struct PacketEncryptor {
    mode: Mode,
    /// `discord.py` calls this `_incr_nonce`; it wraps at `u32::MAX`.
    counter: u32,
    chacha: XChaCha20Poly1305,
    salsa: XSalsa20Poly1305,
}

impl std::fmt::Debug for PacketEncryptor {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PacketEncryptor")
            .field("mode", &self.mode)
            .field("counter", &self.counter)
            .finish_non_exhaustive()
    }
}

impl PacketEncryptor {
    pub fn new(mode: Mode, secret_key: &[u8]) -> Result<Self, CryptoError> {
        let key: [u8; 32] = secret_key
            .try_into()
            .map_err(|_| CryptoError::InvalidKeyLength(secret_key.len()))?;

        Ok(Self {
            mode,
            counter: 0,
            chacha: XChaCha20Poly1305::new(&ChachaKey::from(key)),
            salsa: XSalsa20Poly1305::new(SalsaKey::from_slice(&key)),
        })
    }

    /// Increment the nonce counter with the same wraparound semantics as
    /// `VoiceClient.checked_add('_incr_nonce', 1, 4294967295)`.
    fn next_counter(&mut self) -> u32 {
        let current = self.counter;
        if u64::from(current) + 1 > u64::from(u32::MAX) {
            self.counter = 0;
        } else {
            self.counter = current + 1;
        }
        current
    }

    /// Encrypt an Opus payload and prepend the RTP header, returning the full UDP payload.
    ///
    /// `header` must be the 12 byte RTP header, `data` the (already DAVE-encrypted, if
    /// applicable) Opus frame.
    pub fn encrypt(&mut self, header: &[u8], data: &[u8]) -> Result<Vec<u8>, CryptoError> {
        match self.mode {
            Mode::AeadXChaCha20Poly1305Rtpsize => {
                let nonce = self.next_counter();
                let mut full_nonce = [0u8; 24];
                full_nonce[..4].copy_from_slice(&nonce.to_be_bytes());

                let mut buffer = data.to_vec();
                self.chacha
                    .encrypt_in_place(&XNonce::from(full_nonce), header, &mut buffer)
                    .map_err(|_| CryptoError::Encrypt)?;

                let mut out = Vec::with_capacity(header.len() + buffer.len() + 4);
                out.extend_from_slice(header);
                out.extend_from_slice(&buffer);
                out.extend_from_slice(&full_nonce[..4]);
                Ok(out)
            }
            Mode::XSalsa20Poly1305Lite => {
                let nonce = self.next_counter();
                let mut full_nonce = [0u8; 24];
                full_nonce[..4].copy_from_slice(&nonce.to_be_bytes());

                let mut buffer = data.to_vec();
                self.salsa
                    .encrypt_in_place(SalsaNonce::from_slice(&full_nonce), b"", &mut buffer)
                    .map_err(|_| CryptoError::Encrypt)?;

                let mut out = Vec::with_capacity(header.len() + buffer.len() + 4);
                out.extend_from_slice(header);
                out.extend_from_slice(&buffer);
                out.extend_from_slice(&full_nonce[..4]);
                Ok(out)
            }
            Mode::XSalsa20Poly1305Suffix => {
                let mut full_nonce = [0u8; 24];
                rand::rng().fill_bytes(&mut full_nonce);

                let mut buffer = data.to_vec();
                self.salsa
                    .encrypt_in_place(SalsaNonce::from_slice(&full_nonce), b"", &mut buffer)
                    .map_err(|_| CryptoError::Encrypt)?;

                let mut out = Vec::with_capacity(header.len() + buffer.len() + 24);
                out.extend_from_slice(header);
                out.extend_from_slice(&buffer);
                out.extend_from_slice(&full_nonce);
                Ok(out)
            }
            Mode::XSalsa20Poly1305 => {
                // The nonce is the RTP header, zero padded to 24 bytes.
                let mut full_nonce = [0u8; 24];
                let len = header.len().min(12);
                full_nonce[..len].copy_from_slice(&header[..len]);

                let mut buffer = data.to_vec();
                self.salsa
                    .encrypt_in_place(SalsaNonce::from_slice(&full_nonce), b"", &mut buffer)
                    .map_err(|_| CryptoError::Encrypt)?;

                let mut out = Vec::with_capacity(header.len() + buffer.len());
                out.extend_from_slice(header);
                out.extend_from_slice(&buffer);
                Ok(out)
            }
        }
    }

    /// Decrypt a received RTP packet payload (without the RTP header, and with any trailing
    /// nonce bytes already accounted for by the caller).
    pub fn decrypt(&self, header: &[u8], payload: &[u8]) -> Result<Vec<u8>, CryptoError> {
        let mut buffer = payload.to_vec();
        match self.mode {
            Mode::AeadXChaCha20Poly1305Rtpsize | Mode::XSalsa20Poly1305Lite => {
                let mut full_nonce = [0u8; 24];
                let split = payload.len().saturating_sub(4);
                let (body, nonce) = payload.split_at(split);
                full_nonce[..4].copy_from_slice(nonce);
                buffer = body.to_vec();
                if self.mode == Mode::AeadXChaCha20Poly1305Rtpsize {
                    self.chacha
                        .decrypt_in_place(&XNonce::from(full_nonce), header, &mut buffer)
                        .map_err(|_| CryptoError::Decrypt)?;
                } else {
                    self.salsa
                        .decrypt_in_place(SalsaNonce::from_slice(&full_nonce), b"", &mut buffer)
                        .map_err(|_| CryptoError::Decrypt)?;
                }
            }
            Mode::XSalsa20Poly1305Suffix => {
                let split = payload.len().saturating_sub(24);
                let (body, nonce) = payload.split_at(split);
                buffer = body.to_vec();
                self.salsa
                    .decrypt_in_place(SalsaNonce::from_slice(nonce), b"", &mut buffer)
                    .map_err(|_| CryptoError::Decrypt)?;
            }
            Mode::XSalsa20Poly1305 => {
                let mut full_nonce = [0u8; 24];
                let len = header.len().min(12);
                full_nonce[..len].copy_from_slice(&header[..len]);
                self.salsa
                    .decrypt_in_place(SalsaNonce::from_slice(&full_nonce), b"", &mut buffer)
                    .map_err(|_| CryptoError::Decrypt)?;
            }
        }
        Ok(buffer)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn header() -> Vec<u8> {
        vec![
            0x80, 0x78, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0xde, 0xad, 0xbe, 0xef,
        ]
    }

    #[test]
    fn roundtrip_all_modes() {
        for mode in Mode::supported() {
            let key = [7u8; 32];
            let mut enc = PacketEncryptor::new(mode, &key).unwrap();
            let header = header();
            let plaintext = b"opus-payload-bytes".to_vec();
            let packet = enc.encrypt(&header, &plaintext).unwrap();

            assert!(
                packet.starts_with(&header),
                "{mode} must prepend the RTP header"
            );
            let body = &packet[header.len()..];
            let dec = PacketEncryptor::new(mode, &key).unwrap();
            let out = dec.decrypt(&header, body).unwrap();
            assert_eq!(out, plaintext, "mode {mode} did not round-trip");
        }
    }

    #[test]
    fn rtpsize_appends_four_byte_nonce() {
        let key = [3u8; 32];
        let mut enc = PacketEncryptor::new(Mode::AeadXChaCha20Poly1305Rtpsize, &key).unwrap();
        let header = header();
        let packet = enc.encrypt(&header, b"abc").unwrap();
        // header + ciphertext(3) + tag(16) + nonce(4)
        assert_eq!(packet.len(), header.len() + 3 + 16 + 4);
    }

    #[test]
    fn counter_wraps_like_discord_py() {
        let key = [1u8; 32];
        let mut enc = PacketEncryptor::new(Mode::XSalsa20Poly1305Lite, &key).unwrap();
        enc.counter = u32::MAX;
        let first = enc.next_counter();
        assert_eq!(first, u32::MAX);
        assert_eq!(enc.counter, 0);
    }

    #[test]
    fn rejects_short_keys() {
        assert!(PacketEncryptor::new(Mode::XSalsa20Poly1305, &[0u8; 16]).is_err());
    }
}
