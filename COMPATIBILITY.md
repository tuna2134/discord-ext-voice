# Compatibility with `discord.py`

This document maps the `discord.py` voice API onto this library, and lists every deliberate
difference. It reflects `discord.py` 2.7.1 (`discord/voice_client.py`, `discord/voice_state.py`,
`discord/gateway.py`, `discord/player.py`, `discord/opus.py`, `discord/oggparse.py`).

## `VoiceClient`

| `discord.VoiceClient` | Status |
| --- | --- |
| `__init__(client, channel)` | Same signature; subclasses `discord.voice_client.VoiceProtocol` when `discord.py` is importable |
| `connect(*, reconnect, timeout, self_deaf, self_mute)` | Same |
| `disconnect(*, force=False)` | Same |
| `move_to(channel, *, timeout=30.0)` | Same |
| `is_connected()` | Same |
| `wait_until_connected(timeout=30.0)` | Same (blocking) |
| `play(source, *, after, application, bitrate, fec, expected_packet_loss, bandwidth, signal_type)` | Same |
| `is_playing()`, `is_paused()`, `stop()`, `pause()`, `resume()` | Same |
| `source` (getter/setter) | Same |
| `send_audio_packet(data, *, encode=True)` | Same |
| `guild`, `user`, `channel`, `session_id`, `token`, `endpoint`, `ssrc`, `mode`, `secret_key` | Same |
| `ws`, `timeout`, `latency`, `average_latency` | Same |
| `voice_privacy_code`, `dave_protocol_version` | Same (`voice_privacy_code` is `None` without an active DAVE session) |
| `checked_add(attr, value, limit)` | Same (still used by `discord.py`-style code) |
| `create_connection_state()`, `on_voice_state_update()`, `on_voice_server_update()`, `cleanup()` | Same |
| `supported_modes`, `warn_nacl`, `warn_dave` | Same values; `warn_nacl`/`warn_dave` are `False` because both features are always available |
| `_incr_nonce`, `sequence`, `timestamp`, `encoder` | Kept in sync for introspection; the authoritative counters live in Rust |

`voice.client.native` is an addition: the underlying
`discord_ext_voice._native.VoiceConnection`, useful for diagnostics.

## `VoiceConnectionState`

| Member | Status |
| --- | --- |
| `ConnectionFlowState` | Same values 0–8 |
| `state` getter/setter | Same, and mirrored into the native phase |
| `timeout`, `reconnect`, `self_deaf`, `self_mute` | Same |
| `token`, `session_id`, `endpoint`, `endpoint_ip`, `server_id`, `ip`, `port`, `voice_port` | Same |
| `ws`, `secret_key`, `ssrc`, `mode` | Same |
| `dave_session`, `dave_protocol_version`, `can_encrypt`, `max_dave_protocol_version` | Same semantics; `dave_session` is a native handle |
| `connect`, `disconnect`, `soft_disconnect`, `move_to`, `wait`, `wait_async`, `is_connected` | Same |
| `voice_state_update`, `voice_server_update` | Same state machine, including channel moves mid-connect |
| `reinit_dave_session`, `_recover_from_invalid_commit`, `_execute_transition` | Present; the work happens inside the native reader task |
| `send_packet` | Same (raw UDP) |
| `socket` | **Different**: returns the native UDP handle instead of a `socket.socket` |
| `add_socket_listener`, `remove_socket_listener` | **Not implemented**: raise `NotImplementedError` (receive-side audio is out of scope) |

Close-code handling in the websocket poll loop matches `discord.py`:
`1000` normal, `4014`/`4022` potential reconnect, `4015` resume, `4021` rate limited,
anything else exponential backoff.

One improvement: `_voice_disconnect` sets `_expecting_disconnect` *before* asking the guild to
change voice state, which removes a race where the resulting `VOICE_STATE_UPDATE` looked like
an external disconnect and recursed.

## `DiscordVoiceWebSocket`

| Member | Status |
| --- | --- |
| All opcode constants (`IDENTIFY` … `MLS_INVALID_COMMIT_WELCOME`) | Same values |
| `send_as_json`, `send_binary`, `send_heartbeat`, `speak`, `close` | Same coroutine surface |
| `latency`, `average_latency`, `secret_key`, `seq_ack`, `gateway` | Same |
| `poll_event()`, `received_message()`, `received_binary_message()`, `initial_connection()`, `discover_ip()`, `load_secret_key()`, `resume()`, `identify()`, `select_protocol()`, `client_connect()`, `send_transition_ready()` | **Not present**: the websocket, heartbeats, IP discovery and DAVE handling live in Rust. Use `DiscordVoiceWebSocket.speak/send_*/close` and the `VoiceConnection` accessors instead |

## `discord.opus`

