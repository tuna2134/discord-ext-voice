# discord-ext-voice

A Rust voice backend for [`discord.py`](https://github.com/Rapptz/discord.py). The public
`VoiceClient` is discord.py's own, so the API is not *compatible* with it — it **is** it.

```python
import discord
import discord_ext_voice as dev

voice = await channel.connect(cls=dev.VoiceClient)
voice.play(dev.FFmpegPCMAudio('song.mp3'), after=lambda error: print('finished', error))
```

`dev.VoiceClient` subclasses `discord.VoiceClient`, `dev.VoiceConnectionState` subclasses
`discord.voice_state.VoiceConnectionState`, `dev.DiscordVoiceWebSocket` subclasses
`discord.gateway.DiscordVoiceWebSocket`, and the audio player, audio sources and volume
transformer are discord.py's own classes re-exported unchanged. Only the parts that must be
native are overridden — and those parts are the whole voice protocol:

| Concern | Where it lives |
| --- | --- |
| Voice websocket (TCP → TLS → websocket) | Rust: `tokio` + `tokio-rustls` + `tokio-tungstenite` |
| Voice gateway payloads | Rust: `voice-model` (crates.io: `serenity-voice-model`) |
| UDP socket, IP discovery, heartbeat, RTP | Rust |
| RTP encryption (all four modes) | Rust: `xsalsa20poly1305` / `chacha20poly1305` |
| DAVE end-to-end encryption (MLS) | Rust: `davey` |
| Opus codec | Rust: `audiopus` (libopus) |
| Audio decode + resample | Rust: `ac-ffmpeg` + `libswresample` |
| Python bindings | Rust: `pyo3` + `pyo3-async-runtimes`, built with `maturin` |
| Connection flow state machine, reconnect policy | `discord.voice_state.VoiceConnectionState` |
| Audio player thread, `PCMAudio`, `PCMVolumeTransformer` | `discord.player` |
| Gateway opcodes and outgoing frame builders | `discord.gateway.DiscordVoiceWebSocket` |
| `ffmpeg` subprocess fallback | `discord.player.FFmpegPCMAudio` |

## Why

* **No PyNaCl on the audio path.** `xsalsa20poly1305*` and
  `aead_xchacha20_poly1305_rtpsize` are implemented natively, including the incrementing nonce
  behaviour discord.py relies on. The test suite decrypts every mode with PyNaCl to prove
  byte-level compatibility.
* **No `ffmpeg` subprocess for local media.** `FFmpegPCMAudio` decodes and resamples in
  process with libav; the binary is only used for URLs, pipes and explicit CLI options.
* **Encoding, encryption and the UDP send happen in Rust**, so playback jitter does not
  compete with Python work — the Python player thread just hands Rust 20ms of PCM.
* **DAVE end-to-end encryption** is handled entirely in Rust with `davey`: MLS key packages,
  commit/welcome processing, epoch transitions and the passthrough windows.
* **No duplicated Python.** Roughly 860 lines of Python (mostly docstrings) instead of the
  ~2400 it takes to re-implement discord.py's player, state machine and gateway handling.

## Requirements

Build time:

* Rust 1.85 or newer (edition 2024)
* `pkg-config`
* FFmpeg development libraries: `libavformat`, `libavcodec`, `libavutil`, `libswresample`
* `libopus` development libraries

Debian/Ubuntu:

```bash
sudo apt install pkg-config libavcodec-dev libavformat-dev libavutil-dev libswresample-dev libopus-dev
```

Arch:

```bash
sudo pacman -S pkgconf ffmpeg opus
```

At runtime the extension is **dynamically linked** against those FFmpeg and Opus libraries.
Building fully self-contained wheels would mean vendoring and statically linking FFmpeg, which
is deliberately out of scope.

## Install

```bash
python -m venv .venv && . .venv/bin/activate
pip install maturin
maturin develop --release
```

`discord.py[voice]` is a runtime dependency: `discord.VoiceClient.__init__` refuses to build
without PyNaCl and the Python `davey` bindings, even though this library's crypto and MLS
session are native.

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

Because `dev.VoiceClient` *is* a `discord.VoiceClient`, everything works the same way it does
with discord.py: `is_playing()`, `pause()`, `resume()`, `stop()`, `source`, `move_to()`,
`latency`, `average_latency`, `voice_privacy_code`, the `after` callback, and so on.

### Audio sources

| Class | Behaviour |
| --- | --- |
| `PCMAudio(stream)` | `discord.player.PCMAudio`, reused unchanged |
| `FFmpegPCMAudio(source)` | Subclass of discord.py's, decoding in process with libav when it can |
| `FFmpegOpusAudio(source)` | Subclass of discord.py's: Opus pass-through, otherwise libopus |
| `PCMVolumeTransformer(source, volume)` | `discord.player.PCMVolumeTransformer`, reused unchanged |
| `AudioPlayer` | `discord.player.AudioPlayer`, reused unchanged |

`FFmpegPCMAudio` takes discord.py's arguments plus `force_cli: bool = False`:

```python
dev.FFmpegPCMAudio('song.mp3')                        # in-process libav decode
dev.FFmpegPCMAudio('stream.mp3', before_options='-ss 30')  # ffmpeg CLI (option grammar)
dev.FFmpegPCMAudio('https://example.com/a.mp3')       # ffmpeg CLI (network protocol)
dev.FFmpegPCMAudio('song.mp3', force_cli=True)        # ffmpeg CLI, explicitly
```

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
├── voice_state.py               # VoiceConnectionState: the native integration points
├── gateway.py                   # DiscordVoiceWebSocket: native socket, inherited opcodes
├── player.py                    # native-first FFmpegPCMAudio / FFmpegOpusAudio
├── opus.py                      # native Encoder/Decoder + discord.opus constants
└── errors.py                    # native errors + discord.py's, and FFmpegSourceError
src/                             # the core
├── lib.rs                       # module registration and constants
├── connection.rs                # the VoiceConnection pyclass
├── transport.rs                 # websocket + UDP actor, heartbeat, IP discovery
├── crypto.rs                    # the four RTP encryption modes
├── dave.rs                      # DAVE/MLS session and transition state machine
├── opus.rs                      # libopus encode/decode
├── resample.rs                  # libswresample driver (48kHz stereo s16)
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

**Resampling talks to libswresample directly.** `ac-ffmpeg`'s `AudioResampler` wraps
`swr_convert_frame`, whose per-frame parameter check rejects the *unspecified* channel layouts
that current FFmpeg decoders emit for plain mono/stereo streams. `src/resample.rs` drives the
buffer-based `swr_convert` API instead, which has no such check. Demuxing and decoding still
go through `ac-ffmpeg`.

**The audio player thread stays in Python**, because it is discord.py's. Each 20ms frame goes
from that thread into Rust for encoding, encryption and transmission, so the timing model is
identical to discord.py's while the work is native.

## Testing

```bash
cargo test          # 22 Rust unit tests
pytest -q           # 128 Python tests
```

The Python suite includes:

* a fake voice gateway that speaks the real protocol (HELLO, IDENTIFY, READY, UDP IP
  discovery, SELECT_PROTOCOL, SESSION_DESCRIPTION, heartbeats, DAVE opcodes) and records RTP;
* PyNaCl interoperability tests for **all four** encryption modes, in both directions;
* DAVE tests asserting a real MLS key package (opcode 26) is produced and that transitions are
  acknowledged;
* end-to-end `VoiceClient` tests covering playback, pause/resume, `after` callbacks,
  `disconnect`, resume-after-4015 and `discord.py` subclass checks.

## Limitations

* **Receiving audio is not implemented.** `VoiceConnectionState.add_socket_listener` raises
  `NotImplementedError`, matching the scope of discord.py's own voice support (the sending
  side); the `voice_recv` use case is not covered.
* **`before_options`/`options` always use the `ffmpeg` binary.** They are FFmpeg CLI grammar,
  so they cannot be mapped onto the in-process decoder; the binary is invoked exactly as
  discord.py would.
* **A missing local file fails at construction**, not on the first `read()`, so a typo is
  reported immediately.
* **Wheels are dynamically linked** against the system FFmpeg and Opus.

See [COMPATIBILITY.md](COMPATIBILITY.md) for the exact list of inherited and overridden
members, and the handful of intentional behaviour differences.

## License

MIT. The Python API surface this package extends belongs to
[`discord.py`](https://github.com/Rapptz/discord.py) (Copyright (c) 2015-present Rapptz, MIT).
