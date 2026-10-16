#!/usr/bin/env python3
"""Build a PEP 503 simple index for GitHub Pages from built distributions.

The layout it writes is what ``pip install --extra-index-url <url>/simple/`` expects:

    site/
    ├── index.html                      # a landing page with the pip command
    └── simple/
        ├── index.html                  # the project list (PEP 503)
        └── discord-ext-voice/
            ├── index.html              # that project's files, with sha256 hashes
            ├── discord_ext_voice-0.1.0-cp313-...whl
            └── discord_ext_voice-0.1.0.tar.gz

The project and version of every file are read from the archive itself (``.dist-info/METADATA``
or ``PKG-INFO``), not from the file name: an sdist is named ``<name>-<version>.tar.gz`` and the
name may contain ``-``, which makes the file name ambiguous.

``--previous-url`` merges what is already published into the new tree. A Pages deployment replaces
the whole site, so without it publishing 0.2.0 would silently drop 0.1.0 from the index.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import shutil
import tarfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

#: Matches the `href` of a link in an index page this script wrote.
HREF = re.compile(r'href="([^"]+)"')


def normalize(name: str) -> str:
    """PEP 503 name normalisation: lowercase, with runs of ``-``, ``_`` and ``.`` collapsed."""
    return re.sub(r'[-_.]+', '-', name).lower()


def metadata(path: Path) -> tuple[str, str, str | None]:
    """The ``(name, version, requires-python)`` a distribution advertises about itself."""
    if path.suffix == '.whl':
        with zipfile.ZipFile(path) as archive:
            member = next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))
            text = archive.read(member).decode('utf-8', 'replace')
    elif path.name.endswith('.tar.gz'):
        with tarfile.open(path, 'r:gz') as archive:
            member = next(m for m in archive.getmembers() if m.name.endswith('PKG-INFO'))
            extracted = archive.extractfile(member)
            assert extracted is not None
            text = extracted.read().decode('utf-8', 'replace')
    else:
        raise ValueError(f'{path.name} is neither a wheel nor an sdist')

    fields: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            break  # the RFC 822 headers end at the first blank line
        key, _, value = line.partition(':')
        fields[key.strip().lower()] = value.strip()
    return fields['name'], fields['version'], fields.get('requires-python') or None


def digest(path: Path) -> str:
    """The SHA-256 of a file, as pip's ``#sha256=`` fragment wants it."""
    accumulator = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            accumulator.update(chunk)
    return accumulator.hexdigest()


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 (our own site)
        return response.read().decode('utf-8', 'replace')


def hrefs(page: str) -> list[str]:
    return HREF.findall(page)


def merge_previous(previous_url: str, site: Path) -> int:
    """Copy the already-published files into the new tree, returning how many were fetched."""
    if not previous_url:
        return 0

    try:
        root = fetch(previous_url)
    except Exception as exc:  # a first publish has nothing to merge
        print(f'no previous index at {previous_url} ({exc}); starting from scratch')
        return 0

    fetched = 0
    for link in hrefs(root):
        project_url = urllib.parse.urljoin(previous_url, link)
        if not project_url.endswith('/'):
            continue
        directory = site / 'simple' / urllib.parse.unquote(project_url.rstrip('/').rsplit('/', 1)[-1])
        try:
            page = fetch(project_url)
        except Exception as exc:
            print(f'skipping {project_url}: {exc}')
            continue

        for file_link in hrefs(page):
            name = Path(urllib.parse.unquote(file_link.split('#', 1)[0])).name
            if not name or name == 'index.html':
                continue
            target = directory / name
            if target.exists():
                continue
            try:
                with urllib.request.urlopen(  # noqa: S310 (our own site)
                    urllib.parse.urljoin(project_url, file_link), timeout=120
                ) as response:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(response.read())
            except Exception as exc:
                print(f'unable to fetch {file_link}: {exc}')
                continue
            fetched += 1

    print(f'merged {fetched} file(s) from {previous_url}')
    return fetched


