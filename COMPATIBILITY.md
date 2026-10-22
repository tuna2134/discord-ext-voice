# How this library relates to `discord.py`

This package is not a re-implementation of `discord.py`'s voice stack, and it is not a subclass
of it either: the voice implementation — transport, codec, encryption, connection flow and audio
sources — is this package's own, and only the *host framework* comes from `discord.py`. This
document lists exactly what comes from where, and the handful of intentional behaviour
differences.

It reflects `discord.py` **2.7.1** (`discord/voice_client.py`, `discord/voice_state.py`,
`discord/gateway.py`, `discord/player.py`, `discord/opus.py`, `discord/errors.py`).

## Layering

```python
class VoiceClient(discord.voice_client.VoiceProtocol): ...    # the only discord.py inheritance
class AudioPlayer: ...                                        # wrapper over the native thread

# Everything else is Rust, in discord_ext_voice._native:
#   VoiceConnection          transport + negotiated session + connection state/flow
#   DiscordVoiceWebSocket    latency + close, over the native socket
#   AudioSource, PCMAudio, FFmpegPCMAudio, FFmpegOpusAudio, PCMVolumeTransformer
#   ConnectionFlowState, VoiceStateAction, VoiceServerAction, CloseAction
#   VoiceError and its subclasses, including FFmpegSourceError
```

`discord.VoiceProtocol` is discord.py's documented extension point for third-party backends
("such as Lavalink or a native library implementation"), which is exactly what this is — and the
only class of ours that inherits from `discord.py`. discord.py's own `VoiceClient` is
deliberately **not** inherited: it is built on PyNaCl and the Python `davey` bindings, so
inheriting it would mean bypassing its constructor and carrying code paths that can never run
here.

Everything else `discord.py` still provides is *used*, not inherited: `discord.Client` (the main
gateway that delivers `VOICE_STATE_UPDATE`/`VOICE_SERVER_UPDATE`), `Guild.change_voice_state`,
and discord.py's exception classes, which the public API raises. `discord.player` is not imported
at all — not even for its constants — so its `audioop` dependency and its `ffmpeg` subprocess
handling never load.

The Rust core is reached through a single object, `discord_ext_voice._native.VoiceConnection`,
exposed as `voice.native`; it is the same object as `voice._connection` (the connection state and
the transport are one thing, not two).

## `VoiceClient`

`VoiceClient` lives in `discord_ext_voice/voice_client.py` and is a `VoiceProtocol`
implementation. It is also the *only* place discord.py is written to: the `Guild.change_voice_state`
calls happen here, in Python, and the resulting events are fed to the native connection.

| Member | Behaviour |
| --- | --- |
| `__init__(client, channel)`, `create_connection_state()` | build the native `VoiceConnection` |
| `on_voice_state_update`, `on_voice_server_update` | update the native flow, then perform the discord.py-side action it returns (channel pointer, disconnect, reconnect) |
| `connect`, `disconnect`, `move_to`, `is_connected`, `wait_until_connected` | the orchestration: `Guild.change_voice_state` here, handshake/reconnect/close in Rust |
| `guild`, `user`, `session_id`, `endpoint`, `ssrc`, `mode`, `ws`, `timeout` | read from the native connection (or, for `timeout`, stored here) |
| `token`, `secret_key` | **not exposed**: the session credentials stay in the native connection, where the handshake and the RTP encryption use them |
| `latency`, `average_latency`, `voice_privacy_code` | read from the native transport |
| `play`, `is_playing`, `is_paused`, `stop`, `pause`, `resume`, `source` | discord.py's semantics; `play` builds the native Opus encoder and starts a `discord_ext_voice.AudioPlayer` |
| `send_audio_packet` | encodes, DAVE-encrypts, RTP-encrypts and sends in Rust |
| `supported_modes` | read from the native `SUPPORTED_MODES`, which matches `discord.VoiceClient.supported_modes` |
| `cleanup` | inherited from `VoiceProtocol` (removes the client from the state cache) |
| `native` | **added** → the `_native.VoiceConnection` handle (same object as `_connection`) |

`play` keeps discord.py's exact signature, validation and `ClientException`/`TypeError`
behaviour — only the encoder is swapped, so no `discord.opus.load_opus()` is needed.

