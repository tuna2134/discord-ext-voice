"""A minimal Discord bot that joins a voice channel and plays a file.

Run it with a token in the ``DISCORD_TOKEN`` environment variable::

    DISCORD_TOKEN=... python examples/bot.py song.mp3

The only thing that differs from a stock ``discord.py`` bot is ``cls=dev.VoiceClient``
(and the audio source classes coming from ``discord_ext_voice``).
"""

from __future__ import annotations

import os
import sys
import logging

import discord

import discord_ext_voice as dev

logging.basicConfig(level=logging.DEBUG)
TOKEN = os.environ.get('DISCORD_TOKEN')
FILE = sys.argv[1] if len(sys.argv) > 1 else 'song.mp3'

intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True

client = discord.Client(intents=intents)


@client.event
async def on_ready() -> None:
    print(f'logged in as {client.user}')


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.content:
        return

    if message.content == '!join':
        if message.author.voice is None:
            await message.reply('join a voice channel first')
            return
        voice = await message.author.voice.channel.connect(cls=dev.VoiceClient)
        print(
            f'!join: connected to {voice.channel} (mode={voice.mode}, '
            f'dave={voice.native.dave_protocol_version}) '
            f'registered={message.guild.voice_client is voice} '
            f'all={client.voice_clients}'
        )

    elif message.content == '!play':
        voice = message.guild.voice_client
        print(f'!play: file={FILE!r} guild.voice_client={voice!r} all={client.voice_clients}')
        if voice is None:
            await message.reply('not connected')
            return

        try:
            source = dev.FFmpegPCMAudio(FILE)
        except Exception as exc:
            print(f'!play: could not open {FILE!r}: {exc!r}')
            await message.reply(f'could not open {FILE}: {exc}')
            return

        print(f'!play: source={source!r}')
        voice.play(
            source,
            after=lambda error: print(f'finished, error={error!r}'),
            bitrate=128,
        )
        print(f'!play: playing={voice.is_playing()} source={voice.source!r}')

    elif message.content == '!status':
        # Handy when audio does not arrive: it separates "the player is running" from "the
        # transport is encrypting", which is what the log alone makes hard to tell apart.
        voice = message.guild.voice_client
        if voice is None:
            await message.reply('not connected')
            return
        native = voice.native
        await message.reply(
            f'connected={voice.is_connected()} playing={voice.is_playing()} '
            f'phase={native.phase} source={voice.source!r}\n'
            f'mode={native.mode} ssrc={native.ssrc} latency={native.latency:.3f}s '
            f'dropped={native.dropped_packets}\n'
            f'dave: version={native.dave_protocol_version} '
            f'session={native.has_dave_session()} can_encrypt={native.can_encrypt}'
        )

    elif message.content == '!leave':
        voice = message.guild.voice_client
        print(f'!leave: guild.voice_client={voice!r} all={client.voice_clients}')
        if voice is None:
            await message.reply('not connected')
            return

        try:
            await voice.disconnect()
        except Exception as exc:
            print(f'!leave: disconnect failed: {exc!r}')
            await message.reply(f'disconnect failed: {exc}')
            return

        print(
            f'!leave: done connected={voice.is_connected()} all={client.voice_clients}'
        )


if __name__ == '__main__':
    if TOKEN is None:
        raise SystemExit('set DISCORD_TOKEN to run this example')
    client.run(TOKEN)
