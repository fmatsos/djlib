from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class DjlibConfig:
    music_root: Path
    data_root: Path

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
        return cls(
            Path(paths.get('music_root', base.music_root)),
            Path(paths.get('data_root', base.data_root)),
        )