Deliberately absent, because the native transport owns them and nothing reads them:
`checked_add`, `sequence`, `timestamp`, `_incr_nonce`, `server_id`, `socket`, `_get_voice_packet`
and the `_encrypt_*` helpers (the PyNaCl-based packet builder).

## `VoiceConnection` (the native connection state)

`_native.VoiceConnection` (`src/connection.rs` + `src/flow.rs`) is both the transport handle and
the connection state. The flow's *decisions* are in `flow.rs`: the phase ladder, which gateway
event moves it where, what a close code means, the 5 connection attempts, the `1 + i * 2` retry
spacing, the jittered reconnect backoff, and the "wait until the flow reaches one of these
phases" primitive. The phase itself is the transport's, so there is one state rather than two.

The behaviour is `discord.py`'s: the state names and their order, the transition table, the
close-code handling and the retry/backoff policy are the same on purpose.

| Member | Behaviour |
| --- | --- |
| `state`, `phase`, `set_phase(value)` | the phase as a `ConnectionFlowState` member, as the raw number, and the previous number |
| `joined`, `expecting_disconnect`, `disconnected` | the gateway bookkeeping the flow dispatches on (readable and writable) |
| `voice_state_update(channel_id, current_channel_id)`, `voice_server_update(has_endpoint, has_token, changed)` | run the transition table and return a `VoiceStateAction`/`VoiceServerAction` |
| `close_action(code, reconnect)` | the close-code verdict as a `CloseAction` |
| `note_attempt()`, `retry_wait(attempt)`, `MAX_ATTEMPTS` | the connect-attempt policy (`_native.MAX_ATTEMPTS`, `_native.retry_wait`) |
| `reconnect_delay()`, `reset_backoff()` | the jittered reconnect backoff |
| `wait_for_state(states, timeout)`, `wait_connected(timeout)`, `wait(timeout)` | await/block on the phase; `wait_for_state` returns `False` on timeout so the caller raises `asyncio.TimeoutError` |
| `handshake(resume=False, timeout=...)`, `close(code=None)`, `wait_closed()` | the native handshake and socket lifecycle |
| `ip`, `port`, `voice_port`, `ssrc`, `mode`, `secret_key`, `close_code`, `close_reason` | populated by the native handshake |
| `dave_protocol_version`, `dave_session`, `can_encrypt` (property), `max_dave_protocol_version`, `voice_privacy_code`, `reinit_dave_session()` | live values from the native DAVE session |
| `send_packet`, `send_audio_packet`, `decrypt_rtp` | native UDP/RTP (receive-side decryption is for tests and extensions) |
| `_create_socket`, `_execute_transition`, `_recover_from_invalid_commit`, `dave_pending_transitions`, `dave_downgraded` | **not present**: the native side owns the socket and answers the DAVE transitions itself |

The error translation in `VoiceClient._open_websocket` matters: the native handshake raises
`HandshakeError`/`ConnectionClosedError`, which are converted to `asyncio.TimeoutError` and
`discord.ConnectionClosed` so the flow retries and resumes exactly like discord.py's.

## `DiscordVoiceWebSocket`

This is this package's own class (`src/gateway.rs`, exposed as
`discord_ext_voice._native.DiscordVoiceWebSocket`), named after discord.py's because it plays the
same role. It is small on purpose: Rust owns the socket, the heartbeat and every incoming frame,
so there is no aiohttp connection, no keep-alive thread and no frame reader left to wrap.

| Member | Behaviour |
| --- | --- |
| `latency` | the native heartbeat round trip, in seconds |
| `close(code=1000)` | close the native socket (the reconnect loop and `disconnect` call it) |
| `close_code` | the close code, once the socket ended |
| `resume` | whether this connection's handshake sends RESUME rather than IDENTIFY |
| the opcode constants, `speak`, `send_as_json`, `send_binary`, `send_heartbeat`, `poll_event`, `wait_closed`, `seq_ack`, `secret_key`, `average_latency` | **not present**: the native transport sends and reads every frame, and the negotiated values are read from the native connection (`VoiceClient`, `VoiceConnection`) |

## Audio sources

