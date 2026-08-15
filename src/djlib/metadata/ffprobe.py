import json
from pathlib import Path
from typing import Any

from djlib.metadata.types import CommandRunner, MetadataExtractionError, TechnicalMetadata


class FfprobeClient:
    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def extract_technical(self, path: Path) -> TechnicalMetadata:
        argv = [
            'ffprobe', '-v', 'error', '-print_format', 'json',
            '-show_format', '-show_streams', str(path),
        ]
        result = self._runner.run(argv)

        try:
            parsed = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise MetadataExtractionError(
                f'ffprobe produced invalid JSON for {path}: {result.stderr.strip()}'
            ) from exc

        streams = parsed.get('streams') or []
        audio_streams = [s for s in streams if s.get('codec_type') == 'audio']
        if not audio_streams:
            raise MetadataExtractionError(
                f'ffprobe found no audio stream for {path}: {result.stderr.strip()}'
            )
        stream = audio_streams[0]
        fmt = parsed.get('format') or {}

        duration_raw = fmt.get('duration') or stream.get('duration')
        if duration_raw is None:
            raise MetadataExtractionError(f'ffprobe reported no duration for {path}')
        duration_ms = round(float(duration_raw) * 1000)

        return TechnicalMetadata(
            container_format=fmt.get('format_name'),
            codec=stream.get('codec_name'),
            bitrate=_optional_int(fmt.get('bit_rate') or stream.get('bit_rate')),
            sample_rate=_optional_int(stream.get('sample_rate')),
            bit_depth=_optional_int(stream.get('bits_per_sample') or stream.get('bits_per_raw_sample')),
            channels=_optional_int(stream.get('channels')),
            duration_ms=duration_ms,
        )


def _optional_int(value: Any) -> int | None:
    if value in (None, 0, '0'):
        return None
    return int(float(value))
