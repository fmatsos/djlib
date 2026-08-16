import csv
import io

from djlib.catalog.stats_export import StatsExportRow, write_stats_csv, write_stats_html


def test_write_stats_csv_header_and_row() -> None:
    stream = io.StringIO()
    write_stats_csv([StatsExportRow(category='files', metric='total', value='42')], stream)
    reader = csv.reader(io.StringIO(stream.getvalue()))
    rows = list(reader)
    assert rows[0] == ['category', 'metric', 'value']
    assert rows[1] == ['files', 'total', '42']


def test_write_stats_html_contains_table_and_title() -> None:
    stream = io.StringIO()
    write_stats_html(
        [StatsExportRow(category='files', metric='total', value='42')],
        generated_at='2026-08-16T00:00:00+00:00',
        stream=stream,
    )
    html = stream.getvalue()
    assert 'djlib stats' in html
    assert '<table' in html
    assert 'total' in html