| Class | Source |
| --- | --- |
| `AudioSource` | **ours** (`src/sources.rs`), a native base class that Python may subclass; `is_audio_source(x)` accepts it, or any object with the same `read`/`is_opus`/`cleanup` methods |
| `PCMAudio` | **ours**: raw PCM from a file object |
| `FFmpegPCMAudio`, `FFmpegOpusAudio` | **ours**: built on the native readers (`_native.DecodeSource` / `_native.OpusSource`) with `symphonia`, which take a path, a file object or an `http(s)` URL (streamed with `reqwest`) |
| `PCMVolumeTransformer` | **ours**: the native `_native.scale_pcm`, so `audioop` is not needed |
| `AudioPlayer` | **ours**, a thin wrapper over the native threads: a one-second read-ahead queue, 20ms pacing, pause/resume, the silence frames, the `SPEAKING` announcements and the reconnection wait are Rust |

## `discord.opus`

| Member | Source |
| --- | --- |
| `Encoder`, `Decoder` | **not provided**: the codec is internal. `play()` and `send_audio_packet` encode with `oprs` (a pure-Rust libopus port) and validate the same parameters, raising `ValueError`; use `discord.opus` if you need the classes themselves |
| `OpusError` | native; raised for codec failures |
| `OPUS_SILENCE`, `APPLICATION_AUDIO`, `APPLICATION_VOIP`, `APPLICATION_LOWDELAY` | re-exported from `discord.opus` |
| `load_opus`, `is_loaded`, `OpusNotLoaded` | **not re-exported from this package**; use `discord.opus` if you need discord.py's ctypes encoder. The native encoder requires no loading step |
| `APPLICATION_CTL`, `BAND_CTL`, `SIGNAL_CTL` | only exist as private `discord.opus._OpusStruct` attributes upstream, so they are not exposed here |

## Exceptions

| Type | Source |
| --- | --- |
| `ClientException`, `ConnectionClosed`, `DiscordException` | re-exported from `discord.errors` |
| `OpusNotLoaded` | re-exported from `discord.opus` |
| `VoiceError`, `NotConnected`, `AlreadyConnected`, `HandshakeError`, `ConnectionClosedError`, `ProtocolError`, `EncryptionError`, `DaveError`, `OpusError`, `FFmpegError` | native (`VoiceError` is the base) |
| `FFmpegSourceError` | **added**, subclasses the native `FFmpegError` |

The public API still *raises* `discord.ClientException` where discord.py does (`play`,
`stop`, `move_to`, `send_audio_packet`) and `discord.ConnectionClosed` when the voice websocket
closes, so user code written against discord.py keeps working. What it does not do is
*subclass* them: `except discord.ClientException` catches what the API raises, and everything
else is reachable through the native `VoiceError` hierarchy.

## Intentional differences

1. **PyNaCl and the Python `davey` bindings are not used.** `VoiceClient` is a
   `VoiceProtocol` implementation, so discord.py's capability checks, its PyNaCl-based packet
   builder and its Python `davey` session are never entered. discord.py still logs its own
   capability warning at login, because it inspects its own backend; see the README.
   `isinstance(voice, discord.VoiceClient)` is therefore `False` — use
   `isinstance(voice, discord.VoiceProtocol)` (which is what `channel.connect` checks).
2. **A missing local file raises `FFmpegSourceError` at construction**, where discord.py only
   surfaces it once the subprocess exits. A `PathLike` is accepted.
3. **`before_options`/`options` raise `TypeError`.** They are FFmpeg CLI grammar, which the
   in-process decoder cannot express; `seek=` replaces the common `-ss` case, and there is no
   subprocess to hand the rest to. `force_cli`, `pipe`, `stderr` and `executable` go with them.
4. **Network URLs go through the native reader.** `FFmpegPCMAudio('https://…')` streams the
   response with `reqwest` (rustls) instead of spawning the `ffmpeg` binary with a URL, which is
   what discord.py does; `seek=` and mid-stream seeks issue `Range` requests where the server
   supports them.
5. **Reconnect event ordering.** `_inner_connect` reaches
   `got_both_voice_updates` after a `4015` resume only if the server update arrives before the
   state update, because `voice_state_update` does not transition away from `connected`. This
   is discord.py's own behaviour; the test gateway dispatches in that order.
6. **`add_socket_listener`/`remove_socket_listener` raise `NotImplementedError`** instead of
   silently registering a callback that would never fire.
