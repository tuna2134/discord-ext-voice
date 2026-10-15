"""Audio source tests: `ac-ffmpeg` decoding, seeking, resampling, Opus pass-through, volume.

Everything here is this package's own source implementation, so the `ffmpeg` binary is only
ever used to *create* the Opus fixture; no test decodes through a subprocess.
"""

from __future__ import annotations

import array
import io
import shutil
import subprocess
from pathlib import Path

import pytest

import discord_ext_voice as dev

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


def test_pcm_audio_drops_a_partial_frame():
    """`discord.py`'s player reads an incomplete frame as the end of the source."""
    stream = io.BytesIO(b'\x00' * (dev.FRAME_SIZE + 100))
    source = dev.PCMAudio(stream)
    assert len(source.read()) == dev.FRAME_SIZE
    assert source.read() == b''


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


def test_ffmpeg_pcm_audio_seeks_natively(sine_wav: Path):
    full = dev.FFmpegPCMAudio(str(sine_wav))
    try:
        full_bytes = 0
        while (chunk := full.read()) :
            full_bytes += len(chunk)
    finally:
        full.cleanup()

    # `seek` is the native form of `-ss`: the demuxer moves and the decoder is rebuilt.
    trimmed = dev.FFmpegPCMAudio(str(sine_wav), seek=0.25)
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
    assert isinstance(info.value, dev.VoiceError)


def test_ffmpeg_pcm_audio_reports_a_non_media_file(tmp_path: Path):
    """With no CLI to fall back to, an undecodable input fails at construction."""
    bogus = tmp_path / 'file.txt'
    bogus.write_text('not audio at all')
    with pytest.raises(dev.FFmpegSourceError) as info:
        dev.FFmpegPCMAudio(bogus)
    assert isinstance(info.value, dev.FFmpegError)


def test_ffmpeg_pcm_audio_rejects_ffmpeg_cli_arguments(sine_wav: Path):
    """discord.py's FFmpeg-CLI arguments do not exist: there is no `ffmpeg` process."""
    for options in (
        {'before_options': '-ss 0.25'},
        {'options': '-vn'},
        {'executable': 'avconv'},
        {'pipe': True},
        {'stderr': True},
        {'force_cli': True},
    ):
        with pytest.raises(TypeError) as info:
            dev.FFmpegPCMAudio(str(sine_wav), **options)
        # The message names what was passed and points at the native alternative.
        assert next(iter(options)) in str(info.value)
        assert 'seek=' in str(info.value)


class _Unseekable(io.RawIOBase):
    """A read-only stream that reports itself as unseekable, like a pipe or a socket."""

    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def readinto(self, target: bytearray) -> int:
        data = self._buffer.read(len(target))
        target[: len(data)] = data
        return len(data)


def test_ffmpeg_pcm_audio_reads_an_unseekable_stream(sine_wav: Path):
    source = dev.FFmpegPCMAudio(_Unseekable(sine_wav.read_bytes()))
    try:
        assert len(source.read()) == dev.FRAME_SIZE
    finally:
        source.cleanup()


def test_ffmpeg_pcm_audio_reports_a_failed_seek(sine_wav: Path):
    with pytest.raises(dev.FFmpegSourceError):
        dev.FFmpegPCMAudio(_Unseekable(sine_wav.read_bytes()), seek=0.25)


@pytest.mark.parametrize('seek', [-1.0, float('nan'), float('inf'), 'soon'])
def test_ffmpeg_pcm_audio_rejects_a_bad_seek_position(sine_wav: Path, seek: object):
    with pytest.raises(ValueError) as info:
        dev.FFmpegPCMAudio(str(sine_wav), seek=seek)  # type: ignore[arg-type]
    assert 'non-negative' in str(info.value)


# ---------------------------------------------------------------------------- FFmpegOpusAudio


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
    # No `ffprobe`/`ffmpeg` process is started, so there is no bitrate to report.
    assert bitrate is None

    assert dev.FFmpegOpusAudio.probe_codec(str(opus_file)).startswith('opus')
    assert dev.FFmpegOpusAudio.probe_codec('/nonexistent') is None


def test_ffmpeg_opus_audio_seeks_natively(opus_file: Path):
    source = dev.FFmpegOpusAudio(str(opus_file), seek=0.1)
    try:
        assert source.is_opus() is True
        assert len(source.read()) > 0
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


def test_volume_transformer_rejects_opus_sources(opus_file: Path):
    """`self.original` is set before the check, so `__del__` -> `cleanup()` stays safe."""
    source = dev.FFmpegOpusAudio(str(opus_file))
    try:
        with pytest.raises(dev.ClientException):
            dev.PCMVolumeTransformer(source)
    finally:
        source.cleanup()


def test_volume_transformer_rejects_a_non_source():
    with pytest.raises(TypeError):
        dev.PCMVolumeTransformer(b'not a source')  # type: ignore[arg-type]


def test_volume_transformer_forwards_cleanup():
    """Cleanup is forwarded to the wrapped source."""
    cleaned: list[bool] = []

    class Source(dev.AudioSource):
        def read(self) -> bytes:
            return b'\x00' * dev.FRAME_SIZE

        def cleanup(self) -> None:
            cleaned.append(True)

    transformer = dev.PCMVolumeTransformer(Source(), 0.5)
    transformer.cleanup()
    assert cleaned == [True]


def test_volume_transformer_of_silence_stays_empty():
    transformer = dev.PCMVolumeTransformer(dev.PCMAudio(io.BytesIO(b'')), 0.5)
    assert transformer.read() == b''
