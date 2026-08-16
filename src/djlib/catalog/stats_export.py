"""Read-only `djlib stats export` data collection and writers -- combines
`compute_catalog_stats`/`DuplicateService.stats()` (already surfaced by
`catalog stats`/`duplicates stats` on the terminal) into one flat, long-format
row list suitable for CSV/HTML.
"""

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy.orm import Session

from djlib.catalog.queries import compute_catalog_stats
from djlib.config import DjlibConfig
from djlib.db.enums import DuplicateStatus, PairClassification, TrackStatus
from djlib.duplicates.service import DuplicateService
from djlib.export.html_table import Column, render_table_html

STATS_EXPORT_FIELDS = ('category', 'metric', 'value')

_HTML_COLUMNS = (
    Column('category', 'Category'),
    Column('metric', 'Metric'),
    Column('value', 'Value'),
)


@dataclass(frozen=True)
class StatsExportRow:
    category: str
    metric: str
    value: str


def collect_stats_export_rows(session: Session, config: DjlibConfig) -> list[StatsExportRow]:
    catalog_stats = compute_catalog_stats(session)
    duplicate_stats = DuplicateService(config, session).stats()

    rows = [
        StatsExportRow('files', 'total', str(catalog_stats.files_total)),
        StatsExportRow('files', 'present', str(catalog_stats.files_present)),
        StatsExportRow('files', 'missing', str(catalog_stats.files_missing)),
    ]
    for status in TrackStatus:
        rows.append(
            StatsExportRow(
                'tracks',
                status.value.lower(),
                str(catalog_stats.track_status_counts.get(status.value, 0)),
            )
        )
    rows.extend(
        [
            StatsExportRow('scans', 'runs_total', str(catalog_stats.scan_runs_total)),
            StatsExportRow(
                'scans', 'failed_files_total', str(catalog_stats.scan_files_failed_total)
            ),
            StatsExportRow(
                'scans', 'latest_scan_public_id', catalog_stats.latest_scan_public_id or '-'
            ),
            StatsExportRow(
                'scans', 'latest_scan_status', catalog_stats.latest_scan_status or '-'
            ),
        ]
    )
    for status in DuplicateStatus:
        rows.append(
            StatsExportRow(
                'duplicate_groups',
                status.value.lower(),
                str(duplicate_stats.group_status_counts.get(status.value, 0)),
            )
        )
    for classification in PairClassification:
        rows.append(
            StatsExportRow(
                'duplicate_pairs',
                classification.value.lower(),
                str(duplicate_stats.pair_classification_counts.get(classification.value, 0)),
            )
        )
    return rows


def write_stats_csv(rows: Iterable[StatsExportRow], stream: TextIO) -> None:
    writer = csv.writer(stream)
    writer.writerow(STATS_EXPORT_FIELDS)
    for row in rows:
        writer.writerow([row.category, row.metric, row.value])


def write_stats_html(rows: Iterable[StatsExportRow], generated_at: str, stream: TextIO) -> None:
    table_rows = [{'category': row.category, 'metric': row.metric, 'value': row.value} for row in rows]
    stream.write(
        render_table_html(
            title='djlib stats',
            generated_at=generated_at,
            columns=list(_HTML_COLUMNS),
            rows=table_rows,
        )
    )