7. **There is no `socket` attribute to poke.** Rust owns the UDP socket; use
   `voice.native` (for example `voice.native.send_packet(...)`) instead.
8. **Nothing inherits from `discord.py` except `VoiceClient`.** `VoiceConnection`,
   `DiscordVoiceWebSocket`, the audio sources and the player are this package's own classes, so
   an `isinstance` check against discord.py's equivalents is `False` (the *behaviour* is the
   same, and the enums keep discord.py's names and values). `dev.AudioPlayer` is not a
   `threading.Thread`: `start`/`stop`/`pause`/`resume`/`join` behave the same, but `is_alive()`
   reports the native playback thread and attributes like `loops`/`_start` do not exist.

## Additions

* `discord_ext_voice.probe(source)` — codec/rate/channels/duration without decoding.
* `voice.native` / `voice._connection` — the `_native.VoiceConnection`: `is_connected()`, `phase`,
  `latency`, `average_latency`, `seq_ack`, `ssrc`, `ip`/`port`/`voice_port`, `mode`,
  `secret_key`, `dave_protocol_version`, `can_encrypt`, `has_dave_session`,
  `voice_privacy_code`, `decrypt_rtp`, `send_packet`, `close_code`, `close_reason`,
  `wait_closed`, ...
* `FFmpegPCMAudio(source, seek=...)` / `FFmpegOpusAudio(source, seek=...)` — the native `-ss`,
  implemented by seeking the demuxer and rebuilding the decoder.
* A dropped `http(s)` stream is resumed from the byte it reached (a `Range` request, or a
  restart-and-skip for a server that ignores ranges) instead of ending playback. discord.py
  hands the URL to the `ffmpeg` binary, which has its own reconnect behaviour.
* `FFmpegPCMAudio(url, headers={...})` / `FFmpegOpusAudio(url, headers={...})` /
  `probe(url, headers={...})` — extra HTTP headers for every request the URL reader makes,
  which is what a URL from an extractor needs (`yt-dlp`'s `http_headers`); a `User-Agent` there
  replaces the package's own. discord.py has no equivalent, because it hands the URL to the
  `ffmpeg` binary.
* `AudioPlayer.join(timeout=None)` / `is_alive()` — wait for the playback thread from Python.
* `await FFmpegPCMAudio.open(source, ...)` / `await FFmpegOpusAudio.open(source, ...)` — open a
  source (including an `http(s)` URL) without blocking the event loop during the probe.
* `set_read_ahead(seconds)` / `set_read_stall_timeout(seconds)` — tune the native read-ahead
  window (default 1s) and the no-data timeout (default 30s; `0` disables it).
* `voice.fec` / `voice.expected_packet_loss` — per-client defaults for `play()`'s Opus encoder
  parameters. In-band FEC is the dominant encoding cost; discord.py's `0.15` default costs about
  1.2ms per 20ms frame, while `0.02` costs about 0.22ms.
* The player reads up to one second ahead of the clock into a native frame queue, so a streamed
  source whose network read occasionally takes longer than 20ms does not stutter.
* `voice.native` (same object as `voice._connection`) — the `_native.VoiceConnection`:
  `state`/`phase`/`set_phase`, `joined`, `expecting_disconnect`, `disconnected`,
  `voice_state_update(...)`, `voice_server_update(...)`, `close_action(code, reconnect)`,
  `note_attempt()`, `reconnect_delay()`, `reset_backoff()`, `wait_for_state(states, timeout)`
  and the blocking `wait_connected(timeout)`. `_native.MAX_ATTEMPTS` and `_native.retry_wait()`
  are module-level; the dispatch enums (`ConnectionFlowState`, `VoiceStateAction`,
  `VoiceServerAction`, `CloseAction`) are built in Rust, so the numbers cannot drift.
* `FFmpegOpusAudio.probe_codec(source)` and the native `_native.scale_pcm` behind
  `PCMVolumeTransformer`.
* `is_audio_source(candidate)` — whether an object can be played (our sources, or anything with
  the same three methods).
* `DISCORD_EXT_VOICE_MAX_DAVE_VERSION=0` disables the DAVE advertisement in IDENTIFY (a
  diagnostic switch for calls that refuse to come up; end-to-end encryption is then off).
* `ws://host:port` endpoints (plaintext websocket), used for local gateways and testing.
