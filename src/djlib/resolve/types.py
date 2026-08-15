from dataclasses import dataclass


@dataclass(frozen=True)
class FilenameMetadata:
    artist: str | None
    title: str | None
    version: str | None
    edition: str | None


@dataclass(frozen=True)
class ParsedTitle:
    title: str
    version: str | None
    edition: str | None


@dataclass(frozen=True)
class FeaturedArtistParse:
    primary: str
    featured_artists: tuple[str, ...]


@dataclass(frozen=True)
class RawIdentity:
    artist: str | None
    title: str | None


@dataclass(frozen=True)
class ResolvedField:
    value: str | None
    source: str


@dataclass(frozen=True)
class FeaturedArtistEntry:
    position: int
    name: str
    source: str


@dataclass(frozen=True)
class ResolvedMetadata:
    artist: ResolvedField
    title: ResolvedField
    version: ResolvedField
    edition: ResolvedField
    featured_artists: tuple[FeaturedArtistEntry, ...]
