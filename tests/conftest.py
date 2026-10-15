"""Shared pytest fixtures and helpers."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
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
