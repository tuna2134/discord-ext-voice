#!/usr/bin/env python3
"""Build the PEP 503 simple index that GitHub Pages serves.

``pip install --extra-index-url <pages-url>/simple/ discord-ext-voice`` reads exactly this:

    site/
    ├── index.html                      # a landing page with the pip command
    └── simple/
        ├── index.html                  # the project list (PEP 503)
        └── discord-ext-voice/
            └── index.html              # that project's files, with sha256 hashes

The distributions themselves are *not* copied here: they are published as GitHub release assets
(a wheel is ~73MB, because maturin bundles the FFmpeg libraries into it, and a Pages deployment
replaces the whole site on every publish). Each link therefore points at a release asset, with the
``#sha256=`` fragment pip verifies against.

Because the site is replaced on every publish, ``--previous-url`` reads the index that is live now
and carries its links over, so publishing 0.2.0 does not drop 0.1.0 from the index. Only the small
HTML pages are fetched for that; the distributions are never downloaded here.

The project and version of each file are read from the archive itself (``.dist-info/METADATA`` or
``PKG-INFO``), not from the file name: an sdist is named ``<name>-<version>.tar.gz`` and the name
may contain ``-``, which makes the file name ambiguous.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import tarfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

#: The `href` of an anchor in a page this script wrote, with its optional hash fragment.
ANCHOR = re.compile(
    r'<a href="(?P<href>[^"#]+)(?:#sha256=(?P<sha256>[0-9a-f]+))?"'
    r'(?:\s+data-requires-python="(?P<requires>[^"]*)")?>(?P<text>[^<]*)</a>'
)


@dataclass(frozen=True)
class Link:
    """One distribution: where pip downloads it, and what it should check."""

    filename: str
    href: str
    sha256: str
    requires_python: str | None


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


def local_links(dist: Path, base_url: str) -> dict[str, list[Link]]:
    """The distributions in ``dist``, keyed by normalised project name."""
    projects: dict[str, list[Link]] = {}
    for path in sorted(dist.iterdir()):
        if not path.is_file() or not (path.suffix == '.whl' or path.name.endswith('.tar.gz')):
            continue
        name, _, requires = metadata(path)
        href = f'{base_url.rstrip("/")}/{urllib.parse.quote(path.name)}'
        projects.setdefault(normalize(name), []).append(
            Link(path.name, href, digest(path), requires)
        )
    return projects


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 (our own site)
        return response.read().decode('utf-8', 'replace')


def published_links(previous_url: str) -> dict[str, list[Link]]:
    """The links of the index that is live now, keyed by normalised project name."""
    if not previous_url:
        return {}

    try:
        root = fetch(previous_url)
    except Exception as exc:  # a first publish has nothing to carry over
        print(f'no previous index at {previous_url} ({exc}); starting from scratch')
        return {}

    projects: dict[str, list[Link]] = {}
    for anchor in ANCHOR.finditer(root):
        project_url = urllib.parse.urljoin(previous_url, anchor.group('href'))
        if not project_url.endswith('/'):
            continue
        project = urllib.parse.unquote(project_url.rstrip('/').rsplit('/', 1)[-1])

        try:
            page = fetch(project_url)
        except Exception as exc:
            print(f'skipping {project_url}: {exc}')
            continue

        links = projects.setdefault(project, [])
        for found in ANCHOR.finditer(page):
            filename = found.group('text').strip() or found.group('href').rsplit('/', 1)[-1]
            links.append(
                Link(
                    filename,
                    urllib.parse.urljoin(project_url, found.group('href')),
                    found.group('sha256') or '',
                    found.group('requires') or None,
                )
            )

    print(f'carried over {sum(len(v) for v in projects.values())} link(s) from {previous_url}')
    return projects


def write_pages(site: Path, projects: dict[str, list[Link]], url: str) -> None:
    """Write the index pages: one per project, plus the root list and a landing page."""
    simple = site / 'simple'
    simple.mkdir(parents=True, exist_ok=True)

    for project, links in sorted(projects.items()):
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
        # Newest last, which is the order the releases were published in.
        for link in links:
            fragment = f'#sha256={link.sha256}' if link.sha256 else ''
            attributes = (
                f' data-requires-python="{html.escape(link.requires_python)}"'
                if link.requires_python
                else ''
            )
            lines.append(
                f'    <a href="{html.escape(link.href)}{fragment}"{attributes}>'
                f'{html.escape(link.filename)}</a><br>'
            )
        lines += ['  </body>', '</html>', '']
        (simple / project / 'index.html').parent.mkdir(parents=True, exist_ok=True)
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
    (simple / 'index.html').write_text('\n'.join(lines), encoding='utf-8')

    command = f'pip install --extra-index-url {url.rstrip("/")}/simple/ discord-ext-voice'
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
                "    <p>A PEP 503 index, built from this repository's GitHub Actions runs. "
                'The distributions themselves are release assets.</p>',
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
        '--base-url',
        required=True,
        help='where the distributions are downloadable, e.g. a release download URL',
    )
    parser.add_argument(
        '--previous-url',
        default='',
        help='the simple index that is live now, whose links are carried over',
    )
    parser.add_argument('--url', default='', help='the public URL of the site, for the landing page')
    args = parser.parse_args()

    args.site.mkdir(parents=True, exist_ok=True)
    projects = published_links(args.previous_url)
    added = local_links(args.dist, args.base_url)
    for project, links in added.items():
        known = {link.filename for link in projects.setdefault(project, [])}
        projects[project].extend(link for link in links if link.filename not in known)

    write_pages(args.site, projects, args.url.rstrip('/') or 'the site')

    for project, links in sorted(projects.items()):
        print(f'{project}: {len(links)} file(s)')
    print(f'added {sum(len(v) for v in added.values())} from {args.dist}')


if __name__ == '__main__':
    main()
