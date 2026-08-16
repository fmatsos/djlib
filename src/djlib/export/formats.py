import datetime as dt
from enum import StrEnum
from pathlib import Path


class ExportFormat(StrEnum):
    CSV = 'csv'
    HTML = 'html'


def default_export_path(
    data_root: Path, name: str, fmt: ExportFormat, now: dt.datetime
) -> Path:
    return data_root / 'exports' / f'{name}-{now:%Y%m%d-%H%M%S}.{fmt.value}'
