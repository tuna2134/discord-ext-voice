# discord-ext-voice

A Rust voice backend for [`discord.py`](https://github.com/Rapptz/discord.py). The public
`VoiceClient` exposes discord.py's voice-client API — implemented on discord.py's own
`VoiceProtocol` extension point, so a bot changes the class it connects with and nothing else.

```python
import discord
import discord_ext_voice as dev

voice = await channel.connect(cls=dev.VoiceClient)
voice.play(dev.FFmpegPCMAudio('song.mp3'), after=lambda error: print('finished', error))
```

`dev.VoiceClient` implements `discord.VoiceProtocol` — discord.py's documented extension point
for alternative voice backends — on top of the native transport, and is the only class in this
package that inherits from `discord.py`. Only one other thing is Python: `dev.AudioPlayer`, a thin
wrapper over the native playback thread. Everything else is Rust, including
`dev.VoiceConnectionState` (the connection state and flow), `dev.DiscordVoiceWebSocket` (the
websocket facade), the audio sources and the exception types. From `discord.py` we *use* the host
framework rather than inherit it: the client and its gateway, and the exception classes the public
API raises.

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
| Connection state and flow, reconnect policy | Rust: `_native.VoiceConnection` + `flow.rs` (phases, close codes, retries, backoff) |
| Audio sources (decode, resample, volume) | Rust: `sources.rs` on `symphonia` / `rubato` / `oprs` |
| `http(s)` inputs | `reqwest` (rustls, no OpenSSL), streamed straight into the decoder |
| Playback clock (the player thread) | Rust: `player.rs`; `player.py` is only a wrapper |
| Voice gateway opcodes and frame surface | Rust: `transport.rs` |
| `discord.py` calls (join/leave/move) | `voice_client.py`: `Guild.change_voice_state`, then the event goes to Rust |
| `ffmpeg` subprocess | **not used** — no binary, no pipe, no process to reap |

## Why

* **No PyNaCl on the audio path.** `xsalsa20poly1305*` and
  `aead_xchacha20_poly1305_rtpsize` are implemented natively, including the incrementing nonce
  behaviour discord.py relies on. The test suite decrypts every mode with PyNaCl to prove
  byte-level compatibility.
* **No FFmpeg, in any form.** The audio sources are this package's own (native
  `_native.FFmpegPCMAudio`/`FFmpegOpusAudio`): `symphonia` demuxes and decodes in process, `rubato` converts
  the sample rate, `oprs` (libopus 1.6.1, ported to pure Rust) handles Opus — which symphonia has
  no decoder for — and `seek=` replaces `-ss`. No `ffmpeg` binary, no C library, no child process:
  the extension links nothing but libc.
* **Encoding, encryption, the UDP send and the playback clock itself happen in Rust**, so
  playback jitter does not compete with Python work — the player reads one frame from the source
  and paces it against a native 20ms clock.
* **DAVE end-to-end encryption** is handled entirely in Rust with `davey`: MLS key packages,
  commit/welcome processing, epoch transitions and the passthrough windows.
* **A thin Python layer.** Only `VoiceClient` and `AudioPlayer` are Python. `VoiceClient` holds
  the one thing Rust cannot: the `discord.py` boundary. Joining, leaving and moving are
  `Guild.change_voice_state` calls made there, and the resulting gateway events are handed to the
  native connection; the flow state machine, close-code policy, retry spacing and reconnect
  backoff all live in Rust. The one thing discord.py provides is what a bot already drives: the
  client, the main gateway, and the `VoiceProtocol` extension point `VoiceClient` implements.

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

Every source is implemented in this package (`src/sources.rs`, exposed through `_native`) and
decodes in process with `symphonia` — no FFmpeg, no binary:

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

# In an async command, open a URL without blocking the event loop while it is probed:
source = await dev.FFmpegPCMAudio.open(url, headers=info['http_headers'])

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

The synchronous constructor probes the input before returning, which for an `http(s)` URL means
the event loop waits for the CDN. `await FFmpegPCMAudio.open(...)` (and the Opus equivalent) does
the probe in a native future, so a bot command can keep serving other events while that happens.

`headers=` sends extra HTTP headers with every request the reader makes (the first fetch and any
`Range` a seek issues). Extractors hand those out with the URLs they produce — `yt-dlp`'s
`http_headers`, for instance — and many CDNs reject a URL fetched without them, because the URL is
bound to the client that asked for it. A `User-Agent` there replaces this package's own; passing
headers for a local file or a file object is a `TypeError`, since nothing would send them.

`dev.AudioSource` is this package's own native base class, and nothing here inherits from
discord.py: a source only has to provide `read`/`is_opus`/`cleanup`, which is also what makes a
user's own `discord.AudioSource` (or duck-typed equivalent) playable — `dev.is_audio_source(x)`
answers that question. Native sources are subclassable from Python (the `yt-dlp` example does
exactly that with `PCMVolumeTransformer`). discord.py's FFmpeg-CLI arguments —
`before_options`, `options`, `executable`, `pipe`, `stderr`, `force_cli` — do not exist here and
raise `TypeError` naming the argument.

### Opus

