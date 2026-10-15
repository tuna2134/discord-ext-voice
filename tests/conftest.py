"""Shared pytest fixtures and helpers."""

from __future__ import annotations

import asyncio
import functools
import http.server
import os
import shutil
import subprocess
import sys
import threading
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_gateway import FakeVoiceGateway  # noqa: E402

FFMPEG = shutil.which('ffmpeg')


@pytest.fixture
async def gateway_factory():
    """Create fake voice gateways that are stopped when the test finishes."""

    created: list[FakeVoiceGateway] = []

    async def factory(**kwargs) -> FakeVoiceGateway:
        server = FakeVoiceGateway(**kwargs)
        await server.start()
        created.append(server)
        return server

    try:
        yield factory
    finally:
        for server in created:
            await server.stop()


class _LimitedReader:
    """Serve only the requested slice of a file (what a `Range` response means)."""

    def __init__(self, handle, remaining: int) -> None:
        self._handle = handle
        self._remaining = remaining

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b''
        want = self._remaining if size is None or size < 0 else min(size, self._remaining)
        data = self._handle.read(want)
        self._remaining -= len(data)
        return data

    def close(self) -> None:
        self._handle.close()


class _RangeHandler(http.server.SimpleHTTPRequestHandler):
    """A file server that honours `Range` requests, which is what seeking over HTTP uses."""

    def log_message(self, *args: object) -> None:
        pass

    def send_head(self):  # type: ignore[override]
        path = Path(self.translate_path(self.path))
        if not path.is_file():
            self.send_error(404, 'not found')
            return None

        assert path.stat
        size = path.stat().st_size
        start, end = 0, max(size - 1, 0)
        header = self.headers.get('Range')
        if header and header.startswith('bytes='):
            first = header[len('bytes=') :].split('-', 1)[0]
            if first.isdigit():
                start = min(int(first), end)
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        else:
            self.send_response(200)

        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        self.end_headers()

        handle = path.open('rb')
        handle.seek(start)
        return _LimitedReader(handle, end - start + 1)


@pytest.fixture
def http_server():
    """Serve a directory over HTTP, returning a factory for its base URL."""

    servers: list[http.server.ThreadingHTTPServer] = []

    def start(directory: Path) -> str:
        handler = functools.partial(_RangeHandler, directory=str(directory))
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f'http://127.0.0.1:{server.server_port}'

    try:
        yield start
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


@pytest.fixture
def opus_file(tmp_path: Path, sine_wav: Path) -> Path:
    """An Ogg Opus copy of :func:`sine_wav`, for the pass-through source."""
    if FFMPEG is None:
        pytest.skip('the ffmpeg binary is required to create an Opus file')

    path = tmp_path / 'sine.opus'
    subprocess.run(
        [FFMPEG, '-y', '-loglevel', 'error', '-i', str(sine_wav), '-c:a', 'libopus', str(path)],
        check=True,
    )
    return path


@pytest.fixture
def sine_wav(tmp_path: Path) -> Path:
    """A 0.5s 440Hz stereo 48kHz WAV file."""
    path = tmp_path / 'sine.wav'
    write_wav(path, seconds=0.5, sample_rate=48000, channels=2)
    return path


@pytest.fixture
def mono_wav(tmp_path: Path) -> Path:
    """A 0.5s 440Hz mono 44.1kHz WAV file, to exercise resampling."""
    path = tmp_path / 'mono.wav'
    write_wav(path, seconds=0.5, sample_rate=44100, channels=1)
    return path


def write_wav(path: Path, *, seconds: float, sample_rate: int, channels: int, frequency: float = 440.0) -> Path:
    import array
    import math

    frames = int(seconds * sample_rate)
    samples = array.array('h')
    for index in range(frames):
        value = int(0.5 * 32767 * math.sin(2 * math.pi * frequency * index / sample_rate))
        for _ in range(channels):
            samples.append(value)

    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())
    return path


requires_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason='the ffmpeg binary is required')
