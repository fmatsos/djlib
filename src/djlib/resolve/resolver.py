from djlib.resolve.filename import parse_filename
from djlib.resolve.parser import parse_title_annotations, split_featured_artists
from djlib.resolve.types import (
    FeaturedArtistEntry,
    RawIdentity,
    ResolvedField,
    ResolvedMetadata,
)


def _valid(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _prefer(tag_value: str | None, filename_value: str | None) -> ResolvedField:
    valid_tag = _valid(tag_value)
    if valid_tag is not None:
        return ResolvedField(valid_tag, 'TAG')
    conservative_filename_value = _valid(filename_value)
    if conservative_filename_value is not None:
        return ResolvedField(conservative_filename_value, 'FILENAME')
    return ResolvedField(None, 'UNKNOWN')


class MetadataResolver:
    def resolve(self, file_name: str, raw: RawIdentity) -> ResolvedMetadata:
        filename_meta = parse_filename(file_name)

        tag_title = _valid(raw.title)
        tag_parsed = parse_title_annotations(tag_title) if tag_title is not None else None

        title_field = _prefer(
            tag_parsed.title if tag_parsed else None, filename_meta.title
        )
        version_field = _prefer(
            tag_parsed.version if tag_parsed else None, filename_meta.version
        )
        edition_field = _prefer(
            tag_parsed.edition if tag_parsed else None, filename_meta.edition
        )
        artist_field = _prefer(raw.artist, filename_meta.artist)

        artist_feat = (
            split_featured_artists(artist_field.value)
            if artist_field.value is not None
            else None
        )
        title_feat = (
            split_featured_artists(title_field.value)
            if title_field.value is not None
            else None
        )

        final_artist = artist_feat.primary if artist_feat is not None else artist_field.value
        final_title = title_feat.primary if title_feat is not None else title_field.value

        featured_artists: list[FeaturedArtistEntry] = []
        position = 0
        for name in artist_feat.featured_artists if artist_feat else ():
            featured_artists.append(FeaturedArtistEntry(position=position, name=name, source='ARTIST'))
            position += 1
        for name in title_feat.featured_artists if title_feat else ():
            featured_artists.append(FeaturedArtistEntry(position=position, name=name, source='TITLE'))
            position += 1

        return ResolvedMetadata(
            artist=ResolvedField(final_artist, artist_field.source),
            title=ResolvedField(final_title, title_field.source),
            version=version_field,
            edition=edition_field,
            featured_artists=tuple(featured_artists),
        )
