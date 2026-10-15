# How this library relates to `discord.py`

This package is not a re-implementation of `discord.py`'s voice stack — it is a **subclass
layer** that replaces the transport, codec and encryption with Rust while inheriting everything
else. This document lists exactly what comes from where, and the handful of intentional
behaviour differences.

It reflects `discord.py` **2.7.1** (`discord/voice_client.py`, `discord/voice_state.py`,
`discord/gateway.py`, `discord/player.py`, `discord/opus.py`, `discord/errors.py`).

## Layering

```python
class VoiceClient(discord.voice_client.VoiceProtocol): ...                   # implements the protocol
class VoiceConnectionState(discord.voice_state.VoiceConnectionState): ...    # 12 overrides
class DiscordVoiceWebSocket(discord.gateway.DiscordVoiceWebSocket): ...      # read side only
class AudioSource(discord.player.AudioSource): ...                           # our own base class
class FFmpegPCMAudio(AudioSource): ...                                       # native reader
class FFmpegOpusAudio(AudioSource): ...                                      # native reader
class PCMVolumeTransformer(AudioSource): ...                                 # native scaling
```

`discord.VoiceProtocol` is discord.py's documented extension point for third-party backends
("such as Lavalink or a native library implementation"), which is exactly what this is.
discord.py's own `VoiceClient` is deliberately **not** inherited: it is built on PyNaCl and the
Python `davey` bindings, so inheriting it would mean bypassing its constructor and carrying
code paths that can never run here.

The Rust core is reached through a single object, `discord_ext_voice._native.VoiceConnection`,
exposed as `voice.native` (`voice._connection.native`).

## `VoiceClient`

`VoiceClient` lives in `discord_ext_voice/voice_client.py` and is a `VoiceProtocol`
implementation, so its members are written here rather than inherited:

