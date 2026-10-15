# discord-ext-voice

A Rust voice backend for [`discord.py`](https://github.com/Rapptz/discord.py). The public
`VoiceClient` is discord.py's own, so the API is not *compatible* with it — it **is** it.

```python
import discord
import discord_ext_voice as dev

voice = await channel.connect(cls=dev.VoiceClient)
voice.play(dev.FFmpegPCMAudio('song.mp3'), after=lambda error: print('finished', error))
```

`dev.VoiceClient` implements `discord.VoiceProtocol` — discord.py's documented extension point
for alternative voice backends — on top of the native transport, and is the only class in this
package that inherits from `discord.py`. Everything below it is this package's own:
`dev.VoiceConnectionState` (the connection flow and reconnect policy),
`dev.DiscordVoiceWebSocket` (the opcode surface the player talks to) and the audio sources
(decoding in process with libav). From `discord.py` we *use* the host framework rather than
inherit it: the client and its gateway, the `AudioPlayer` playback clock, and the exception
classes the public API raises.

Everything that must be native is native — and that is the whole voice protocol:

| Concern | Where it lives |
| --- | --- |
| Voice websocket (TCP → TLS → websocket) | Rust: `tokio` + `tokio-rustls` + `tokio-tungstenite` |
| Voice gateway payloads | Rust: `voice-model` (crates.io: `serenity-voice-model`) |
| UDP socket, IP discovery, heartbeat, RTP | Rust |
| RTP encryption (all four modes) | Rust: `xsalsa20poly1305` / `chacha20poly1305` |
| DAVE end-to-end encryption (MLS) | Rust: `davey` |
| Opus codec | Rust: `audiopus` (libopus) |
| Audio decode + resample | Rust: `ac-ffmpeg` (a fork carrying one fix, see [patches/README.md](patches/README.md)) |
| Python bindings | Rust: `pyo3` + `pyo3-async-runtimes`, built with `maturin` |
| PyNaCl, Python `davey` | **not needed** — the encryption and the MLS session are native |
| Client API (`play`, `pause`, `source`, ...) | This package, implementing `discord.VoiceProtocol` |
| Connection flow, reconnect policy | This package: `discord_ext_voice.voice_state` |
| Audio sources (decode, resample, volume) | This package: `discord_ext_voice.sources` on `ac-ffmpeg` / `libopus` |
| Playback clock (the player thread) | `discord.player.AudioPlayer` |
| Voice gateway opcodes and frame surface | This package: `discord_ext_voice.gateway` (Discord's opcode values) |
| `ffmpeg` subprocess | **not used** — no binary, no pipe, no process to reap |

## Why

* **No PyNaCl on the audio path.** `xsalsa20poly1305*` and
  `aead_xchacha20_poly1305_rtpsize` are implemented natively, including the incrementing nonce
  behaviour discord.py relies on. The test suite decrypts every mode with PyNaCl to prove
  byte-level compatibility.
* **No `ffmpeg` subprocess, ever.** The audio sources are this package's own
  (`discord_ext_voice.sources`): libav demuxes, decodes and resamples in process,
  `libswresample`/`libopus` do the rest, and `seek=` replaces `-ss`. There is no binary to
  install and no child process to reap. Getting the resampler there needed one upstream fix to
  `ac-ffmpeg` — see [patches/README.md](patches/README.md).
* **Encoding, encryption and the UDP send happen in Rust**, so playback jitter does not
  compete with Python work — the Python player thread just hands Rust 20ms of PCM.
* **DAVE end-to-end encryption** is handled entirely in Rust with `davey`: MLS key packages,
  commit/welcome processing, epoch transitions and the passthrough windows.
* **A thin Python layer.** About 1100 lines of Python (well over half of it docstrings and type
  hints). The connection flow and the websocket surface are ours; the player thread and the
  `VoiceProtocol` plumbing stay discord.py's, because they are the parts a `discord.py` bot
  already drives.

## Requirements

Build time:

* Rust 1.85 or newer (edition 2024)
* `pkg-config`
* FFmpeg development libraries: `libavformat`, `libavcodec`, `libavutil`, `libswresample`
* `libopus` development libraries
* `git`, for the first build: `ac-ffmpeg` comes from a fork on GitHub (see
  [patches/README.md](patches/README.md)) and Cargo clones it once into its own cache

Debian/Ubuntu:

```bash
sudo apt install pkg-config libavcodec-dev libavformat-dev libavutil-dev libswresample-dev libopus-dev
```

Arch:

```bash
sudo pacman -S pkgconf ffmpeg opus
```

At runtime the extension is **dynamically linked** against those FFmpeg and Opus libraries.
Building fully self-contained wheels would mean statically linking FFmpeg, which is deliberately
out of scope.

## Install

```bash
git clone <this repository> && cd discord-ext-voice
python -m venv .venv && . .venv/bin/activate
pip install maturin
maturin develop --release
```

`ac-ffmpeg` is used through a small fork that carries one fix (see
[patches/README.md](patches/README.md)). Cargo fetches it automatically — no submodule, no
vendored tree — and `Cargo.lock` pins the exact commit, so the first build needs `git` and
network access and every later build is offline.

Only plain `discord.py` is required. `discord.py[voice]` (PyNaCl + the Python `davey`
bindings) is **not** needed: `VoiceClient` implements `VoiceProtocol` directly, so discord.py's
capability checks and its PyNaCl/`davey` code paths are never entered.

> **Note:** discord.py logs ``PyNaCl is not installed, voice will NOT be supported`` (and the
> `davey` equivalent) once at login, because it inspects *its own* backend. Voice through
> `discord_ext_voice.VoiceClient` works regardless. If you also want discord.py's built-in
> voice client, install `discord.py[voice]`; to silence the message instead, raise the log
> level for `discord.client`.

## Usage

### Inside a `discord.py` bot

```python
import discord
import discord_ext_voice as dev

intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_message(message):
    if message.content == '!join':
        voice = await message.author.voice.channel.connect(cls=dev.VoiceClient)
        voice.play(
            dev.FFmpegPCMAudio('song.mp3'),
            after=lambda error: print('finished', error),
        )
    elif message.content == '!leave':
        await message.guild.voice_client.disconnect()
```

`dev.VoiceClient` provides the same members discord.py's client does for sending audio:
`play()`, `is_playing()`, `is_paused()`, `pause()`, `resume()`, `stop()`, `source`, `move_to()`,
`disconnect()`, `is_connected()`, `wait_until_connected()`, `latency`, `average_latency`,
`voice_privacy_code`, the `after` callback, and so on. `channel.connect(cls=dev.VoiceClient)`
accepts it because it implements `discord.VoiceProtocol`.

### Audio sources

Every source is implemented in this package ([`discord_ext_voice.sources`](python/discord_ext_voice/sources.py))
and decodes in process with libav — the `ffmpeg` binary is never started:

| Class | Behaviour |
| --- | --- |
| `PCMAudio(stream)` | Raw 16-bit 48kHz stereo PCM from a file object |
| `FFmpegPCMAudio(source, *, seek=None)` | libav decodes and resamples to 48kHz stereo `s16` |
| `FFmpegOpusAudio(source, *, seek=None)` | Opus packets passed through, otherwise PCM + libopus |
| `PCMVolumeTransformer(source, volume)` | Volume, scaled by the native core (no `audioop`) |
| `AudioPlayer` | `discord.player.AudioPlayer`, reused as the playback clock |

```python
dev.FFmpegPCMAudio('song.mp3')                 # decode in process with libav
dev.FFmpegPCMAudio('song.mp3', seek=30.0)      # start 30s in (what `-ss 30` used to do)
dev.FFmpegPCMAudio(open('song.mp3', 'rb'))     # a seekable file object works too
dev.FFmpegOpusAudio('voice.ogg')               # Opus packets passed through untouched
dev.PCMVolumeTransformer(dev.FFmpegPCMAudio('song.mp3'), 0.5)
```

`dev.AudioSource` is this package's own base class, and nothing here inherits from discord.py: a
source only has to provide `read`/`is_opus`/`cleanup`, which is also what makes a user's own
`discord.AudioSource` (or duck-typed equivalent) playable — `dev.is_audio_source(x)` answers that
question. discord.py's FFmpeg-CLI arguments —
`before_options`, `options`, `executable`, `pipe`, `stderr`, `force_cli` — do not exist here and
raise `TypeError` naming the argument.

### Opus

```python
import discord_ext_voice as dev

encoder = dev.opus.Encoder(application='voip', bitrate=96, fec=True)
packet = encoder.encode(pcm_bytes)                  # 20ms of 48kHz stereo s16 -> one frame
samples = dev.opus.Decoder().decode(packet)         # -> 3840 bytes of PCM
```

`dev.opus.Encoder`/`Decoder` are native, so unlike `discord.opus.Encoder` they need no
`load_opus()` call. The constants (`OPUS_SILENCE`, `APPLICATION_AUDIO`, ...) come from
`discord.opus`.

### The native handle

```python
voice = await channel.connect(cls=dev.VoiceClient)
voice.native.is_connected()          # transport state
voice.native.latency                 # voice websocket latency
voice.native.voice_privacy_code      # DAVE displayable code
voice.native.decrypt_rtp(header, payload)
voice.native.dave_protocol_version   # 0 when DAVE is not negotiated
voice.native.mode                    # negotiated RTP encryption mode
```

## Architecture

```
python/discord_ext_voice/        # thin discord.py extension
├── voice_client.py              # VoiceClient: create_connection_state, play, send_audio_packet
├── voice_state.py               # VoiceConnectionState: connect/reconnect flow (ours)
├── gateway.py                   # DiscordVoiceWebSocket: opcodes + speak/close on the native socket
├── sources.py                   # our AudioSource / PCMAudio / FFmpeg*Audio / volume
├── opus.py                      # native Encoder/Decoder + discord.opus constants
└── errors.py                    # native errors + discord.py's, and FFmpegSourceError
src/                             # the core
├── lib.rs                       # module registration and constants
├── connection.rs                # the VoiceConnection pyclass
├── transport.rs                 # websocket + UDP actor, heartbeat, IP discovery
├── crypto.rs                    # the four RTP encryption modes
├── dave.rs                      # DAVE/MLS session and transition state machine
├── opus.rs                      # libopus encode/decode
└── source.rs                    # FFmpeg-backed audio sources
```

### Design notes

**TLS is layered by hand.** A `tokio::net::TcpStream` is upgraded with
`tokio_rustls::TlsConnector` and only then handed to `tokio_tungstenite::client_async`, rather
than using tokio-tungstenite's own connectors. That keeps the trust store and cipher
configuration under our control and lets an endpoint written as `ws://host:port` use a
plaintext websocket (used by locally hosted gateways and the test suite).

**Gateway frames are normalised before parsing.** `voice_model::Event` ignores an unknown
top-level member without consuming its value, and Discord's voice gateway sends a `seq` member
alongside `op`/`d`. Every incoming frame is filtered down to `op` + `d` (recording `seq` for
`RESUME`) and re-parsed from text.

**`ac-ffmpeg` is patched to a fork that carries one resampler fix.** Its `AudioResampler`
hands frames straight to `swr_convert_frame`, which rejects the *unspecified* channel layouts
that current FFmpeg
decoders emit for plain PCM streams (`AVERROR_INPUT_CHANGED`, "Input changed"), and its public
API offers no configuration that avoids it. The build therefore patches `ac-ffmpeg` to a fork
of upstream `09f1421` (release 0.19.0) with a small fix that normalises the layout of a shallow
frame copy before converting. The change is also exported as
`patches/0001-resampler-accept-unspecified-channel-layout.patch`, which is what gets rebased
onto new releases and sent upstream; dropping the fork once a fixed release exists is a three
line change, see [patches/README.md](patches/README.md).

**The audio player thread stays in Python**, because it is discord.py's. Each 20ms frame goes
from that thread into Rust for encoding, encryption and transmission, so the timing model is
identical to discord.py's while the work is native.

## Testing

```bash
cargo test          # 16 Rust unit tests
pytest -q           # 128 Python tests (the interop module skips itself without PyNaCl)
```

The Python suite includes:

* a fake voice gateway that speaks the real protocol (HELLO, IDENTIFY, READY, UDP IP
  discovery, SELECT_PROTOCOL, SESSION_DESCRIPTION, heartbeats, DAVE opcodes) and records RTP;
* PyNaCl interoperability tests for **all four** encryption modes, in both directions
  (`pynacl` is a test dependency; the tests skip if it is missing);
* a test asserting `VoiceClient` still constructs when `discord.voice_client.has_nacl` and
  `has_dave` are false;
* DAVE tests asserting a real MLS key package (opcode 26) is produced and that transitions are
  acknowledged;
* end-to-end `VoiceClient` tests covering playback, pause/resume, `after` callbacks,
  `disconnect`, resume-after-4015 and the `discord.py` layering checks (only `VoiceClient`
  inherits from it).

## Limitations

* **Receiving audio is not implemented.** `VoiceConnectionState.add_socket_listener` raises
  `NotImplementedError`, matching the scope of discord.py's own voice support (the sending
  side); the `voice_recv` use case is not covered.
* **`before_options`/`options` are gone** (they raise `TypeError`): FFmpeg CLI grammar cannot
  be mapped onto the in-process decoder. `seek=` covers `-ss`, and everything else belongs in
  the media file.
* **Network URLs are not supported.** Fetch the URL with your own HTTP client and hand the
  response (or a temporary file) to `FFmpegPCMAudio`; decoding a URL directly would mean
  bundling an HTTP client and pulling network I/O into the player thread.
* **A missing local file fails at construction**, not on the first `read()`, so a typo is
  reported immediately.
* **Wheels are dynamically linked** against the system FFmpeg and Opus.

See [COMPATIBILITY.md](COMPATIBILITY.md) for the exact list of inherited and overridden
members, and the handful of intentional behaviour differences.

## License

MIT. The Python API surface this package extends belongs to
[`discord.py`](https://github.com/Rapptz/discord.py) (Copyright (c) 2015-present Rapptz, MIT).
