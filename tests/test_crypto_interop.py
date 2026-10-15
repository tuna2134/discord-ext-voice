"""Wire-format interoperability with PyNaCl, discord.py's encryption backend.

These tests prove that the native RTP encryption produces packets PyNaCl can decrypt, and that
packets PyNaCl produces are accepted by the native decryptor — i.e. that this library is a
drop-in replacement on the wire.

PyNaCl is a *test* dependency only: the library's encryption is native, so it needs no Python
crypto bindings at runtime.
"""

from __future__ import annotations

import struct

import pytest

pytest.importorskip('nacl.secret', reason='PyNaCl is required for the wire-format tests')
pytest.importorskip('nacl.utils')

import nacl.secret  # noqa: E402
import nacl.utils  # noqa: E402

import discord_ext_voice as dev  # noqa: E402

from fake_gateway import SSRC, open_connection  # noqa: E402

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------- encryption


def _pynacl_decrypt(mode: str, key: bytes, header: bytes, body: bytes) -> bytes:
    if mode == 'aead_xchacha20_poly1305_rtpsize':
        nonce = body[-4:] + b'\x00' * 20
        return nacl.secret.Aead(key).decrypt(body[:-4], header, nonce)
    if mode == 'xsalsa20_poly1305_lite':
        nonce = body[-4:] + b'\x00' * 20
        return nacl.secret.SecretBox(key).decrypt(body[:-4], nonce)
    if mode == 'xsalsa20_poly1305_suffix':
        return nacl.secret.SecretBox(key).decrypt(body[:-24], body[-24:])
    if mode == 'xsalsa20_poly1305':
        return nacl.secret.SecretBox(key).decrypt(body, header + b'\x00' * 12)
    raise AssertionError(f'unknown mode {mode}')


def _pynacl_encrypt(mode: str, key: bytes, header: bytes, plaintext: bytes, counter: int) -> bytes:
    prefix = struct.pack('>I', counter)
    if mode == 'aead_xchacha20_poly1305_rtpsize':
        nonce = prefix + b'\x00' * 20
        encrypted = nacl.secret.Aead(key).encrypt(plaintext, header, nonce)
        # `Aead.encrypt` returns nonce + ciphertext + tag; Discord wants the latter only.
        return bytes(encrypted.ciphertext) + prefix
    if mode == 'xsalsa20_poly1305_lite':
        nonce = prefix + b'\x00' * 20
        return bytes(nacl.secret.SecretBox(key).encrypt(plaintext, nonce).ciphertext) + prefix
    if mode == 'xsalsa20_poly1305_suffix':
        nonce = nacl.utils.random(24)
        return bytes(nacl.secret.SecretBox(key).encrypt(plaintext, nonce).ciphertext) + nonce
    if mode == 'xsalsa20_poly1305':
        return bytes(nacl.secret.SecretBox(key).encrypt(plaintext, header + b'\x00' * 12).ciphertext)
    raise AssertionError(f'unknown mode {mode}')


@pytest.mark.parametrize('mode', list(dev.SUPPORTED_MODES))
async def test_rtp_packets_are_decryptable_by_pynacl(gateway_factory, mode):
    gateway = await gateway_factory(modes=[mode])
    conn = await open_connection(gateway)

    try:
        assert conn.mode == mode
        conn.send_audio_packet(b'\x00' * dev.FRAME_SIZE, encode=True)
        packets = await gateway.wait_for_rtp(count=1, timeout=10.0)

        header, body = packets[0][:12], packets[0][12:]
        assert header[0] == 0x80
        assert header[1] == 0x78
        assert struct.unpack('>H', header[2:4])[0] == 1  # first sequence number
        assert struct.unpack('>I', header[4:8])[0] == 0  # first timestamp
        assert struct.unpack('>I', header[8:12])[0] == SSRC

        plaintext = _pynacl_decrypt(mode, bytes(gateway.secret_key), header, body)
        # The payload must be a real Opus frame that decodes to one 20ms frame.
        decoder = dev.opus.Decoder()
        assert len(decoder.decode(plaintext)) == dev.FRAME_SIZE
    finally:
        await conn.close()


@pytest.mark.parametrize('mode', list(dev.SUPPORTED_MODES))
async def test_rtp_packets_encrypted_by_pynacl_are_accepted(gateway_factory, mode):
    gateway = await gateway_factory(modes=[mode])
    conn = await open_connection(gateway)

    try:
        header = bytearray(12)
        header[0] = 0x80
        header[1] = 0x78
        struct.pack_into('>H', header, 2, 42)
        struct.pack_into('>I', header, 4, 960)
        struct.pack_into('>I', header, 8, SSRC)

        plaintext = dev.opus.Encoder().encode(b'\x00' * dev.FRAME_SIZE)
        body = _pynacl_encrypt(mode, bytes(gateway.secret_key), bytes(header), plaintext, counter=99)
        assert bytes(conn.decrypt_rtp(bytes(header), body)) == plaintext
    finally:
        await conn.close()


async def test_reference_packet_bytes_match_discord_py_layout(gateway_factory):
    """The `rtpsize` layout is header + ciphertext + tag + 4 byte nonce."""
    gateway = await gateway_factory(modes=['aead_xchacha20_poly1305_rtpsize'])
    conn = await open_connection(gateway)

    try:
        conn.send_audio_packet(b'\x00' * dev.FRAME_SIZE, encode=True)
        packets = await gateway.wait_for_rtp(count=1, timeout=10.0)
        packet = packets[0]
        # 12 byte header + opus payload + 16 byte tag + 4 byte nonce
        assert len(packet) > 12 + 16 + 4
        assert packet[12:-20] != b''
    finally:
        await conn.close()
