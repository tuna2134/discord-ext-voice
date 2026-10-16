"""`http(s)` URLs as audio sources: reqwest streams them straight into the demuxer."""

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


def test_http_url_resumes_a_dropped_stream(sine_wav, http_server):
    """A server that closes the connection mid-body must not look like the end of the file.

    A throttling CDN drops an idle connection all the time, and the demuxer reads the end of the
    input as the end of the file — so playback used to stop with `FFmpeg demux error`. The reader
    asks for the rest from the byte it reached instead.
    """
    whole = http_server(sine_wav.parent)
    dropped = http_server(sine_wav.parent, truncate_first=4096)

    complete = read_frames(dev.FFmpegPCMAudio(f'{whole}/{sine_wav.name}'))
    resumed = read_frames(dev.FFmpegPCMAudio(f'{dropped}/{sine_wav.name}'))

    assert len(complete) > 20, 'the fixture has to be longer than one truncated response'
    assert resumed == complete, 'the resumed stream has to be the same audio'

    # The first response was cut short, and the rest was asked for by range.
    assert any('range' in request for request in http_server.requests[1:]), (
        http_server.requests
    )


# ---------------------------------------------------------------------------- headers


def test_http_url_sends_the_given_headers(sine_wav, http_server):
    """A URL from an extractor is usually bound to the client that asked for it."""
    base = http_server(sine_wav.parent)
    headers = {
        'User-Agent': 'yt-dlp/2026.08.19',
        'Referer': 'https://example.com/watch',
        'X-Extra': 'yes',
    }
    source = dev.FFmpegPCMAudio(f'{base}/{sine_wav.name}', headers=headers)
    read_frames(source, limit=1)

    assert http_server.requests, 'the server saw no request'
    first = http_server.requests[0]
    assert first['user-agent'] == 'yt-dlp/2026.08.19', 'a given User-Agent replaces ours'
    assert first['referer'] == 'https://example.com/watch'
    assert first['x-extra'] == 'yes'


def test_http_url_sends_them_on_every_request(sine_wav, http_server):
    """Every request the reader makes carries them, seeks included.

    A seek does not necessarily make a new request — symphonia can serve one from what it has
    already buffered, which is what happens for a file this small — so this asserts on the
    requests that were made rather than on their number.
    """
    base = http_server(sine_wav.parent)
    url = f'{base}/{sine_wav.name}'

    reader = _native.DecodeSource(url, headers={'X-Extra': 'seek'})
    try:
        assert len(reader.read()) == dev.FRAME_SIZE
        reader.seek(0.05)  # backwards: a ranged request, not a skip
        assert len(reader.read()) == dev.FRAME_SIZE
    finally:
        reader.cleanup()

    assert http_server.requests, 'the server saw no request'
    assert all(request.get('x-extra') == 'seek' for request in http_server.requests)


def test_http_url_headers_are_optional(sine_wav, http_server):
    base = http_server(sine_wav.parent)
    read_frames(dev.FFmpegPCMAudio(f'{base}/{sine_wav.name}'), limit=1)

    user_agent = http_server.requests[0]['user-agent']
    assert user_agent.startswith('discord-ext-voice/'), user_agent


def test_headers_are_rejected_where_they_cannot_be_sent(sine_wav, tmp_path):
    with pytest.raises(TypeError, match='only applies to an http'):
        dev.FFmpegPCMAudio(str(sine_wav), headers={'X-Extra': 'yes'})

    with pytest.raises(TypeError, match='mapping'):
        dev.FFmpegPCMAudio('https://example.com/a.mp3', headers=['X-Extra', 'yes'])

    with pytest.raises(TypeError, match='str to str'):
        dev.FFmpegPCMAudio('https://example.com/a.mp3', headers={'X-Extra': 1})

    with pytest.raises(ValueError, match='header name'):
        dev.FFmpegPCMAudio('https://example.com/a.mp3', headers={'not a name': 'yes'})

    with pytest.raises(ValueError, match='not valid'):
        dev.FFmpegPCMAudio('https://example.com/a.mp3', headers={'X-Extra': 'line\nbreak'})


def test_opus_sources_take_headers_too(opus_file, http_server):
    base = http_server(opus_file.parent)
    source = dev.FFmpegOpusAudio(f'{base}/{opus_file.name}', headers={'X-Extra': 'opus'})
    try:
        assert source.is_opus() is True
        assert len(source.read()) > 0
    finally:
        source.cleanup()

    assert all(request.get('x-extra') == 'opus' for request in http_server.requests)


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
