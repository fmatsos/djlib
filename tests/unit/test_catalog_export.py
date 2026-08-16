import csv
import io

from djlib.catalog.export import CATALOG_EXPORT_FIELDS, CatalogExportRow, write_catalog_csv, write_catalog_html


def _row(**overrides: object) -> CatalogExportRow:
    defaults = dict(
        file_public_id='fil_1',
        relative_path='Artist - Title.flac',
        is_present=True,
        track_public_id='trk_1',
        track_status='ACTIVE',
        artist='Artist',
        title='Title',
        version=None,
        edition=None,
        featured_artists='',
        genre='House',
        bpm='128',
        key='Am',
        duration_ms=180000,
        codec='flac',
        bitrate=None,
        sample_rate=44100,
        size_bytes=12345,
        quality_score=95.5,
    )
    defaults.update(overrides)
    return CatalogExportRow(**defaults)


def test_write_catalog_csv_header_and_row() -> None:
    stream = io.StringIO()
    write_catalog_csv([_row()], stream)
    reader = csv.reader(io.StringIO(stream.getvalue()))
    rows = list(reader)
    assert rows[0] == list(CATALOG_EXPORT_FIELDS)
    assert rows[1][0] == 'fil_1'
    assert rows[1][1] == 'Artist - Title.flac'


def test_write_catalog_html_contains_table_and_title() -> None:
    stream = io.StringIO()
    write_catalog_html([_row()], generated_at='2026-08-16T00:00:00+00:00', stream=stream)
    html = stream.getvalue()
    assert 'djlib catalogue' in html
    assert '<table' in html
    assert 'Artist - Title.flac' in html
