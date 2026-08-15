from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_EXTENSIONS = {
    '.mp3', '.flac', '.wav', '.aiff', '.aif', '.m4a', '.aac', '.ogg', '.opus',
}


@dataclass(frozen=True)
class DiscoveredFile:
    relative_path: str
    size_bytes: int
    mtime_ns: int


def discover_audio_files(root: Path) -> Iterator[DiscoveredFile]:
    if not root.is_dir():
        raise FileNotFoundError(f'music_root is not a directory: {root}')

    discovered = []
    for path in root.rglob('*'):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        stat = path.stat()
        discovered.append(
            DiscoveredFile(
                relative_path=path.relative_to(root).as_posix(),
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        )
    discovered.sort(key=lambda f: f.relative_path)
    return iter(discovered)
