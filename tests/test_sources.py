"""Audio source tests: `ac-ffmpeg` decoding, resampling, Opus pass-through and volume."""

from __future__ import annotations

import array
import io
import shutil
import subprocess
from pathlib import Path

import pytest

import discord_ext_voice as dev

from conftest import requires_ffmpeg, write_wav

FFMPEG = shutil.which('ffmpeg')


# ---------------------------------------------------------------------------- PCMAudio


def test_pcm_audio_reads_exact_frames():
    stream = io.BytesIO(b'\x01\x02' * (dev.FRAME_SIZE // 2 * 3))
    source = dev.PCMAudio(stream)
    frames = []
    while True:
        data = source.read()
        if not data:
            break
        frames.append(data)
    assert len(frames) == 3
    assert all(len(frame) == dev.FRAME_SIZE for frame in frames)


def test_pcm_audio_is_not_opus():
    assert dev.PCMAudio(io.BytesIO(b'')).is_opus() is False


# ---------------------------------------------------------------------------- probe


def test_probe_reports_codec_and_geometry(sine_wav: Path):
    codec, sample_rate, channels, duration = dev.probe(sine_wav)
    assert codec == 'pcm_s16le'
    assert sample_rate == 48000
    assert channels == 2
    assert duration == pytest.approx(0.5, abs=0.1)


def test_probe_rejects_a_missing_file(tmp_path: Path):
    with pytest.raises(dev.FFmpegError):
        dev.probe(tmp_path / 'nope.wav')


def test_probe_rejects_a_non_media_file(tmp_path: Path):
    bogus = tmp_path / 'file.txt'
    bogus.write_text('this is definitely not audio')
    with pytest.raises(dev.FFmpegError):
        dev.probe(bogus)


# ---------------------------------------------------------------------------- FFmpegPCMAudio


def test_ffmpeg_pcm_audio_decodes_twenty_millisecond_frames(sine_wav: Path):
    source = dev.FFmpegPCMAudio(str(sine_wav))
    try:
        frames = 0
        while True:
            data = source.read()
            if not data:
                break
            assert len(data) in (dev.FRAME_SIZE,)
            frames += 1
        # 0.5s / 20ms == 25 frames
        assert frames == pytest.approx(25, abs=1)
    finally:
        source.cleanup()


def test_ffmpeg_pcm_audio_resamples_and_upmixes(mono_wav: Path):
    """44.1kHz mono must come out as 48kHz stereo 20ms frames."""
    source = dev.FFmpegPCMAudio(str(mono_wav))
    try:
        total = 0
        while True:
            data = source.read()
            if not data:
                break
            assert len(data) == dev.FRAME_SIZE
            total += len(data)
        assert total == pytest.approx(dev.FRAME_SIZE * 25, abs=dev.FRAME_SIZE)
    finally:
        source.cleanup()


def test_ffmpeg_pcm_audio_skips_with_seek_options(sine_wav: Path):
    full = dev.FFmpegPCMAudio(str(sine_wav))
    try:
        full_bytes = 0
        while (chunk := full.read()) :
            full_bytes += len(chunk)
    finally:
        full.cleanup()

    trimmed = dev.FFmpegPCMAudio(str(sine_wav), before_options='-ss 0.25')
    try:
        trimmed_bytes = 0
        while (chunk := trimmed.read()):
            trimmed_bytes += len(chunk)
    finally:
        trimmed.cleanup()

    assert trimmed_bytes < full_bytes
    assert trimmed_bytes == pytest.approx(full_bytes / 2, abs=dev.FRAME_SIZE * 2)


def test_ffmpeg_pcm_audio_reads_from_a_file_object(sine_wav: Path):
    with open(sine_wav, 'rb') as handle:
        source = dev.FFmpegPCMAudio(handle)
        try:
            assert len(source.read()) == dev.FRAME_SIZE
        finally:
            source.cleanup()


def test_ffmpeg_pcm_audio_rejects_a_bad_source(tmp_path: Path):
    with pytest.raises(dev.FFmpegError):
        dev.FFmpegPCMAudio(str(tmp_path / 'missing.wav'))


def test_ffmpeg_pcm_audio_fails_fast_for_a_missing_file(tmp_path: Path):
    """Unlike discord.py this raises at construction instead of on the first read."""
    with pytest.raises(dev.FFmpegSourceError) as info:
        dev.FFmpegPCMAudio(tmp_path / 'missing.wav')
    assert isinstance(info.value, dev.ClientException)


@requires_ffmpeg
def test_ffmpeg_pcm_audio_reports_a_non_media_file(tmp_path: Path):
    bogus = tmp_path / 'file.txt'
    bogus.write_text('not audio at all')
    # The in-process decoder fails, the ffmpeg CLI fallback fails too, and discord.py records
    # that in `_current_error`, which `AudioPlayer` turns into the `after` callback's error.
    source = dev.FFmpegPCMAudio(bogus)
    try:
        while source.read():
            pass
        assert isinstance(source._current_error, dev.ClientException)
    finally:
        source.cleanup()


@requires_ffmpeg
def test_ffmpeg_pcm_audio_cli_fallback(sine_wav: Path):
    source = dev.FFmpegPCMAudio(str(sine_wav), force_cli=True)
    try:
        assert len(source.read()) == dev.FRAME_SIZE
    finally:
        source.cleanup()


@requires_ffmpeg
def test_ffmpeg_pcm_audio_cli_fallback_pipes_stdin(sine_wav: Path):
    """`pipe=True` writes a file-like source into the ffmpeg process, like discord.py."""
    with open(sine_wav, 'rb') as handle:
        source = dev.FFmpegPCMAudio(handle, pipe=True)
        try:
            # The audio comes back as raw PCM frames.
            for _ in range(10):
                data = source.read()
                if not data:
                    break
                assert len(data) == dev.FRAME_SIZE
        finally:
            source.cleanup()


@requires_ffmpeg
def test_ffmpeg_pcm_audio_rejects_a_path_when_piping(sine_wav: Path):
    with pytest.raises(TypeError):
        dev.FFmpegPCMAudio(str(sine_wav), pipe=True)


# ---------------------------------------------------------------------------- FFmpegOpusAudio


@pytest.fixture
def opus_file(tmp_path: Path, sine_wav: Path) -> Path:
    if FFMPEG is None:
        pytest.skip('ffmpeg is required')
    path = tmp_path / 'sine.opus'
    subprocess.run(
        [FFMPEG, '-y', '-loglevel', 'error', '-i', str(sine_wav), '-c:a', 'libopus', str(path)],
        check=True,
    )
    return path


def test_ffmpeg_opus_audio_passes_opus_packets_through(opus_file: Path):
    source = dev.FFmpegOpusAudio(str(opus_file))
    try:
        assert source.is_opus() is True
        decoder = dev.opus.Decoder()
        packets = 0
        while True:
            packet = source.read()
            if not packet:
                break
            # Each packet must be a real Opus frame.
            assert len(decoder.decode(packet)) == dev.FRAME_SIZE
            packets += 1
            if packets > 30:
                break
        assert packets > 5
    finally:
        source.cleanup()


def test_ffmpeg_opus_audio_falls_back_to_pcm_for_non_opus(sine_wav: Path):
    """A WAV input cannot be passed through, so libopus encoding is used instead."""
    source = dev.FFmpegOpusAudio(str(sine_wav))
    try:
        assert source.is_opus() is False
        assert len(source.read()) == dev.FRAME_SIZE
    finally:
        source.cleanup()


async def test_ffmpeg_opus_audio_probe(opus_file: Path):
    codec, bitrate = await dev.FFmpegOpusAudio.probe(str(opus_file))
    assert codec is not None and codec.startswith('opus')

    source = await dev.FFmpegOpusAudio.from_probe(str(opus_file))
    try:
        assert source.is_opus() is True
    finally:
        source.cleanup()


# ---------------------------------------------------------------------------- volume


def test_volume_transformer_scales_samples(sine_wav: Path):
    original = dev.FFmpegPCMAudio(str(sine_wav))
    transformer = dev.PCMVolumeTransformer(original, 0.5)
    try:
        data = transformer.read()
        assert len(data) == dev.FRAME_SIZE
        assert transformer.volume == 0.5

        samples = array.array('h')
        samples.frombytes(data)
        assert max(abs(sample) for sample in samples) < 16000  # 0.5 * 0.5 * 32767

        transformer.volume = -1.0
        assert transformer.volume == 0.0
    finally:
        transformer.cleanup()


@pytest.mark.filterwarnings('ignore::pytest.PytestUnraisableExceptionWarning')
def test_volume_transformer_rejects_opus_sources(opus_file: Path):
    # `discord.PCMVolumeTransformer` raises before setting `self.original`, so its
    # `AudioSource.__del__` -> `cleanup()` path raises again during garbage collection.
    # That is discord.py's behaviour, reused verbatim.
    source = dev.FFmpegOpusAudio(str(opus_file))
    try:
        with pytest.raises(dev.ClientException):
            dev.PCMVolumeTransformer(source)
    finally:
        source.cleanup()


def test_volume_transformer_forwards_cleanup():
    """`discord.PCMVolumeTransformer` is reused as-is, so cleanup must still be forwarded."""
    cleaned: list[bool] = []

    class Source(dev.AudioSource):
        def read(self) -> bytes:
            return b'\x00' * dev.FRAME_SIZE

        def cleanup(self) -> None:
            cleaned.append(True)

    transformer = dev.PCMVolumeTransformer(Source(), 0.5)
    transformer.cleanup()
    assert cleaned == [True]
