import re

from djlib.resolve.types import FeaturedArtistParse, ParsedTitle

# Version markers are checked as a *suffix* of a parenthetical/bracketed group so that a
# remixer/artist name preceding the marker (e.g. "Nalin & Kane Remix") is preserved verbatim
# in the resulting version string rather than collapsed to the bare marker word.
_VERSION_MARKER_RE = re.compile(
    r'(?i)\b(remix|mix|edit|rework|bootleg|mashup|vip|dub|instrumental|live)\s*$'
)
# Edition is a distinct axis from version (design §10.5): remastering/reissue metadata must
# never be written into resolved_version, and a version marker must never leak into edition.
_EDITION_MARKER_RE = re.compile(
    r'(?i)\b(remaster(?:ed)?|anniversary edition|deluxe edition|reissue)\s*$'
)
_ANNOTATION_GROUP_RE = re.compile(r'[(\[]([^()\[\]]+)[)\]]')

_FEATURE_MARKER_RE = re.compile(r'(?i)\bfeaturing\b|\b(?:feat|ft)\.?(?!\w)')
_FEATURE_SPLIT_RE = re.compile(r'(?i)\s*(?:,|&|\band\b)\s*')


def parse_title_annotations(title: str) -> ParsedTitle:
    version: str | None = None
    edition: str | None = None

    def _classify(match: re.Match[str]) -> str:
        nonlocal version, edition
        content = match.group(1).strip()
        if edition is None and _EDITION_MARKER_RE.search(content):
            edition = content
            return ''
        if version is None and _VERSION_MARKER_RE.search(content):
            version = content
            return ''
        return match.group(0)

    remaining = _ANNOTATION_GROUP_RE.sub(_classify, title)
    remaining = re.sub(r'\s+', ' ', remaining).strip()
    return ParsedTitle(title=remaining, version=version, edition=edition)


def split_featured_artists(artist: str) -> FeaturedArtistParse:
    match = _FEATURE_MARKER_RE.search(artist)
    if match is None:
        return FeaturedArtistParse(primary=artist.strip(), featured_artists=())

    head = artist[: match.start()].strip().rstrip('([').strip()
    tail = artist[match.end() :].strip()
    if tail[:1] in (':', '-'):
        tail = tail[1:].strip()
    tail = tail.rstrip(')]').strip()

    names = tuple(name.strip() for name in _FEATURE_SPLIT_RE.split(tail) if name.strip())
    return FeaturedArtistParse(primary=head, featured_artists=names)