| Member | Status |
| --- | --- |
| `Encoder(application, bitrate, fec, expected_packet_loss, bandwidth, signal_type)` | Same keyword arguments and validation (including `ValueError` for bad values) |
| `Encoder.encode(pcm, frame_size=SAMPLES_PER_FRAME)` | Same |
| `Encoder.SAMPLING_RATE`, `CHANNELS`, `FRAME_LENGTH`, `SAMPLE_SIZE`, `SAMPLES_PER_FRAME`, `FRAME_SIZE` | Same values |
| `Encoder.set_bitrate`, `set_fec`, `set_expected_packet_loss_percent`, `set_bandwidth`, `set_signal_type` | Same |
| `Decoder()`, `Decoder.decode(packet)` | Same |
| `Decoder.decode_plc()` (ours) | Converts a lost packet; `discord.py` does the same through `opus_decode(None, ...)` |
| `OpusError` | Same name; carries a message instead of an integer `code` |
| `OpusNotLoaded` | Exists for compatibility and is never raised |
| `load_opus(name)`, `is_loaded()` | No-ops: `is_loaded()` always returns `True` |
| `OPUS_SILENCE` | Same bytes (`b'\xf8\xff\xfe'`) |
| `Decoder.packet_get_nb_frames`, `packet_get_nb_channels`, `packet_get_samples_per_frame`, `set_gain`, `set_volume`, `_set_gain` | **Not present** (receive-side helpers) |

## `discord.player`

| Member | Status |
| --- | --- |
| `AudioSource` (interface) | Same |
| `PCMAudio(stream)` | Same, including returning `b''` on a short read |
| `FFmpegPCMAudio(source, *, executable, pipe, stderr, before_options, options)` | Same arguments; implemented in process where possible, CLI otherwise. Adds `force_cli: bool = False` |
| `FFmpegOpusAudio(source, *, executable, pipe, stderr, before_options, options, codec, bitrate)` | Same arguments; `codec`/`bitrate` are accepted but ignored because libopus is always the encoder |
| `FFmpegOpusAudio.from_probe()` / `probe()` | Same shape; uses the native probe instead of running `ffprobe` |
| `PCMVolumeTransformer(original, volume)` | Same behaviour, implemented in Rust | 
| `AudioPlayer(source, client, *, after)` | Same class contract: a `threading.Thread` with `stop`, `pause`, `resume`, `is_playing`, `is_paused`, `set_source`, `send_silence`, `DELAY == 0.02` |
| `FFmpegAudio` | Present as a base class, but without the `_spawn_process`/stderr plumbing that `discord.py` exposes |

Differences worth knowing:

* **`FFmpegPCMAudio` fails fast for a missing local file** — `discord.py` only raises once the
  subprocess exits, which surfaces on the first `read()`. Here a non-existent path raises
  `FFmpegError` during construction (and `PathLike` sources are accepted).
* **`FFmpegOpusAudio` output is chosen by probing** — if the input already carries Opus
  packets they are passed through untouched; otherwise the input is decoded to PCM and encoded
  with libopus by the normal encoder, which is what `discord.py` achieves by asking FFmpeg to
  transcode.
* **`_check_process_returncode` waits for the child** so FFmpeg failures raise
  deterministically instead of depending on when `Popen` noticed the exit.
* `audioop` is gone from Python 3.13; `PCMVolumeTransformer` therefore scales samples in Rust.

## `discord.oggparse`

`OggError`, `OggPage` and `OggStream` are ported (MIT). `OggStream.read_packet()` is an
addition that skips the `OpusHead`/`OpusTags` header packets, so only real audio packets reach
Discord. This module is only used by the CLI fallback path.

## Exceptions

`VoiceError` is the base class for everything the native core raises. When `discord.py` is
installed:

```python
class ClientException(discord.errors.ClientException, VoiceError): ...
```

so both `except discord.ClientException` and `except discord_ext_voice.VoiceError` work.
`OpusNotLoaded` and `FFmpegProcessError` subclass `ClientException`, matching `discord.py`.

Native errors: `VoiceError`, `NotConnected`, `AlreadyConnected`, `HandshakeError`,
`ConnectionClosedError`, `ProtocolError`, `EncryptionError`, `DaveError`, `OpusError`,
`FFmpegError`.

## Additions

* `discord_ext_voice.probe(source)` — codec/geometry probe without decoding.
* `VoiceClient.native` and `VoiceConnection` — direct access to the transport, including
  `decrypt_rtp`, `decrypt_dave`, `encode`, `handle_dave_binary`, `wait_for_phase` and
  `voice_privacy_code`.
* `ws://host:port` endpoints are accepted, which makes local gateways and testing possible.
* `FFmpegPCMAudio(force_cli=True)` / `FFmpegOpusAudio(codec=..., bitrate=...)` knobs.
