"""Small helpers that keep the package usable on every supported Python version.

``requires-python`` is ``>=3.8`` to match ``discord.py``, so ``str.removeprefix`` (added in
3.9) cannot be used directly.
"""

from __future__ import annotations

__all__ = ('strip_scheme',)

_SCHEMES = ('wss://', 'ws://')


def strip_scheme(endpoint: str) -> str:
    """Return `endpoint` without a leading ``wss://`` or ``ws://`` scheme."""
    for scheme in _SCHEMES:
        if endpoint.startswith(scheme):
            return endpoint[len(scheme) :]
    return endpoint
