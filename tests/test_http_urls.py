"""`http(s)` URLs as audio sources: reqwest streams them straight into libav."""

from __future__ import annotations

import pytest

import discord_ext_voice as dev
from discord_ext_voice import _native


def read_frames(source, limit: int = 500) -> list[bytes]:
    frames: list[bytes] = []
    try:
        while len(frames) < limit:
            data = source.read()
            if not data:
                break
            frames.append(data)
    finally:
        source.cleanup()
    return frames


# ---------------------------------------------------------------------------- decoding


def test_http_url_decodes_frames(sine_wav, http_server):
    base = http_server(sine_wav.parent)
    source = dev.FFmpegPCMAudio(f'{base}/{sine_wav.name}')

    frames = read_frames(source)
    assert len(frames) > 20, 'the whole 0.5s file has to come through'
    assert all(len(frame) == dev.FRAME_SIZE for frame in frames)
    assert source.is_opus() is False


def test_http_url_reports_a_missing_file(sine_wav, http_server):
    base = http_server(sine_wav.parent)
    with pytest.raises(dev.FFmpegSourceError):
        dev.FFmpegPCMAudio(f'{base}/does-not-exist.wav')


def test_http_url_reports_a_dead_host():
    # Nothing listens on port 1, so the first read fails and the source reports why.
    with pytest.raises(dev.FFmpegSourceError):
        dev.FFmpegPCMAudio('http://127.0.0.1:1/song.wav')


def test_http_url_probe(sine_wav, http_server):
    base = http_server(sine_wav.parent)
    codec, sample_rate, channels, duration = dev.probe(f'{base}/{sine_wav.name}')
    assert codec == 'pcm_s16le'
    assert sample_rate == 48000
    assert channels == 2
    assert duration == pytest.approx(0.5, abs=0.1)


# ---------------------------------------------------------------------------- seeking


def test_http_url_seeks(sine_wav, http_server):
    base = http_server(sine_wav.parent)
    url = f'{base}/{sine_wav.name}'

    # A start offset, exactly like the local-file case.
    trimmed = dev.FFmpegPCMAudio(url, seek=0.25)
    trimmed_frames = len(read_frames(trimmed))
    full_frames = len(read_frames(dev.FFmpegPCMAudio(url)))
    assert 0 < trimmed_frames < full_frames
    assert trimmed_frames == pytest.approx(full_frames / 2, abs=3)

    # Seeking backwards mid-stream has to re-request the input (a ranged request here, since
    # this server honours ranges). `seek=` only covers the start offset, so this drives the
    # native reader directly.
    reader = _native.DecodeSource(url)
    try:
        assert len(reader.read()) == dev.FRAME_SIZE
        reader.seek(0.05)
        assert len(reader.read()) == dev.FRAME_SIZE
    finally:
        reader.cleanup()


# ---------------------------------------------------------------------------- opus


def test_http_url_passes_opus_packets_through(opus_file, http_server):
    base = http_server(opus_file.parent)
    source = dev.FFmpegOpusAudio(f'{base}/{opus_file.name}')
    try:
        assert source.is_opus() is True
        decoder = dev.opus.Decoder()
        packets = 0
        while packets < 10:
            packet = source.read()
            if not packet:
                break
            assert len(decoder.decode(packet)) == dev.FRAME_SIZE
            packets += 1
        assert packets > 3
    finally:
        source.cleanup()