def place(dist: Path, site: Path) -> int:
    """Copy the built distributions into their project directories, returning how many."""
    placed = 0
    for path in sorted(dist.iterdir()):
        if not path.is_file() or not (path.suffix == '.whl' or path.name.endswith('.tar.gz')):
            continue
        name, _, _ = metadata(path)
        target = site / 'simple' / normalize(name) / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        placed += 1
    return placed


def write_pages(site: Path) -> dict[str, int]:
    """Write every index page from the files that are in the tree, returning per-project counts."""
    simple = site / 'simple'
    projects: dict[str, list[tuple[Path, str, str | None]]] = {}

    for path in sorted(simple.rglob('*')):
        if not path.is_file() or path.name == 'index.html':
            continue
        name, version, requires = metadata(path)
        directory = simple / normalize(name)
        if path.parent != directory:  # a file that was merged into the wrong place
            directory.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(directory / path.name))
            path = directory / path.name
        projects.setdefault(normalize(name), []).append((path, version, requires))

    for project, files in sorted(projects.items()):
        lines = [
            '<!DOCTYPE html>',
            '<html lang="en">',
            '  <head>',
            '    <meta charset="utf-8">',
            f'    <title>Links for {html.escape(project)}</title>',
            '  </head>',
            '  <body>',
            f'    <h1>Links for {html.escape(project)}</h1>',
        ]
        for path, _, requires in sorted(files, key=lambda item: item[0].name):
            attributes = f' data-requires-python="{html.escape(requires)}"' if requires else ''
            lines.append(
                f'    <a href="{html.escape(path.name)}#sha256={digest(path)}"{attributes}>'
                f'{html.escape(path.name)}</a><br>'
            )
        lines += ['  </body>', '</html>', '']
        (simple / project / 'index.html').write_text('\n'.join(lines), encoding='utf-8')

    lines = [
        '<!DOCTYPE html>',
        '<html lang="en">',
        '  <head>',
        '    <meta charset="utf-8">',
        '    <title>Simple index</title>',
        '  </head>',
        '  <body>',
    ]
    for project in sorted(projects):
        lines.append(f'    <a href="{html.escape(project)}/">{html.escape(project)}</a><br>')
    lines += ['  </body>', '</html>', '']
    simple.mkdir(parents=True, exist_ok=True)
    (simple / 'index.html').write_text('\n'.join(lines), encoding='utf-8')

    return {project: len(files) for project, files in projects.items()}


def write_landing(site: Path, url: str) -> None:
    """A small page at the site root, so the URL is useful when opened in a browser."""
    command = f'pip install --extra-index-url {url}/simple/ discord-ext-voice'
    (site / 'index.html').write_text(
        '\n'.join(
            [
                '<!DOCTYPE html>',
                '<html lang="en">',
                '  <head>',
                '    <meta charset="utf-8">',
                '    <title>discord-ext-voice package index</title>',
                '  </head>',
                '  <body>',
                '    <h1>discord-ext-voice package index</h1>',
                '    <p>A PEP 503 index, built from this repository\'s GitHub Actions runs.</p>',
                f'    <p>Index: <a href="simple/">{html.escape(url)}/simple/</a></p>',
                f'    <pre>{html.escape(command)}</pre>',
                '  </body>',
                '</html>',
                '',
            ]
        ),
        encoding='utf-8',
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', type=Path, default=Path('dist'), help='built distributions')
    parser.add_argument('--site', type=Path, default=Path('site'), help='the tree to write')
    parser.add_argument(
        '--previous-url',
        default='',
        help='the already-published simple index, merged into the new tree',
    )
    parser.add_argument('--url', default='', help='the public URL, for the landing page')
    args = parser.parse_args()

    args.site.mkdir(parents=True, exist_ok=True)
    placed = place(args.dist, args.site)
    merged = merge_previous(args.previous_url, args.site)
    projects = write_pages(args.site)
    write_landing(args.site, args.url.rstrip('/') or 'the site')

    print(f'placed {placed} distribution(s), merged {merged} file(s)')
    for project, count in projects.items():
        print(f'  {project}: {count} file(s)')


if __name__ == '__main__':
    main()
