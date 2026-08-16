import datetime as dt
from pathlib import Path

from djlib.export.formats import ExportFormat, default_export_path


def test_export_format_values() -> None:
    assert ExportFormat.CSV == 'csv'
    assert ExportFormat.HTML == 'html'


def test_default_export_path_csv() -> None:
    now = dt.datetime(2026, 8, 16, 12, 30, 45, tzinfo=dt.UTC)
    path = default_export_path(Path('/data'), 'catalog', ExportFormat.CSV, now)
    assert path == Path('/data/exports/catalog-20260816-123045.csv')


def test_default_export_path_html() -> None:
    now = dt.datetime(2026, 8, 16, 12, 30, 45, tzinfo=dt.UTC)
    path = default_export_path(Path('/data'), 'duplicates', ExportFormat.HTML, now)
    assert path == Path('/data/exports/duplicates-20260816-123045.html')
