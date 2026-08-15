import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from subprocess import CompletedProcess
from typing import Protocol


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str]) -> CompletedProcess[str]: ...


class SubprocessCommandRunner:
    def run(self, argv: Sequence[str]) -> CompletedProcess[str]:
        return subprocess.run(list(argv), check=False, capture_output=True, text=True)


class MetadataExtractionError(Exception):
    """Per-file metadata extraction failure; caller records it and continues scanning."""


@dataclass(frozen=True)
class RawMetadata:
    title: str | None
    artist: str | None
    album: str | None
    album_artist: str | None
    genre: str | None
    bpm: str | None
    key: str | None
    comment: str | None
    raw_json: dict


@dataclass(frozen=True)
class TechnicalMetadata:
    container_format: str | None
    codec: str | None
    bitrate: int | None
    sample_rate: int | None
    bit_depth: int | None
    channels: int | None
    duration_ms: int | None


@dataclass(frozen=True)
class ExtractedMetadata:
    raw: RawMetadata
    technical: TechnicalMetadata
