"""Opus codec tests (the native `oprs` encoder/decoder)."""

from __future__ import annotations

import array
import math
import os

import pytest

import discord_ext_voice as dev


def test_encoder_produces_a_small_packet_for_silence():
    encoder = dev.opus.Encoder()
    packet = encoder.encode(b'\x00' * dev.FRAME_SIZE)
    assert isinstance(packet, bytes)
    assert 0 < len(packet) < 200


def test_encoder_with_explicit_frame_size():
    encoder = dev.opus.Encoder()
    packet = encoder.encode(b'\x00' * dev.FRAME_SIZE, 960)
    assert 0 < len(packet) < 200


def test_decoder_roundtrip_yields_one_frame_of_pcm():
    encoder = dev.opus.Encoder(application='voip', bitrate=96)
    decoder = dev.opus.Decoder()

    import array
    import math

    samples = array.array('h')
    for index in range(dev.SAMPLES_PER_FRAME * dev.CHANNELS):
        samples.append(int(8000 * math.sin(2 * math.pi * 440 * index / 48000)))

    packet = encoder.encode(samples.tobytes())
    pcm = decoder.decode(packet)
    assert len(pcm) == dev.FRAME_SIZE


def test_decoder_conceals_a_lost_packet():
    decoder = dev.opus.Decoder()
    pcm = decoder.decode_plc()
    assert len(pcm) == dev.FRAME_SIZE


def test_decoder_rejects_garbage():
    decoder = dev.opus.Decoder()
    with pytest.raises(dev.OpusError):
        decoder.decode(b'\xff' * 500)


def test_silence_packet_is_valid_opus():
    decoder = dev.opus.Decoder()
    assert len(decoder.decode(dev.OPUS_SILENCE)) == dev.FRAME_SIZE


def test_encoder_mutation_helpers():
    encoder = dev.opus.Encoder()
    encoder.set_bitrate(256)
    encoder.set_bitrate(1)  # clamped to 16
    encoder.set_fec(False)
    encoder.set_expected_packet_loss_percent(0.5)
    encoder.set_bandwidth('wide')
    encoder.set_signal_type('music')
    assert encoder.encode(b'\x00' * dev.FRAME_SIZE)


def test_encoder_mutation_helpers_reject_unknown_values():
    encoder = dev.opus.Encoder()
    with pytest.raises(KeyError):
        encoder.set_bandwidth('ultra')
    with pytest.raises(KeyError):
        encoder.set_signal_type('loud')


@pytest.mark.parametrize('application', ['audio', 'voip', 'lowdelay'])
def test_all_applications_build(application):
    assert dev.opus.Encoder(application=application).encode(b'\x00' * dev.FRAME_SIZE)


@pytest.mark.parametrize('bandwidth', ['narrow', 'medium', 'wide', 'superwide', 'full'])
def test_all_bandwidths_build(bandwidth):
    assert dev.opus.Encoder(bandwidth=bandwidth).encode(b'\x00' * dev.FRAME_SIZE)


@pytest.mark.parametrize('signal', ['auto', 'voice', 'music'])
def test_all_signal_types_build(signal):
    assert dev.opus.Encoder(signal_type=signal).encode(b'\x00' * dev.FRAME_SIZE)


def test_native_encoder_needs_no_load_opus():
    """`discord.opus` needs `load_opus` before its ctypes encoder can be built; ours cannot."""
    import discord.opus

    assert dev.opus.Encoder is not discord.opus.Encoder
    assert len(dev.opus.Encoder().encode(b'\x00' * dev.FRAME_SIZE)) > 0


def test_silence_packet_matches_discord_py():
    import discord.opus

    assert dev.OPUS_SILENCE == discord.opus.OPUS_SILENCE


# ---------------------------------------------------------------------------- the libopus oracle


@pytest.mark.skipif(
    os.environ.get('DISCORD_EXT_VOICE_OPUS_ORACLE') != '1',
    reason='set DISCORD_EXT_VOICE_OPUS_ORACLE=1 to compare against the system libopus',
)
@pytest.mark.parametrize(
    ('application', 'bitrate', 'fec', 'bandwidth'),
    [('voip', 96, True, 'full'), ('audio', 128, True, 'full'), ('lowdelay', 64, False, 'wide')],
)
def test_the_encoder_matches_the_system_libopus(application, bitrate, fec, bandwidth):
    """`oprs` is a port of libopus 1.6.1, and this is what proves it in the build that ships.

    The packets and the decoded PCM have to be byte-identical to what the system libopus produces
    for the same input, otherwise the audio Discord receives would change with this dependency. It
    only means anything while the system libopus is the version `oprs` reproduces (1.6.1), which is
    why it is opt-in: a CI image with a different libopus would fail it for no good reason.
    """
    import discord.opus

    pytest.importorskip('discord')
    try:
        discord.opus.load_opus('libopus.so.0')
    except Exception as exc:  # no system libopus to compare against
        pytest.skip(f'the system libopus is not loadable: {exc}')

    samples = array.array('h')
    for index in range(dev.SAMPLES_PER_FRAME * dev.CHANNELS):
        samples.append(int(9000 * math.sin(2 * math.pi * 440 * index / 48000)))
    pcm = samples.tobytes()

    ours = dev.opus.Encoder(application=application, bitrate=bitrate, fec=fec, bandwidth=bandwidth)
    theirs = discord.opus.Encoder(
        application=application, bitrate=bitrate, fec=fec, bandwidth=bandwidth
    )
    packets = [
        (ours.encode(pcm), theirs.encode(pcm, dev.SAMPLES_PER_FRAME)) for _ in range(5)
    ]
    assert all(ours_packet == theirs_packet for ours_packet, theirs_packet in packets)

    our_decoder = dev.opus.Decoder()
    their_decoder = discord.opus.Decoder()
    for ours_packet, theirs_packet in packets:
        assert our_decoder.decode(ours_packet) == their_decoder.decode(theirs_packet)