| Member | Behaviour |
| --- | --- |
| `__init__(client, channel)`, `create_connection_state()` | build the native-backed `VoiceConnectionState` |
| `on_voice_state_update`, `on_voice_server_update` | forwarded to the connection state |
| `connect`, `disconnect`, `move_to`, `is_connected`, `wait_until_connected` | thin delegations to the connection state (which is discord.py's) |
| `guild`, `user`, `session_id`, `token`, `endpoint`, `ssrc`, `mode`, `secret_key`, `ws`, `timeout` | read from the connection state |
| `latency`, `average_latency`, `voice_privacy_code` | read from the native transport |
| `play`, `is_playing`, `is_paused`, `stop`, `pause`, `resume`, `source` | discord.py's semantics; `play` installs the native Opus encoder and then uses `discord.player.AudioPlayer` |
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

| Member | Source |
| --- | --- |
| `voice_state_update`, `voice_server_update` | inherited; both sync the payload into the native core first |
| `connect`, `_connect`, `_inner_connect`, `_wrap_connect` | inherited (5 attempts, backoff) |
| `disconnect`, `soft_disconnect`, `move_to`, `wait`, `wait_async`, `_wait_for_state`, `is_connected` | inherited |
| `_poll_voice_ws` (close codes 1000/4014/4015/4021/4022), `_potential_reconnect` | inherited |
| `ConnectionFlowState` | inherited from `discord.voice_state` |
| `timeout`, `reconnect`, `self_deaf`, `self_mute`, `token`, `session_id`, `endpoint`, `server_id` | inherited |
| `ip`, `port`, `voice_port`, `endpoint_ip`, `ssrc`, `mode`, `secret_key` | populated from the native handshake |
| `state` (getter/setter) | **overridden** → also pushes the phase into the native state machine |
| `dave_protocol_version` | **overridden** → live value from the native session (writes ignored) |
| `max_dave_protocol_version`, `can_encrypt`, `dave_session` | **overridden** → native session |
| `_create_socket` | **overridden** → no-op, Rust owns the UDP socket |
| `_connect_websocket`, `_handshake_websocket` | **overridden** → native handshake, errors translated |
| `send_packet` | **overridden** → native UDP send |
| `reinit_dave_session`, `_recover_from_invalid_commit`, `_execute_transition` | **overridden** → native DAVE |
| `add_socket_listener`, `remove_socket_listener` | **overridden** → `NotImplementedError` (receive side) |
| `socket` | stays `discord.utils.MISSING`; use `state.native` |
| `native` | **added** |

The error translation in `_handshake_websocket` matters: the native handshake raises
`HandshakeError`/`ConnectionClosedError`, which are converted to `asyncio.TimeoutError` and
`discord.ConnectionClosed` so that discord.py's inherited retry and resume logic runs
unchanged.

## `DiscordVoiceWebSocket`

| Member | Source |
| --- | --- |
| Opcode constants (`IDENTIFY` … `MLS_INVALID_COMMIT_WELCOME`) | inherited |
| `send_as_json`, `send_binary`, `send_heartbeat`, `speak`, `identify`, `resume`, `select_protocol`, `client_connect`, `send_transition_ready`, `close` | inherited |
| `close_code` | **added** (used by `discord.ConnectionClosed`) |
| `seq_ack`, `secret_key`, `latency`, `average_latency` | **overridden** → native values; writes ignored |
| `poll_event` | **overridden** → waits for the native socket to end and raises `discord.ConnectionClosed` |
| `received_message`, `received_binary_message`, `initial_connection`, `discover_ip`, `load_secret_key` | **not used** — the native reader handles those frames, so these inherited methods are never called |

Outgoing frames therefore go through discord.py's own payload builders, into a
`_NativeSocket` shim (`send_str` / `send_bytes` / `close`) that maps onto the Rust writer.

## Audio sources

| Class | Source |
| --- | --- |
| `AudioSource` | **ours**, subclassing `discord.player.AudioSource` so discord.py's contract (and any `isinstance` check) still holds |
| `PCMAudio` | **ours**: raw PCM from a file object |
| `FFmpegPCMAudio`, `FFmpegOpusAudio` | **ours**: the native readers (`_native.DecodeSource` / `_native.OpusSource`) |
| `PCMVolumeTransformer` | **ours**: the native `_native.scale_pcm`, so `audioop` is not needed |
| `AudioPlayer` | `discord.player.AudioPlayer`, reused unchanged as the playback clock |

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
| `FFmpegSourceError` | **added**, subclasses both `discord.ClientException` and the native `FFmpegError` |

So `except discord.ClientException` catches every client-level failure, and native-only
failures (protocol/DAVE/crypto) are catchable through the `VoiceError` hierarchy.

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
4. **Network URLs are not supported.** The native reader takes a path or a file object; fetch
   the URL with your own HTTP client first.
5. **Reconnect event ordering.** The inherited `_inner_connect` reaches
   `got_both_voice_updates` after a `4015` resume only if the server update arrives before the
   state update, because `voice_state_update` does not transition away from `connected`. This
   is discord.py's own behaviour; the test gateway dispatches in that order.
6. **`add_socket_listener`/`remove_socket_listener` raise `NotImplementedError`** instead of
   silently registering a callback that would never fire.
7. **`state.socket` is `discord.utils.MISSING`.** Rust owns the UDP socket; use
   `state.native` (or `state.native.send_packet/...`) instead.

## Additions

* `discord_ext_voice.probe(source)` — codec/rate/channels/duration without decoding.
* `voice.native` / `state.native` — the `_native.VoiceConnection`: `is_connected()`, `phase`,
  `latency`, `average_latency`, `seq_ack`, `ssrc`, `ip`/`port`/`voice_port`, `mode`,
  `secret_key`, `dave_protocol_version`, `can_encrypt`, `has_dave_session`,
  `voice_privacy_code`, `decrypt_rtp`, `send_packet`, `close_code`, `close_reason`,
  `wait_closed`, ...
* `FFmpegPCMAudio(source, seek=...)` / `FFmpegOpusAudio(source, seek=...)` — the native `-ss`,
  implemented by seeking the demuxer and rebuilding the decoder.
* `FFmpegOpusAudio.probe_codec(source)` and the native `_native.scale_pcm` behind
  `PCMVolumeTransformer`.
* `DISCORD_EXT_VOICE_MAX_DAVE_VERSION=0` disables the DAVE advertisement in IDENTIFY (a
  diagnostic switch for calls that refuse to come up; end-to-end encryption is then off).
* `ws://host:port` endpoints (plaintext websocket), used for local gateways and testing.
