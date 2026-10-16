# This example requires the 'message_content' privileged intent to function.
# This code comes from the discord.py examples repository, and is licensed under the MIT license.

import asyncio
from os import getenv
import logging

import discord
import yt_dlp as youtube_dl

from discord.ext import commands
import discord_ext_voice as dev

# Suppress noise about console usage from errors.
#
# Current yt-dlp calls this with arguments (`bug_reports_message(before='')`), so the patch has
# to accept them: `lambda: ''` (what discord.py's copy of this example uses) raises
# `TypeError: <lambda>() got an unexpected keyword argument 'before'` while the YouTube
# extractor is being imported.
youtube_dl.utils.bug_reports_message = lambda *args, **kwargs: ''

logging.basicConfig(level=logging.INFO)

ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',  # bind to ipv4 since ipv6 addresses cause issues sometimes
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)


class YTDLSource(dev.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)

        self.data = data

        self.title = data.get('title')
        self.url = data.get('url')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))

        if 'entries' in data:
            # take first item from a playlist
            data = data['entries'][0]

        filename = data['url'] if stream else ytdl.prepare_filename(data)
        # No FFmpeg CLI arguments here: this package decodes in process and never runs the
        # `ffmpeg` binary, so `options='-vn'` (what discord.py's copy passes) is a `TypeError`.
        # The native reader takes the input's audio stream, so there is nothing to suppress.
        #
        # `stream=True` hands the CDN URL straight to `reqwest`, and such a URL is usually bound
        # to the client that asked for it: pass the extractor's own headers along, or the CDN
        # answers 403. A downloaded file is read from disk and needs none.
        headers = data.get('http_headers') if stream else None
        return cls(dev.FFmpegPCMAudio(filename, headers=headers), data=data)


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def join(self, ctx, *, channel: discord.VoiceChannel):
        """Joins a voice channel"""

        if ctx.voice_client is not None:
            return await ctx.voice_client.move_to(channel)

        await channel.connect(cls=dev.VoiceClient)

    @commands.command()
    async def play(self, ctx, *, query):
        """Plays a file from the local filesystem"""

        source = dev.PCMVolumeTransformer(dev.FFmpegPCMAudio(query))
        ctx.voice_client.play(source, after=lambda e: print(f'Player error: {e}') if e else None)

        await ctx.send(f'Now playing: {query}')

    @commands.command()
    async def yt(self, ctx, *, url):
        """Plays from a url (almost anything youtube_dl supports)"""

        async with ctx.typing():
            player = await YTDLSource.from_url(url, loop=self.bot.loop)
            ctx.voice_client.play(player, after=lambda e: print(f'Player error: {e}') if e else None)

        await ctx.send(f'Now playing: {player.title}')

    @commands.command()
    async def stream(self, ctx, *, url):
        """Streams from a url (same as yt, but doesn't predownload)"""

        async with ctx.typing():
            player = await YTDLSource.from_url(url, loop=self.bot.loop, stream=True)
            ctx.voice_client.play(player, after=lambda e: print(f'Player error: {e}') if e else None)

        await ctx.send(f'Now playing: {player.title}')

    @commands.command()
    async def volume(self, ctx, volume: int):
        """Changes the player's volume"""

        if ctx.voice_client is None:
            return await ctx.send('Not connected to a voice channel.')

        ctx.voice_client.source.volume = volume / 100
        await ctx.send(f'Changed volume to {volume}%')

    @commands.command()
    async def stop(self, ctx):
        """Stops and disconnects the bot from voice"""

        await ctx.voice_client.disconnect()

    @play.before_invoke
    @yt.before_invoke
    @stream.before_invoke
    async def ensure_voice(self, ctx):
        if ctx.voice_client is None:
            if ctx.author.voice:
                # `cls=dev.VoiceClient` is what makes this package's backend the one that
                # connects: discord.py's own `VoiceClient` would be used otherwise, and that one
                # needs PyNaCl and the Python `davey` bindings.
                await ctx.author.voice.channel.connect(cls=dev.VoiceClient)
            else:
                await ctx.send('You are not connected to a voice channel.')
                raise commands.CommandError('Author not connected to a voice channel.')
        elif ctx.voice_client.is_playing():
            ctx.voice_client.stop()


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned_or('!'),
    description='Relatively simple music bot example',
    intents=intents,
)


@bot.event
async def on_ready():
    # Tell the type checker that User is filled up at this point
    assert bot.user is not None

    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')


async def main():
    async with bot:
        await bot.add_cog(Music(bot))
        await bot.start(getenv('DISCORD_TOKEN'))


if __name__ == '__main__':
    if getenv('DISCORD_TOKEN') is None:
        raise SystemExit('set DISCORD_TOKEN to run this example')
    asyncio.run(main())