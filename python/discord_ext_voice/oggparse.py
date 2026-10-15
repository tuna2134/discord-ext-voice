"""A minimal Ogg page reader, used only by the ``ffmpeg`` CLI fallback path.

Ported from ``discord.oggparse`` (MIT licensed, Copyright (c) 2015-present Rapptz) with one
addition: :meth:`OggStream.read_packet` skips the ``OpusHead`` and ``OpusTags`` header packets
so that only real audio packets ever reach Discord.
"""

from __future__ import annotations

import struct
from typing import IO, ClassVar, Generator, Optional, Tuple

from .errors import VoiceError

__all__ = ('OggError', 'OggPage', 'OggStream')


class OggError(VoiceError):
    """Raised for Ogg stream parsing errors."""


class OggPage:
    _header: ClassVar[struct.Struct] = struct.Struct('<xBQIIIB')

    flag: int
    gran_pos: int
    serial: int
    pagenum: int
    crc: int
    segnum: int
    segtable: bytes
    data: bytes

    def __init__(self, stream: IO[bytes]) -> None:
        try:
            header = stream.read(struct.calcsize(self._header.format))
            (
                self.flag,
                self.gran_pos,
                self.serial,
                self.pagenum,
                self.crc,
                self.segnum,
            ) = self._header.unpack(header)

            self.segtable = stream.read(self.segnum)
            bodylen = sum(struct.unpack('B' * self.segnum, self.segtable))
            self.data = stream.read(bodylen)
        except Exception:
            raise OggError('bad data stream') from None

    def iter_packets(self) -> Generator[Tuple[bytes, bool], None, None]:
        packetlen = offset = 0
        partial = True

        for seg in self.segtable:
            if seg == 255:
                packetlen += 255
                partial = True
            else:
                packetlen += seg
                yield self.data[offset : offset + packetlen], True
                offset += packetlen
                packetlen = 0
                partial = False

        if partial:
            yield self.data[offset:], False


class OggStream:
    """Iterates the packets of an Ogg bitstream."""

    #: Packets that carry codec setup rather than audio.
    HEADER_SIGNATURES = (b'OpusHead', b'OpusTags')

    def __init__(self, stream: IO[bytes]) -> None:
        self.stream: IO[bytes] = stream
        self._skipped_headers = 0

    def _next_page(self) -> Optional[OggPage]:
        head = self.stream.read(4)
        if head == b'OggS':
            return OggPage(self.stream)
        if not head:
            return None
        raise OggError(f'invalid header magic {head}')

    def _iter_pages(self) -> Generator[OggPage, None, None]:
        page = self._next_page()
        while page:
            yield page
            page = self._next_page()

    def iter_packets(self) -> Generator[bytes, None, None]:
        partial = b''
        for page in self._iter_pages():
            for data, complete in page.iter_packets():
                partial += data
                if complete:
                    yield partial
                    partial = b''

    def read_packet(self) -> Optional[bytes]:
        """Return the next audio packet, skipping the Opus headers.

        Returns ``None`` once the stream ends.
        """
        for packet in self.iter_packets():
            if packet.startswith(self.HEADER_SIGNATURES):
                self._skipped_headers += 1
                continue
            return packet
        return None
