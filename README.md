# discord-ext-voice

A drop-in, `discord.py`-compatible Discord voice library whose audio and networking stack is
implemented in Rust.

```python
import discord
import discord_ext_voice as dev

voice = await channel.connect(cls=dev.VoiceClient)
voice.play(dev.FFmpegPCMAudio('song.mp3'), after=lambda error: print('finished', error))
```

The public API mirrors `discord.voice_client.VoiceClient`, `discord.player` and
`discord.opus`, so existing bot code keeps working — but underneath:

| Concern | Implementation |
| --- | --- |
| Voice websocket | `tokio` + `tokio-rustls` + `tokio-tungstenite` |
| Voice gateway payloads | `voice-model` (published on crates.io as `serenity-voice-model`) |
| RTP encryption | `xsalsa20poly1305` / `chacha20poly1305` (RustCrypto) |
| End-to-end encryption | `davey` (DAVE / MLS, RFC 9420) |
| Opus codec | `audiopus` (libopus) |
| Audio decode + resample | `ac-ffmpeg` (libavformat / libavcodec) + `libswresample` |
| Python bindings | `pyo3` + `pyo3-async-runtimes`, built with `maturin` |

## Why

* **No PyNaCl requirement.** `xsalsa20poly1305*` and `aead_xchacha20_poly1305_rtpsize` are
  implemented natively, including the incrementing nonce behaviour `discord.py` relies on.
  The test suite decrypts every mode with PyNaCl to prove byte-level compatibility.
* **No `ffmpeg` subprocess for local media.** `FFmpegPCMAudio` decodes and resamples in
  process. The CLI is only used as a fallback (URLs, `pipe=True`, unsupported options).
* **Encoding, encryption and the UDP send happen in Rust**, so playback jitter does not
  compete with Python work.
* **DAVE end-to-end encryption.** The MLS session, key packages, commit/welcome handling and
  the transition state machine are handled in Rust with `davey`.
* **Real `discord.py` close-code semantics.** 1000 / 4014 / 4015 / 4021 / 4022 are handled
  like `discord.py`, including `RESUME` and exponential-backoff reconnects.

## Requirements

Build time:

* Rust 1.85 or newer (the crate uses edition 2024)
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

At runtime the extension is **dynamically linked** against those FFmpeg and Opus libraries,
so the target machine needs the matching shared objects. Building fully self-contained wheels
would mean vendoring and statically linking FFmpeg, which is deliberately left out of scope.

## Install

```bash
python -m venv .venv && . .venv/bin/activate
pip install maturin
maturin develop --release
```

To use it inside a `discord.py` bot, install the extra:

```bash
pip install 'discord-ext-voice[discord]'
```

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
async def on_ready():
    print('ready as', client.user)

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

`discord_ext_voice.VoiceClient` subclasses `discord.voice_client.VoiceProtocol` whenever
`discord.py` is importable, so `channel.connect(cls=...)` accepts it and its exception types
are also `discord.ClientException` subclasses.

### Audio sources

| Class | Behaviour |
| --- | --- |
| `PCMAudio(stream)` | Raw 48kHz stereo `s16` read from a file-like object |
| `FFmpegPCMAudio(source)` | In-process decode + resample; CLI fallback when needed |
| `FFmpegOpusAudio(source)` | Opus pass-through when the input is Opus, otherwise libopus encoding |
| `PCMVolumeTransformer(source, volume)` | Volume scaling in Rust (no `audioop`) |

### Opus

```python
import discord_ext_voice as dev

encoder = dev.opus.Encoder(application='voip', bitrate=96, fec=True)
packet = encoder.encode(pcm_bytes)                 # 20ms of 48kHz stereo s16 -> one Opus frame
samples = dev.opus.Decoder().decode(packet)        # -> 3840 bytes of PCM
```

`dev.opus.is_loaded()` is always `True` and `load_opus()` is a no-op: Opus is linked into the
native extension.

### Standalone

Nothing in the library requires `discord.py`; only `Guild.change_voice_state` (used to move
the bot in and out of channels) is expected to exist on the channel object.

