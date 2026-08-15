import shutil
from pathlib import Path

from djlib.metadata.exiftool import ExifToolClient
from djlib.metadata.ffprobe import FfprobeClient
from djlib.metadata.types import ExtractedMetadata, SubprocessCommandRunner

REQUIRED_EXECUTABLES = ('exiftool', 'ffprobe')


class RequiredExecutableMissingError(RuntimeError):
    def __init__(self, executable: str) -> None:
        super().__init__(f'required executable not found on PATH: {executable}')
        self.executable = executable


def ensure_required_executables() -> None:
    for executable in REQUIRED_EXECUTABLES:
        if shutil.which(executable) is None:
            raise RequiredExecutableMissingError(executable)


class MetadataExtractor:
    def __init__(self, exiftool: ExifToolClient, ffprobe: FfprobeClient) -> None:
        self._exiftool = exiftool
        self._ffprobe = ffprobe

    @classmethod
    def create(cls) -> 'MetadataExtractor':
        runner = SubprocessCommandRunner()
        return cls(ExifToolClient(runner), FfprobeClient(runner))

    def extract(self, path: Path) -> ExtractedMetadata:
        raw = self._exiftool.extract_raw(path)
        technical = self._ffprobe.extract_technical(path)
        return ExtractedMetadata(raw=raw, technical=technical)