The Opus codec is internal: `play()` encodes PCM sources with it, `send_audio_packet(encode=True)`
uses the same encoder, and Opus-carrying sources are passed through untouched. There is no
Python-facing encoder class to configure or create — `play()`'s keyword arguments
(`application`, `bitrate`, `fec`, `expected_packet_loss`, `bandwidth`, `signal_type`) are the
configuration, and a bad value raises `ValueError` before playback starts. If you need
`discord.opus.Encoder` itself, use `discord.opus`.

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
python/discord_ext_voice/        # the whole Python surface
├── __init__.py                  # re-exports the native names
├── voice_client.py              # VoiceClient: the discord.py boundary + flow orchestration
└── player.py                    # AudioPlayer: a wrapper over the native playback thread
src/                             # the core
├── lib.rs                       # module registration and constants
├── connection.rs                # VoiceConnection: transport + flow + negotiated state
├── flow.rs                      # phases, close codes, retry spacing, reconnect backoff
├── gateway.rs                   # DiscordVoiceWebSocket: latency + close
├── enums.rs                     # the IntEnums Python dispatches on
├── transport.rs                 # websocket + UDP actor, heartbeat, IP discovery
├── crypto.rs                    # the four RTP encryption modes
├── dave.rs                      # DAVE/MLS session and transition state machine
├── opus.rs                      # Opus encode/decode (oprs)
├── player.rs                    # the playback clock
├── source.rs                    # demuxing/decoding: symphonia, files, objects, http(s)
└── sources.rs                   # AudioSource / PCMAudio / FFmpeg*Audio / volume
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

**The connection flow's decisions are native.** `VoiceClient` awaits what only Python can await —
discord.py's `Guild.change_voice_state` — and asks the native connection what the event means:
which gateway event moves the phase where, what a close code means
(1000/4014/4015/4021/4022/other), how long a retry waits and how long a reconnect backs off. The
phase itself is the transport's, so there is one state rather than two, and the native connection
owns the credentials and the negotiated session as well. The discord.py calls stay in
`VoiceClient`, never in Rust.

**The playback clock is native.** `discord_ext_voice.AudioPlayer` is a thin wrapper over two Rust
threads: a reader that decodes up to a second ahead into a frame queue, and a clock that paces each
20ms frame from that queue, feeds silence while paused, announces the `SPEAKING` state and waits
for a reconnection. The read-ahead buffer is what keeps a streamed `http(s)` source from
stuttering when a network read takes longer than one frame; a source that is slower than
real time on average still underruns, as it must. It keeps `discord.player.AudioPlayer`'s
semantics — `DELAY = 0.02`, the `after(error)` callback, and a clock that restarts on `resume` —
without importing `discord.player`, whose `audioop` dependency would otherwise be pulled in.

### Performance notes

The 20ms audio path was written to stay allocation-light and to survive a bad network:

* disabled log records cost one atomic level check, not a `format!` plus a GIL round trip (the
  effective Python level is re-read every two seconds, so `setLevel` still works at runtime);
* RTP encryption builds the final datagram once and encrypts the payload in place, instead of
  copying the payload into a scratch buffer and then into the output;
* PCM-to-Opus conversion reuses a sample buffer owned by the encoder, and a
  `PCMVolumeTransformer` at 100% hands the source's `bytes` object straight through;
* the player's read-ahead thread decodes up to one second ahead, so a network read slower than
  20ms is absorbed by the queue instead of becoming a gap;
* one bounded UDP sender task preserves packet order (a slow send is dropped rather than
  reordered), and a source that stops producing frames fails through `after` after 30 seconds
  instead of wedging the player.

`dev.set_read_ahead(seconds)` resizes the decoder queue (default one second) and
`dev.set_read_stall_timeout(seconds)` changes the no-data timeout (`0` disables it).

On the development machine used here, a passthrough frame costs about **2.5µs** and a PCM frame
about **1.4ms**. The Opus encode dominates, and almost all of that is in-band FEC:
`play(..., expected_packet_loss=0.15)` (discord.py's default) costs ~1.2ms/frame, while
`expected_packet_loss=0.02` costs ~0.22ms and `fec=False` is similar. Lower values trade
packet-loss resilience for CPU; the default is kept for compatibility. Set
`voice.expected_packet_loss = 0.02` (and/or `voice.fec = False`) once per client to change the
default for every subsequent `play()`.

`maturin develop` builds the dev profile, so `Cargo.toml` optimizes dependencies there
too — otherwise the debug build makes the pure-Rust ciphers an order of magnitude slower than the
release wheel.

## Testing

```bash
cargo test --lib    # 42 Rust unit tests (1 ignored: it needs the network)
pytest -q           # 166 passed, 1 skipped (the crypto interop tests need PyNaCl; the live URL
                    # test needs DISCORD_EXT_VOICE_LIVE_URL)
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
* `tests/test_flow.py` for the native connection flow: the phase round-trip, the waiting
  primitives (including the timeout contract), and that every action Rust can return has a name
  in Python. The transition table itself is unit-tested in `src/flow.rs`.

## Limitations

* **Receiving audio is not implemented.** `VoiceClient.add_socket_listener` raises
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
