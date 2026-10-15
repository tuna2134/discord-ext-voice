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
            f'connected to {voice.channel} '
            f'(mode={voice.mode}, dave={voice.native.dave_protocol_version})'
        )

    elif message.content == '!play':
        voice = message.guild.voice_client
        if voice is None:
            await message.reply('not connected')
            return
        voice.play(
            dev.FFmpegPCMAudio(FILE),
            after=lambda error: print(f'finished, error={error!r}'),
            bitrate=128,
        )

    elif message.content == '!leave':
        voice = message.guild.voice_client
        if voice is not None:
            await voice.disconnect()


if __name__ == '__main__':
    if TOKEN is None:
        raise SystemExit('set DISCORD_TOKEN to run this example')
    client.run(TOKEN)
