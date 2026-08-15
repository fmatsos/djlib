import re
from pathlib import Path

from djlib.resolve.parser import parse_title_annotations
from djlib.resolve.types import FilenameMetadata

_DASH_CHARS = '‐‑‒–—―−'
_TRACK_PREFIX_RE = re.compile(r'^\d{1,3}\s*[-.]\s*')
_SEPARATOR_RE = re.compile(r'\s+-\s+')

_AMBIGUOUS = FilenameMetadata(artist=None, title=None, version=None, edition=None)


def parse_filename(name: str) -> FilenameMetadata:
    stem = Path(name).stem
    for dash in _DASH_CHARS:
        stem = stem.replace(dash, '-')
    stem = _TRACK_PREFIX_RE.sub('', stem.strip(), count=1)

    parts = _SEPARATOR_RE.split(stem)
    if len(parts) != 2:
        return _AMBIGUOUS

    artist, title_with_annotations = (part.strip() for part in parts)
    if not artist or not title_with_annotations:
        return _AMBIGUOUS

    parsed_title = parse_title_annotations(title_with_annotations)
    return FilenameMetadata(
        artist=artist,
        title=parsed_title.title,
        version=parsed_title.version,
        edition=parsed_title.edition,
    )