## Architecture

```
python/discord_ext_voice/          # discord.py-shaped API layer
├── voice_client.py                # VoiceClient / VoiceProtocol
├── voice_state.py                 # connection flow state + reconnect policy
├── gateway.py                     # DiscordVoiceWebSocket facade / opcodes
├── player.py                      # AudioSource implementations + AudioPlayer thread
├── opus.py                        # opus.Encoder / opus.Decoder
├── oggparse.py                    # Ogg page reader (CLI fallback only)
├── errors.py, enums.py, backoff.py, _compat.py
src/                               # the native core
├── lib.rs                         # module registration and constants
├── connection.rs                  # the VoiceConnection pyclass
├── transport.rs                   # websocket + UDP actor, heartbeat, IP discovery
├── crypto.rs                      # the four RTP encryption modes
├── dave.rs                        # DAVE/MLS session and transition state machine
├── opus.rs                        # libopus encode/decode
├── resample.rs                    # libswresample driver (48kHz stereo s16)
└── source.rs                      # FFmpeg-backed AudioSources
```

### Design notes

**TLS is layered by hand.** Rather than letting `tokio-tungstenite` own the transport, a
`tokio::net::TcpStream` is upgraded with `tokio_rustls::TlsConnector` and only then handed to
`tokio_tungstenite::client_async`. This keeps the trust store, ALPN and cipher configuration
under our control, and lets an endpoint written as `ws://host:port` use a plaintext websocket
(used by locally hosted gateways and the test suite).

**Gateway frames are normalised before parsing.** `voice_model::Event` ignores an unknown
top-level member without consuming its value, and Discord's voice gateway sends a `seq`
member alongside `op`/`d`. Every incoming frame is therefore filtered down to `op` + `d`
(while `seq` is recorded for `RESUME`) and re-parsed from text.

**Resampling talks to libswresample directly.** `ac-ffmpeg`'s `AudioResampler` wraps
`swr_convert_frame`, whose per-frame parameter check rejects the *unspecified* channel layouts
that current FFmpeg decoders emit for plain mono/stereo streams. `src/resample.rs` drives the
buffer-based `swr_convert` API instead, which has no such check. Demuxing and decoding still
go through `ac-ffmpeg`.

**The playback thread stays in Python.** `AudioPlayer` is a `threading.Thread` exactly as in
`discord.py`, so `is_playing`/`pause`/`resume`/`after` behave identically; each 20ms frame is
handed to Rust for encoding, encryption and transmission.

## Testing

```bash
cargo test          # 22 Rust unit tests
pytest -q           # 125 Python tests
```

The Python suite includes:

* a fake voice gateway that speaks the real protocol (HELLO, IDENTIFY, READY, UDP IP
  discovery, SELECT_PROTOCOL, SESSION_DESCRIPTION, heartbeats, DAVE opcodes) and records RTP;
* PyNaCl interoperability tests for **all four** encryption modes in both directions;
* DAVE tests that assert a real MLS key package (opcode 26) is produced and that transitions
  are acknowledged;
* end-to-end `VoiceClient` tests including playback, pause/resume, `after` callbacks,
  `disconnect`, resume-after-4015 and `discord.py` subclass checks.

## Limitations

* **Receiving audio is not implemented.** `VoiceConnectionState.add_socket_listener` raises
  `NotImplementedError`; this library covers everything `discord.py`'s own voice support
  covers (the sending side), not the `voice_recv` extension.
* **`-ss`/`-t` are honoured by decoding and discarding.** The in-process decoder is not
  seeked, which keeps pipes and streams working. Options other than `-ss`/`-t`/`-to`/`-f`
  fall back to the `ffmpeg` CLI.
* **Wheels are dynamically linked** against the system FFmpeg and Opus.

See [COMPATIBILITY.md](COMPATIBILITY.md) for the detailed API-by-API mapping.

## License

MIT. Parts of the Python API surface are derived from `discord.py`
(Copyright (c) 2015-present Rapptz, MIT), and the Ogg reader is ported from the same project.
