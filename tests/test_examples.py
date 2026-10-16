"""The examples have to keep working against the dependencies they use.

``examples/ytdl.py`` patches a yt-dlp helper and hands yt-dlp's output to this package's audio
sources, so it breaks when yt-dlp changes that helper's signature — which is exactly what
happened with ``bug_reports_message(before=...)`` — or when this package stops accepting an
argument the example passes. Both examples are importable: their entry points are guarded by
``if __name__ == '__main__'``.
"""

from __future__ import annotations

import importlib
import importlib.util
import pathlib

import pytest

pytest.importorskip('yt_dlp')

import yt_dlp  # noqa: E402

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / 'examples'


def load(name: str):
    """Import one of the examples as a module."""
    path = EXAMPLES / f'{name}.py'
    spec = importlib.util.spec_from_file_location(f'example_{name}', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_ytdl_example_imports():
    module = load('ytdl')
    assert module.ytdl is not None
    assert callable(module.YTDLSource.from_url)


def test_the_ytdl_patch_accepts_what_yt_dlp_passes():
    load('ytdl')

    # The example replaces this helper to silence the "please report this issue" noise. yt-dlp
    # calls it with arguments (a positional one, and `before=`/`after=`), so a patch that takes
    # none breaks the YouTube extractor's import.
    assert yt_dlp.utils.bug_reports_message() == ''
    assert yt_dlp.utils.bug_reports_message(', ') == ''
    assert yt_dlp.utils.bug_reports_message(before='') == ''

    # This import is what used to raise `TypeError: <lambda>() got an unexpected keyword
    # argument 'before'`.
    importlib.import_module('yt_dlp.extractor.youtube')


def test_the_bot_example_imports():
    module = load('bot')
    assert module.client is not None
    assert module.TOKEN is None or isinstance(module.TOKEN, str)
