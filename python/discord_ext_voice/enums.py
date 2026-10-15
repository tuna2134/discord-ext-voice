"""Small enum surface shared with ``discord.py``.

When ``discord.py`` is installed its enums are reused so that ``isinstance`` checks and
``discord.SpeakingState`` comparisons keep working; otherwise equivalent definitions are
provided so the library can be used standalone.
"""

from __future__ import annotations

from enum import IntEnum

__all__ = ('SpeakingState',)

try:  # pragma: no cover - depends on the environment
    from discord.enums import SpeakingState as SpeakingState
except Exception:  # pragma: no cover

    class SpeakingState(IntEnum):
        """A flag set describing how a speaker is sending audio."""

        none = 0
        """Indicates that the user is not speaking."""

        voice = 1
        """Indicates that the user is speaking through their microphone."""

        soundshare = 2
        """Indicates that the user is sending audio through sound share."""

        priority = 4
        """Indicates that the user is a priority speaker."""

        def __str__(self) -> str:
            return self.name

        def __int__(self) -> int:
            return self.value
