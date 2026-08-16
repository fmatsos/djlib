"""Read-only `djlib duplicates export` data collection and writers -- flat
duplicate-group rows, distinct from `report/generator.py`'s stateful
interactive review page (see that module's docstring): no decisions, no
manifest, just one row per group for a CSV/HTML dump.
"""

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from djlib.db.models import DuplicateGroup, DuplicateGroupMember, DuplicatePairEvidence, FileRecord
from djlib.duplicates.rationale import group_reasons, preferred_choice_from_persisted
from djlib.export.html_table import Column, render_table_html

DUPLICATE_EXPORT_FIELDS = (
    'group_public_id',
    'status',
    'confidence',
    'matcher_version',
    'file_count',
    'member_paths',
    'proposed_preferred_path',
    'reasons',
)

_HTML_COLUMNS = (
    Column('group_public_id', 'Group ID'),
    Column('status', 'Status'),
    Column('confidence', 'Confidence'),
    Column('matcher_version', 'Matcher version'),
    Column('file_count', 'Files'),
    Column('member_paths', 'Member paths'),
    Column('proposed_preferred_path', 'Proposed preferred'),
    Column('reasons', 'Reasons'),
)


@dataclass(frozen=True)
class DuplicateExportRow:
    group_public_id: str
    status: str
    confidence: float | None
    matcher_version: str | None
    file_count: int
    member_paths: str
    proposed_preferred_path: str | None
    reasons: str


def collect_duplicate_export_rows(session: Session) -> list[DuplicateExportRow]:
    groups = list(
        session.execute(select(DuplicateGroup).order_by(DuplicateGroup.id)).scalars()
    )

    rows: list[DuplicateExportRow] = []
    for group in groups:
        file_ids = list(
            session.execute(
                select(DuplicateGroupMember.file_id).where(
                    DuplicateGroupMember.group_id == group.id
                )
            ).scalars()
        )
        files = {
            f.id: f
            for f in session.execute(select(FileRecord).where(FileRecord.id.in_(file_ids))).scalars()
        }
        pair_rows = list(
            session.execute(
                select(DuplicatePairEvidence).where(DuplicatePairEvidence.group_id == group.id)
            ).scalars()
        )

        preferred_choice = preferred_choice_from_persisted(session, files.values())
        proposed_preferred_path = (
            files[preferred_choice.file_id].relative_path
            if preferred_choice is not None and preferred_choice.file_id in files
            else (
                files[group.proposed_preferred_file_id].relative_path
                if group.proposed_preferred_file_id in files
                else None
            )
        )

        rows.append(
            DuplicateExportRow(
                group_public_id=group.public_id,
                status=group.status.value,
                confidence=group.confidence,
                matcher_version=group.matcher_version,
                file_count=len(files),
                member_paths=' | '.join(sorted(f.relative_path for f in files.values())),
                proposed_preferred_path=proposed_preferred_path,
                reasons='; '.join(group_reasons(pair_rows)),
            )
        )
    return rows


def write_duplicates_csv(rows: Iterable[DuplicateExportRow], stream: TextIO) -> None:
    writer = csv.writer(stream)
    writer.writerow(DUPLICATE_EXPORT_FIELDS)
    for row in rows:
        writer.writerow([getattr(row, field) for field in DUPLICATE_EXPORT_FIELDS])


def write_duplicates_html(
    rows: Iterable[DuplicateExportRow], generated_at: str, stream: TextIO
) -> None:
    table_rows = [
        {field: getattr(row, field) for field in DUPLICATE_EXPORT_FIELDS} for row in rows
    ]
    stream.write(
        render_table_html(
            title='djlib duplicate groups',
            generated_at=generated_at,
            columns=list(_HTML_COLUMNS),
            rows=table_rows,
        )
    )
