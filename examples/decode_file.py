"""Inspect and decode audio without a Discord connection.

Shows the pieces of the library that do not need a gateway: probing, the audio sources and the
volume transformer.

    python examples/decode_file.py song.mp3
"""

from __future__ import annotations

import sys

import discord_ext_voice as dev


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if path is None:
        raise SystemExit('usage: python examples/decode_file.py <media file>')

    codec, sample_rate, channels, duration = dev.probe(path)
    print(f'{path}: codec={codec} rate={sample_rate} channels={channels} duration={duration!r}s')

    source = dev.FFmpegPCMAudio(path)
    try:
        frames = 0
        total = 0
        while True:
            data = source.read()
            if not data:
                break
            frames += 1
            total += len(data)
        print(f'decoded {frames} frames ({total} bytes of 48kHz stereo s16)')
    finally:
        source.cleanup()

    if codec is not None and codec.startswith('opus'):
        opus_source = dev.FFmpegOpusAudio(path)
        try:
            packets = 0
            while opus_source.read() and packets < 5:
                packets += 1
            print(f'opus pass-through: read {packets} raw packets without re-encoding')
        finally:
            opus_source.cleanup()
    else:
        # Not an Opus stream, so the pass-through source falls back to PCM + libopus.
        transcoded = dev.FFmpegOpusAudio(path)
        try:
            print(f'opus fallback: is_opus={transcoded.is_opus()}')
        finally:
            transcoded.cleanup()


if __name__ == '__main__':
    main()
