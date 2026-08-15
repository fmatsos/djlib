import json
from pathlib import Path

from djlib.metadata.types import CommandRunner, MetadataExtractionError, RawMetadata

EXIFTOOL_TAGS = (
    '-Title', '-Artist', '-Album', '-AlbumArtist', '-Genre', '-BPM', '-InitialKey', '-Comment',
)


class ExifToolClient:
    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def extract_raw(self, path: Path) -> RawMetadata:
        argv = ['exiftool', '-j', '-n', *EXIFTOOL_TAGS, str(path)]
        result = self._runner.run(argv)

        try:
            parsed = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise MetadataExtractionError(
                f'exiftool produced invalid JSON for {path}: {result.stderr.strip()}'
            ) from exc

        if not isinstance(parsed, list) or not parsed:
            raise MetadataExtractionError(f'exiftool produced no metadata for {path}')

        record = parsed[0]

        def field(key: str) -> str | None:
            value = record.get(key)
            return None if value is None else str(value)

        return RawMetadata(
            title=field('Title'),
            artist=field('Artist'),
            album=field('Album'),
            album_artist=field('AlbumArtist'),
            genre=field('Genre'),
            bpm=field('BPM'),
            key=field('InitialKey'),
            comment=field('Comment'),
            raw_json=record,
        )
