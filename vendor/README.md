# Vendored `ac-ffmpeg`

This directory is a copy of <https://github.com/angelcam/rust-ac-ffmpeg> at commit
`09f1421` ("Release v0.19.0", the released 0.19.0) plus **one fix** to its audio resampler.

`Cargo.toml` points at it through:

```toml
[patch.crates-io]
ac-ffmpeg = { path = "vendor/ac-ffmpeg/ac-ffmpeg" }
```

## Why

`ac-ffmpeg`'s `AudioResampler` hands the caller's frame straight to `swr_convert_frame()`.
FFmpeg decoders routinely leave the channel layout of plain PCM streams *unspecified*
("2 channels, order unspecified"), while `swr_alloc_set_opts2()` resolves such a layout to the
default one for that channel count. `swr_convert_frame()` therefore sees a change of input
parameters, tries to reconfigure through `swr_config_frame()` and, because that fails for an
unspecified layout, returns `AVERROR_INPUT_CHANGED` ("Input changed"). Resampling a plain WAV
file fails with:

```
FFmpegError: FFmpeg resampler error: Input changed
```

`ac-ffmpeg`'s public API cannot work around this, because `AudioResampler::try_push` requires the
frame's layout to equal the configured source layout (`frame.channel_layout() !=
self.source_channel_layout`), leaving only two configurations and both fail:

| source layout passed to the builder | result |
| --- | --- |
| the frame's own (unspecified) layout | `swr_convert_frame` returns `AVERROR_INPUT_CHANGED` |
| `get_channel_layout("stereo")` | `try_push` rejects the frame with `invalid frame, channel layout does not match` |

## The fix

`ac-ffmpeg/src/codec/audio/resampler.c` normalises a shallow copy of the frame to
`av_channel_layout_default()` for its channel count before converting (the sample data is
reference counted, so nothing is copied). Frames with a usable layout are passed through
unchanged, and if the copy cannot be allocated the original frame is used as before.

Also exported as `patches/0001-resampler-accept-unspecified-channel-layout.patch`, which applies
cleanly to upstream `09f1421`:

```bash
git clone https://github.com/angelcam/rust-ac-ffmpeg
cd rust-ac-ffmpeg
git am /path/to/patches/0001-resampler-accept-unspecified-channel-layout.patch
```

## Updating

`scripts/vendor-ac-ffmpeg.sh` re-creates this directory from a fresh upstream checkout plus the
patch, so it can be regenerated or moved to a newer upstream revision.

## Removing

Once a released `ac-ffmpeg` contains the fix:

1. bump the `ac-ffmpeg` requirement in `Cargo.toml` and drop the `[patch.crates-io]` section,
2. delete `vendor/` and `patches/`,
3. delete `scripts/vendor-ac-ffmpeg.sh`.

No source changes are needed elsewhere: the fix is entirely inside the dependency.

## Licence

`ac-ffmpeg` is MIT licensed; `LICENSE` is kept unmodified in this directory.
