from dataclasses import dataclass, field
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class DurationToleranceThresholds:
    """Duration-bucket tolerances for candidate blocking (design §14, Task 7).

    Defaults match the values `duplicates/similarity.py::duration_tolerance_ms`
    hardcoded before Task 10 wired this through config -- this is now the one
    canonical source of those three numbers.
    """

    short_ms: int = 2000
    medium_ms: int = 3000
    long_ms: int = 5000


@dataclass(frozen=True)
class ChromaprintThresholds:
    """Chromaprint similarity thresholds for pairwise classification (design §18, Task 10).

    `auto_equivalent` is the floor for AUDIO_EQUIVALENT (only reachable when
    metadata is also compatible); `review_floor` is the floor below which a
    pair is DIFFERENT rather than PROBABLE/CONFLICT. Starting values only --
    subject to real-library calibration via `djlib duplicates calibrate`
    (design §18), never treated as immutable truth.
    """

    auto_equivalent: float = 0.985
    review_floor: float = 0.93


@dataclass(frozen=True)
class DuplicatesConfig:
    duration: DurationToleranceThresholds = field(default_factory=DurationToleranceThresholds)
    chromaprint: ChromaprintThresholds = field(default_factory=ChromaprintThresholds)


@dataclass(frozen=True)
class DjlibConfig:
    music_root: Path
    data_root: Path
    duplicates: DuplicatesConfig = field(default_factory=DuplicatesConfig)

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_root / 'catalog.sqlite'}"

    @classmethod
    def defaults(cls) -> 'DjlibConfig':
        return cls(Path('/music'), Path('/data'))

    @classmethod
    def load(cls, path: Path | None = None) -> 'DjlibConfig':
        base = cls.defaults()
        if path is None:
            return base
        raw = tomllib.loads(path.read_text(encoding='utf-8'))
        paths = raw.get('paths', {})
        duplicates_raw = raw.get('duplicates', {})
        duration_raw = duplicates_raw.get('duration', {})
        chromaprint_raw = duplicates_raw.get('chromaprint', {})
        return cls(
            Path(paths.get('music_root', base.music_root)),
            Path(paths.get('data_root', base.data_root)),
            duplicates=DuplicatesConfig(
                duration=DurationToleranceThresholds(
                    short_ms=duration_raw.get('short_ms', base.duplicates.duration.short_ms),
                    medium_ms=duration_raw.get('medium_ms', base.duplicates.duration.medium_ms),
                    long_ms=duration_raw.get('long_ms', base.duplicates.duration.long_ms),
                ),
                chromaprint=ChromaprintThresholds(
                    auto_equivalent=chromaprint_raw.get(
                        'auto_equivalent', base.duplicates.chromaprint.auto_equivalent
                    ),
                    review_floor=chromaprint_raw.get(
                        'review_floor', base.duplicates.chromaprint.review_floor
                    ),
                ),
            ),
        )
