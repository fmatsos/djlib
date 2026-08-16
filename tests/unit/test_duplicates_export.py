import csv
import io

from djlib.duplicates.export import (
    DUPLICATE_EXPORT_FIELDS,
    DuplicateExportRow,
    write_duplicates_csv,
    write_duplicates_html,
)


def _row(**overrides: object) -> DuplicateExportRow:
    defaults = dict(
        group_public_id='dup_1',
        status='REVIEW_REQUIRED',
        confidence=0.5,
        matcher_version='1',
        file_count=2,
        member_paths='a.flac | b.mp3',
        proposed_preferred_path='a.flac',
        reasons='at least one PROBABLE pair',
    )
    defaults.update(overrides)
    return DuplicateExportRow(**defaults)


def test_write_duplicates_csv_header_and_row() -> None:
    stream = io.StringIO()
    write_duplicates_csv([_row()], stream)
    reader = csv.reader(io.StringIO(stream.getvalue()))
    rows = list(reader)
    assert rows[0] == list(DUPLICATE_EXPORT_FIELDS)
    assert rows[1][0] == 'dup_1'
    assert rows[1][5] == 'a.flac | b.mp3'


def test_write_duplicates_html_contains_table_and_title() -> None:
    stream = io.StringIO()
    write_duplicates_html([_row()], generated_at='2026-08-16T00:00:00+00:00', stream=stream)
    html = stream.getvalue()
    assert 'djlib duplicate groups' in html
    assert '<table' in html
    assert 'dup_1' in html
