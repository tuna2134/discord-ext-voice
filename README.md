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
`dev.VoiceConnectionState` (the connection flow, whose decisions are native),
`dev.DiscordVoiceWebSocket` (the opcode surface the player talks to), `dev.AudioPlayer` (the
playback clock) and the audio sources (decoding in process with `symphonia`). From `discord.py` we
*use* the host framework rather than inherit it: the client and its gateway, and the exception
classes the public API raises.

Everything that must be native is native — and that is the whole voice protocol:

| Concern | Where it lives |
| --- | --- |
| Voice websocket (TCP → TLS → websocket) | Rust: `tokio` + `tokio-rustls` + `tokio-tungstenite` |
| Voice gateway payloads | Rust: `voice-model` (crates.io: `serenity-voice-model`) |
| UDP socket, IP discovery, heartbeat, RTP | Rust |
| RTP encryption (all four modes) | Rust: `xsalsa20poly1305` / `chacha20poly1305` |
| DAVE end-to-end encryption (MLS) | Rust: `davey` |
| Opus codec | Rust: `oprs` (libopus 1.6.1, ported to pure Rust) |
| Audio decode + resample | Rust: `symphonia` (containers, codecs) + `rubato` (sample rates) |
| Python bindings | Rust: `pyo3` + `pyo3-async-runtimes`, built with `maturin` |
| PyNaCl, Python `davey` | **not needed** — the encryption and the MLS session are native |
| Client API (`play`, `pause`, `source`, ...) | This package, implementing `discord.VoiceProtocol` |
| Connection flow, reconnect policy | Rust: `_native.ConnectionFlow` (phases, close codes, retries, backoff), driven by `discord_ext_voice.voice_state` |
| Audio sources (decode, resample, volume) | This package: `discord_ext_voice.sources` on `symphonia` / `rubato` / `oprs` |
| `http(s)` inputs | `reqwest` (rustls, no OpenSSL), streamed straight into the decoder |
| Playback clock (the player thread) | Rust: `discord_ext_voice.player` (20ms pacing, silence, speaking) |
| Voice gateway opcodes and frame surface | This package: `discord_ext_voice.gateway` (Discord's opcode values) |
| `ffmpeg` subprocess | **not used** — no binary, no pipe, no process to reap |

## Why

* **No PyNaCl on the audio path.** `xsalsa20poly1305*` and
  `aead_xchacha20_poly1305_rtpsize` are implemented natively, including the incrementing nonce
  behaviour discord.py relies on. The test suite decrypts every mode with PyNaCl to prove
  byte-level compatibility.
* **No FFmpeg, in any form.** The audio sources are this package's own
  (`discord_ext_voice.sources`): `symphonia` demuxes and decodes in process, `rubato` converts
  the sample rate, `oprs` (libopus 1.6.1, ported to pure Rust) handles Opus — which symphonia has
  no decoder for — and `seek=` replaces `-ss`. No `ffmpeg` binary, no C library, no child process:
  the extension links nothing but libc.
* **Encoding, encryption, the UDP send and the playback clock itself happen in Rust**, so
  playback jitter does not compete with Python work — the player reads one frame from the source
  and paces it against a native 20ms clock.
* **DAVE end-to-end encryption** is handled entirely in Rust with `davey`: MLS key packages,
  commit/welcome processing, epoch transitions and the passthrough windows.
* **A thin Python layer.** The Python side holds the awaits and the discord.py-compatible
  surface, not the decisions: the connection flow's state machine, close-code policy, retry
  spacing and reconnect backoff are all in `_native.ConnectionFlow`, and Python dispatches on the
  action it returns. The one thing discord.py provides is what a bot already drives: the client,
  the main gateway, and the `VoiceProtocol` extension point `VoiceClient` implements.

## Requirements

Runtime:

* Python 3.11 or newer (no `abi3`: a wheel per CPython version)

Build time:

* Rust 1.85 or newer (edition 2024)
* `git`, for the first build: `oprs` is not on crates.io yet, so Cargo fetches it from
  [its repository](https://github.com/tuna2134/oprs) once and `Cargo.lock` pins the commit

Nothing else — no `pkg-config`, no C library: the demuxers, decoders, the resampler and the Opus
codec are all pure Rust (`symphonia`, `rubato`, `oprs`).

A wheel from the package index is **self-contained**: the extension links nothing but libc, so
there is nothing to bundle and nothing to install. A wheel is around 5MB; a build from source is
the same, except that it fetches `oprs` from its repository once.

## Install

```bash
git clone <this repository> && cd discord-ext-voice
python -m venv .venv && . .venv/bin/activate
pip install maturin
maturin develop --release
```

### From the package index

Every release is published to a PEP 503 index on GitHub Pages, with the wheels and the sdist
attached to the matching GitHub release:

```bash
pip install --extra-index-url https://tuna2134.dev/discord-ext-voice/simple/ discord-ext-voice
```

`https://tuna2134.github.io/discord-ext-voice/simple/` is the same index (it redirects to the
custom domain). The distributions themselves are release assets rather than Pages content — every
deployment replaces the whole site, and the index has to keep every published version — and the
index lists each file with its SHA-256, which pip verifies.

In a `requirements.txt`:

```
--extra-index-url https://tuna2134.dev/discord-ext-voice/simple/
discord-ext-voice
```

The index is rebuilt by [`.github/workflows/pages.yml`](.github/workflows/pages.yml), which runs on
a tag push (the tag names the release) or on demand from the Actions tab. It carries the links of
the already-published index over, so a new version never drops the old ones.

The pages themselves are rendered by [`scripts/pages_index.py`](scripts/pages_index.py) from the
Jinja2 templates in [`scripts/templates/`](scripts/templates), with autoescaping on: the values
come from the built archives and from the release API.

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
and decodes in process with `symphonia` — no FFmpeg, no binary:

| Class | Behaviour |
| --- | --- |
| `PCMAudio(stream)` | Raw 16-bit 48kHz stereo PCM from a file object |
| `FFmpegPCMAudio(source, *, seek=None, headers=None)` | `symphonia` + `rubato` decode and resample to 48kHz stereo `s16` |
| `FFmpegOpusAudio(source, *, seek=None, headers=None)` | Opus packets passed through, otherwise PCM + `oprs` |
| `PCMVolumeTransformer(source, volume)` | Volume, scaled by the native core (no `audioop`) |
| `AudioPlayer(source, client, after=...)` | This package: a thin wrapper over the native playback thread |

```python
dev.FFmpegPCMAudio('song.mp3')                 # decode in process with symphonia
dev.FFmpegPCMAudio('song.mp3', seek=30.0)      # start 30s in (what `-ss 30` used to do)
dev.FFmpegPCMAudio(open('song.mp3', 'rb'))     # a seekable file object works too
dev.FFmpegPCMAudio('https://example.com/a.mp3')  # streamed with reqwest, no download step
dev.FFmpegOpusAudio('voice.ogg')               # Opus packets passed through untouched
dev.PCMVolumeTransformer(dev.FFmpegPCMAudio('song.mp3'), 0.5)

# A URL an extractor produced: send the headers it handed out with it.
info = ytdl.extract_info(url, download=False)
dev.FFmpegPCMAudio(info['url'], headers=info['http_headers'])
```

An `http(s)` URL is fetched by `reqwest` (rustls, so no OpenSSL is needed) and handed to the
demuxer as a stream: playback starts before the download finishes, and seeking uses `Range` requests when the
server supports them (otherwise the request is restarted and the offset skipped). A connection
that dies or is cut short mid-stream — a throttling CDN closing an idle one, which is routine for
a long stream — is picked up again from the byte it reached instead of looking like the end of the
file, which is how the demuxer would read it.

`headers=` sends extra HTTP headers with every request the reader makes (the first fetch and any
`Range` a seek issues). Extractors hand those out with the URLs they produce — `yt-dlp`'s
`http_headers`, for instance — and many CDNs reject a URL fetched without them, because the URL is
bound to the client that asked for it. A `User-Agent` there replaces this package's own; passing
headers for a local file or a file object is a `TypeError`, since nothing would send them.

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

### Examples

Two runnable bots live in [`examples/`](examples):

| Example | What it shows | Extra dependency |
| --- | --- | --- |
| [`bot.py`](examples/bot.py) | `!join` / `!play` / `!status` / `!leave` on a local file | none |
| [`ytdl.py`](examples/ytdl.py) | `!yt` / `!stream` / `!play` / `!volume` / `!stop`, with `yt-dlp` resolving the URL | `pip install yt-dlp` |

```bash
DISCORD_TOKEN=... python examples/bot.py song.mp3
DISCORD_TOKEN=... python examples/ytdl.py
```

`ytdl.py` is discord.py's own example, adapted: it connects with `cls=dev.VoiceClient` (so this
backend is the one that speaks voice), passes the media to `dev.FFmpegPCMAudio` without the
FFmpeg CLI arguments discord.py's copy uses, hands yt-dlp's `http_headers` to the reader on the
`!stream` path (a CDN URL is usually bound to the client that requested it), and patches
`yt_dlp.utils.bug_reports_message` in a way that accepts the arguments current yt-dlp passes.
`tests/test_examples.py` imports both and checks that patch, so an upstream signature change is
caught here, and `tests/test_stream_live.py` plays a real extractor URL end to end when
`DISCORD_EXT_VOICE_LIVE_URL` is set.

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
├── opus.rs                      # Opus encode/decode (oprs)
└── source.rs                    # audio sources: symphonia decoding, files, objects, http(s)
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

**Decoding is pure Rust.** `symphonia` reads the container and decodes the codec, `rubato`
converts anything that is not already 48kHz stereo, and Opus packets — the one codec symphonia
has no decoder for — go to `oprs`, the same pure-Rust libopus port the encoder uses. A
44.1kHz MP3 is therefore resampled in process, and a WebM/Opus stream (what a YouTube URL
gives) is demuxed by symphonia and decoded by libopus.

**The connection flow's decisions are native.** `discord_ext_voice.voice_state` awaits what only
Python can await (discord.py's `Guild.change_voice_state`, the native handshake, the websocket
close) and asks `_native.ConnectionFlow` what to do: which gateway event moves the phase where,
what a close code means (1000/4014/4015/4021/4022/other), how long a retry waits and how long a
reconnect backs off. The phase itself is the transport's, so there is one state rather than two.

**The playback clock is native.** `discord_ext_voice.AudioPlayer` is a thin wrapper over a Rust
thread that paces each 20ms frame, feeds silence while paused, announces the `SPEAKING` state and
waits for a reconnection. It keeps `discord.player.AudioPlayer`'s semantics — `DELAY = 0.02`, the
`after(error)` callback, and a clock that restarts on `resume` — without importing
`discord.player`, whose `audioop` dependency would otherwise be pulled in.

## Testing

```bash
cargo test --lib    # 41 Rust unit tests
pytest -q           # 183 Python tests, 4 skipped (6 crypto interop tests need PyNaCl; 3 libopus
                    # oracle tests need DISCORD_EXT_VOICE_OPUS_ORACLE=1; 1 live URL test needs a URL)
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
  inherits from it);
* `tests/test_examples.py` imports both examples and checks the yt-dlp patch they carry, so an
  upstream signature change is caught here rather than in a bot;
* `tests/test_stream_live.py` plays a real extractor URL (with its `http_headers`) through the
  whole stack — opt-in, via `DISCORD_EXT_VOICE_LIVE_URL`, because it needs the network;
* `tests/test_opus.py` compares the encoder with the system libopus, packet for packet and PCM for
  PCM — opt-in, via `DISCORD_EXT_VOICE_OPUS_ORACLE=1`, because it only means anything when that
  libopus is the version `oprs` ports (1.6.1);
* `tests/test_flow.py` for the native connection flow: the phase round-trip, the waiting
  primitives (including the timeout contract), and that every action Rust can return has a name
  in Python. The transition table itself is unit-tested in `src/flow.rs`.

## Limitations

* **Receiving audio is not implemented.** `VoiceConnectionState.add_socket_listener` raises
  `NotImplementedError`, matching the scope of discord.py's own voice support (the sending
  side); the `voice_recv` use case is not covered.
* **`before_options`/`options` are gone** (they raise `TypeError`): FFmpeg CLI grammar cannot
  be mapped onto the in-process decoder. `seek=` covers `-ss`, and everything else belongs in
  the media file.
* **`http(s)` URLs need a server that behaves.** Seeking backwards on a server without `Range`
  support restarts the request and skips forward, which is slower. A connection that is *dropped*
  is resumed from the byte it reached (three times in a row, then the error is reported), but a
  server that stops sending without closing anything blocks the reader thread until the OS gives
  up: there is a 10s connect timeout, and no read timeout while streaming. Redirects are
  followed.
* **A missing local file fails at construction**, not on the first `read()`, so a typo is
  reported immediately.
* **Wheels are dynamically linked** against the system FFmpeg and Opus.

See [COMPATIBILITY.md](COMPATIBILITY.md) for the exact list of inherited and overridden
members, and the handful of intentional behaviour differences.

## License

MIT. The Python API surface this package extends belongs to
[`discord.py`](https://github.com/Rapptz/discord.py) (Copyright (c) 2015-present Rapptz, MIT).
