"""Exponential backoff, ported from :mod:`discord.backoff` (MIT, Copyright (c) Rapptz)."""

from __future__ import annotations

import random
import time
from typing import Callable, Literal, TypeVar, Union, overload

__all__ = ('ExponentialBackoff',)

T = TypeVar('T', bool, Literal[True], Literal[False])


class ExponentialBackoff:
    """An implementation of the exponential backoff algorithm.

    Once instantiated, :meth:`delay` returns the next interval to wait before retrying a
    connection or transmission. The maximum delay increases exponentially with each retry up
    to ``2 ** 10 * base``, and is reset if no retry happens for ``2 ** 11 * base`` seconds.

    Parameters
    ----------
    base: :class:`int`
        The base delay in seconds.
    integral: :class:`bool`
        Whether whole periods of ``base`` are desirable.
    """

    def __init__(self, base: int = 1, *, integral: bool = False):
        self._base: int = base

        self._exp: int = 0
        self._max: int = 10
        self._reset_time: int = base * 2**11
        self._last_invocation: float = time.monotonic()

        rand = random.Random()
        rand.seed()
        self._randfunc: Callable[..., Union[int, float]] = rand.randrange if integral else rand.uniform

    def delay(self) -> float:
        """Compute the next delay and advance the exponent."""
        invocation = time.monotonic()
        interval = invocation - self._last_invocation
        self._last_invocation = invocation

        if interval > self._reset_time:
            self._exp = 0

        self._exp = min(self._exp + 1, self._max)
        return float(self._randfunc(0, self._base * 2**self._exp))
