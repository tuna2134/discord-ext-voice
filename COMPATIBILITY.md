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
class VoiceConnectionState: ...                               # our own connection flow
# its decisions live in _native.ConnectionFlow (Rust): phases, close codes, retries, backoff
class DiscordVoiceWebSocket: ...                              # our own websocket facade
class AudioSource: ...                                        # our own source base class
class PCMAudio(AudioSource): ...
class FFmpegPCMAudio(AudioSource): ...
class FFmpegOpusAudio(AudioSource): ...
class PCMVolumeTransformer(AudioSource): ...
class AudioPlayer: ...                                        # our own playback clock (native thread)
```

`discord.VoiceProtocol` is discord.py's documented extension point for third-party backends
("such as Lavalink or a native library implementation"), which is exactly what this is — and the
only class of ours that inherits from `discord.py`. discord.py's own `VoiceClient` is
deliberately **not** inherited: it is built on PyNaCl and the Python `davey` bindings, so
inheriting it would mean bypassing its constructor and carrying code paths that can never run
here.

Everything else `discord.py` still provides is *used*, not inherited: `discord.Client` (the main
gateway that delivers `VOICE_STATE_UPDATE`/`VOICE_SERVER_UPDATE`), `Guild.change_voice_state`,
`discord.utils.MISSING`, the `SpeakingState` enum and discord.py's exception classes, which the
public API raises. `discord.player` is not imported at all — not even for its constants — so its
`audioop` dependency and its `ffmpeg` subprocess handling never load.

The Rust core is reached through a single object, `discord_ext_voice._native.VoiceConnection`,
exposed as `voice.native` (`voice._connection.native`).

## `VoiceClient`

`VoiceClient` lives in `discord_ext_voice/voice_client.py` and is a `VoiceProtocol`
implementation, so its members are written here rather than inherited:

| Member | Behaviour |
| --- | --- |
| `__init__(client, channel)`, `create_connection_state()` | build the native-backed `VoiceConnectionState` |
| `on_voice_state_update`, `on_voice_server_update` | forwarded to the connection state |
| `connect`, `disconnect`, `move_to`, `is_connected`, `wait_until_connected` | thin delegations to the connection state |
| `guild`, `user`, `session_id`, `token`, `endpoint`, `ssrc`, `mode`, `secret_key`, `ws`, `timeout` | read from the connection state |
| `latency`, `average_latency`, `voice_privacy_code` | read from the native transport |
| `play`, `is_playing`, `is_paused`, `stop`, `pause`, `resume`, `source` | discord.py's semantics; `play` builds the native Opus encoder and starts a `discord_ext_voice.AudioPlayer` |
| `send_audio_packet` | encodes, DAVE-encrypts, RTP-encrypts and sends in Rust |
| `supported_modes` | read from `discord.VoiceClient.supported_modes` so the two cannot drift |
| `cleanup` | inherited from `VoiceProtocol` (removes the client from the state cache) |
| `native` | **added** → the `_native.VoiceConnection` handle |

`play` keeps discord.py's exact signature, validation and `ClientException`/`TypeError`
behaviour — only the encoder is swapped, so no `discord.opus.load_opus()` is needed.

Deliberately absent, because the native transport owns them and nothing reads them:
`checked_add`, `sequence`, `timestamp`, `_incr_nonce`, `server_id`, `socket`, `_get_voice_packet`
and the `_encrypt_*` helpers (the PyNaCl-based packet builder).

## `VoiceConnectionState`

The connection flow is split in two. The *decisions* are Rust
(`_native.ConnectionFlow`, `src/flow.rs`): the phase ladder, which gateway event moves it where,
what a close code means, the 5 connection attempts, the `1 + i * 2` retry spacing, the jittered
reconnect backoff, and the "wait until the flow reaches one of these phases" primitive.
`discord_ext_voice/voice_state.py` is the awaiting half — discord.py's `Guild.change_voice_state`,
the native handshake, the websocket close — and dispatches on the action the flow returns.

The behaviour is `discord.py`'s: the state names and their order, the transition table, the
close-code handling and the retry/backoff policy are the same on purpose. The phase itself is the
transport's, so `state` and `VoiceConnection.phase` cannot disagree.

| Member | Behaviour |
| --- | --- |
| `connect`, `_connect`, `_inner_connect`, `_wrap_connect` | our flow: `asyncio.wait_for(timeout)` around each attempt, with the attempt count and spacing from `_native.ConnectionFlow` |
| `disconnect`, `soft_disconnect`, `move_to`, `is_connected` | ours; `wait`/`wait_async`/`_wait_for_state` wait on the native flow (and `_wait_for_state` keeps `asyncio.TimeoutError`) |
| `_poll_voice_ws`, `_potential_reconnect` | ours: they await the socket and dispatch on the native close-code verdict (`CloseAction`); the backoff is native |
| `ConnectionFlowState` | **ours**, with discord.py's names and values (`disconnected` = 0 … `connected` = 8) |
| `timeout`, `reconnect`, `self_deaf`, `self_mute`, `token`, `session_id`, `endpoint`, `server_id` | plain attributes, as in discord.py |
| `ip`, `port`, `voice_port`, `endpoint_ip`, `ssrc`, `mode`, `secret_key` | populated from the native handshake |
| `state` (getter/setter) | a view of the native phase: reading and writing it moves the transport with it |
| `dave_protocol_version`, `dave_session`, `can_encrypt`, `max_dave_protocol_version` | live values from the native session (writes ignored) |
| `_connect_websocket`, `_handshake_websocket` | the native handshake, with errors translated |
| `send_packet` | native UDP send |
| `reinit_dave_session` | native DAVE: recreates the MLS session and sends a fresh key package |
| `add_socket_listener`, `remove_socket_listener` | `NotImplementedError` (receive side) |
| `socket`, `ws` | `socket` stays `discord.utils.MISSING` (Rust owns it, use `state.native`); `ws` is this package's websocket facade |
| `native` | **added** |
| `_create_socket`, `_execute_transition`, `_recover_from_invalid_commit`, `dave_pending_transitions`, `dave_downgraded` | **not present**: the native side owns the socket and answers the DAVE transitions itself |

The error translation in `_handshake_websocket` matters: the native handshake raises
`HandshakeError`/`ConnectionClosedError`, which are converted to `asyncio.TimeoutError` and
`discord.ConnectionClosed` so the flow retries and resumes exactly like discord.py's.

## `DiscordVoiceWebSocket`

This is this package's own class (`discord_ext_voice/gateway.py`), named after discord.py's
because it plays the same role. It is small on purpose: Rust owns the socket, the heartbeat and
every incoming frame, so there is no aiohttp connection, no keep-alive thread and no frame
reader left to wrap.

| Member | Behaviour |
| --- | --- |
| Opcode constants (`IDENTIFY` … `MLS_INVALID_COMMIT_WELCOME`) | **ours**, with Discord's values, so the numbers match `discord.gateway.DiscordVoiceWebSocket` |
| `speak` | sends `SPEAKING` with the native SSRC, for anything driving the websocket by hand; the native player sends the same frame itself |
| `close`, `wait_closed` | close the native socket / wait for it to end and return `(code, reason)` |
| `poll_event` | waits for the native socket to end and raises `discord.ConnectionClosed` |
| `send_as_json`, `send_binary`, `send_heartbeat` | raw frame writers, for anything not covered above |
| `close_code` | the native close code, falling back to the last code we closed with |
| `seq_ack`, `secret_key`, `latency`, `average_latency` | native values; writes ignored |
| `identify`, `resume`, `select_protocol`, `client_connect`, `send_transition_ready` | **not present**: the native handshake and DAVE session send those frames themselves |

## Audio sources

| Class | Source |
| --- | --- |
| `AudioSource` | **ours**, a standalone base class (nothing in this package inherits from `discord.py`); `is_audio_source(x)` accepts it, or any object with the same `read`/`is_opus`/`cleanup` methods |
| `PCMAudio` | **ours**: raw PCM from a file object |
| `FFmpegPCMAudio`, `FFmpegOpusAudio` | **ours**: the native readers (`_native.DecodeSource` / `_native.OpusSource`), which take a path, a file object or an `http(s)` URL (streamed with `reqwest`) |
| `PCMVolumeTransformer` | **ours**: the native `_native.scale_pcm`, so `audioop` is not needed |
| `AudioPlayer` | **ours**, a thin wrapper over the native thread: 20ms pacing, pause/resume, the silence frames, the `SPEAKING` announcements and the reconnection wait are Rust |

`FFmpegPCMAudio`/`FFmpegOpusAudio` take the media plus `seek` (the native `-ss`). discord.py's
FFmpeg-CLI arguments (`executable`, `pipe`, `stderr`, `before_options`, `options`,
`force_cli`) raise `TypeError` naming the argument, and `FFmpegAudio` is not re-exported: there
is no subprocess to configure.

`FFmpegOpusAudio.probe`/`probe_codec` use libav (`_native.probe`) instead of spawning
`ffprobe`/`ffmpeg`, so the reported bitrate is always `None` and the `method`/`executable`
arguments do not exist. `from_probe` is not needed: the constructor already probes and passes
Opus packets through when the input has them.

## `discord.opus`

| Member | Source |
| --- | --- |
| `Encoder`, `Decoder` | **native** (`audiopus`/libopus) — same keyword arguments, validation, `ValueError`s and class constants as `discord.opus.Encoder` |
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
7. **`state.socket` is `discord.utils.MISSING`.** Rust owns the UDP socket; use
   `state.native` (or `state.native.send_packet/...`) instead.
8. **Nothing inherits from `discord.py` except `VoiceClient`.** `VoiceConnectionState`,
   `DiscordVoiceWebSocket`, the audio sources and the player are this package's own classes, so
   an `isinstance` check against discord.py's equivalents is `False` (the *behaviour* is the
   same, and the enums keep discord.py's names and values). `dev.AudioPlayer` is not a
   `threading.Thread`: `start`/`stop`/`pause`/`resume`/`join` behave the same, but `is_alive()`
   reports the native playback thread and attributes like `loops`/`_start` do not exist.

## Additions

* `discord_ext_voice.probe(source)` — codec/rate/channels/duration without decoding.
* `voice.native` / `state.native` — the `_native.VoiceConnection`: `is_connected()`, `phase`,
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
* `voice.native.flow` — the `_native.ConnectionFlow`: `state` (the same 0-8 ladder as
  `ConnectionFlowState`), `joined`, `expecting_disconnect`, `voice_state_update(...)`,
  `voice_server_update(...)`, `close_action(code, disconnected, reconnect)`,
  `note_attempt()`, `retry_wait(attempt)`, `reconnect_delay()`, `reset_backoff()`,
  `wait_for_state(states, timeout)` and the blocking `wait_connected(timeout)`.
  `MAX_ATTEMPTS` and the action numbers are class attributes, so the Python enums are built
  from them rather than restated.
* `FFmpegOpusAudio.probe_codec(source)` and the native `_native.scale_pcm` behind
  `PCMVolumeTransformer`.
* `is_audio_source(candidate)` — whether an object can be played (our sources, or anything with
  the same three methods).
* `DISCORD_EXT_VOICE_MAX_DAVE_VERSION=0` disables the DAVE advertisement in IDENTIFY (a
  diagnostic switch for calls that refuse to come up; end-to-end encryption is then off).
* `ws://host:port` endpoints (plaintext websocket), used for local gateways and testing.
