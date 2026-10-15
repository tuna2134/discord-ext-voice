# The `ac-ffmpeg` fork

This project needs **one fix** in [`ac-ffmpeg`](https://github.com/angelcam/rust-ac-ffmpeg),
which is not in the released 0.19.0. The fix lives on a fork, and `Cargo.toml` points Cargo at it:

```toml
[patch.crates-io]
ac-ffmpeg = { git = "https://github.com/tuna2134/rust-ac-ffmpeg", branch = "master" }
```

The fork is upstream `09f1421` ("Release v0.19.0") plus one commit — the change that
`patches/0001-resampler-accept-unspecified-channel-layout.patch` contains. `Cargo.lock` records
the exact commit, so builds stay reproducible until somebody runs `cargo update -p ac-ffmpeg`.
Nothing has to be vendored or checked out by hand: `cargo` fetches the fork on the first build,
which needs `git` and network access, and the crate built from it is byte-for-byte the one this
project was tested against.

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

## Reproducing the fork

```bash
git clone https://github.com/tuna2134/rust-ac-ffmpeg
cd rust-ac-ffmpeg
git checkout 09f1421                       # upstream "Release v0.19.0"
git am /path/to/patches/0001-resampler-accept-unspecified-channel-layout.patch
git push origin HEAD:master
```

`patches/0001-…` is a `git format-patch` export, so `git am` recreates the commit with its
message intact. Applying it to the upstream tree is also the first step of getting the fix
accepted upstream — the same file can be sent as a pull request to
<https://github.com/angelcam/rust-ac-ffmpeg>.

## Updating to a newer upstream release

Rebase the fork onto the new revision, then refresh the patch:

```bash
git -C rust-ac-ffmpeg rebase <new revision>
git -C rust-ac-ffmpeg format-patch -1 --stdout > patches/0001-resampler-accept-unspecified-channel-layout.patch
git -C rust-ac-ffmpeg push --force-with-lease origin HEAD:master
```

Then bump `ac-ffmpeg` in `[dependencies]` (the fork tracks upstream's version number) and run
`cargo update -p ac-ffmpeg`. If upstream changes the surrounding code the rebase conflicts
rather than quietly dropping the fix, and the test suite fails loudly if the fix ever goes
missing: `tests/test_sources.py` decodes a plain WAV file, which is exactly the case that
`AVERROR_INPUT_CHANGED` breaks.

## Removing

Once a released `ac-ffmpeg` contains the fix:

1. bump the `ac-ffmpeg` requirement in `Cargo.toml` and drop the `[patch.crates-io]` section,
2. delete `patches/`.

No source changes are needed elsewhere: the fix is entirely inside the dependency.

## Licence

`ac-ffmpeg` is MIT licensed, and the fork adds no new licensing terms.
