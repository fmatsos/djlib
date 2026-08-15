"""Atomic import of human duplicate-review decisions (design §24).

Task 13 is a major review gate: this is where human curation decisions
become durable and irreversible-by-automation, so this module is
deliberately conservative -- see `.claude/rules/curation-persistence.md`.

## Validation order (design §24)

Every decision in a `decisions.json` file is validated in a strict,
read-only pass *before* any database write is attempted:

1. JSON Schema (`report/decision-schema.json`, reused unmodified from Task
   12 -- structural shape, the four allowed actions, `CHANGE_PREFERRED`
   requires a non-null `preferred_file_id`).
2. `schema_version == 1` (already implied by the schema's `const`, kept as
   an explicit, independently-readable check).
3. `report_id` matches `^rpt_` (already implied by the schema's `pattern`,
   likewise kept explicit).
4. `catalog_revision` matches a freshly recomputed
   `compute_catalog_revision(session)` -- see "The report_id gap" below.
5. Every `group_id` (and `preferred_file_id`, where present) actually
   exists.
6. Every referenced group's *current* status is still `REVIEW_REQUIRED` --
   a group already `CONFIRMED`/`REJECTED`/`DEFERRED` by an earlier import
   is stale for a second import attempt.
7. For `CHANGE_PREFERRED`, the named file is an actual (still-active)
   member of that group.

The whole file is rejected -- with zero database writes -- on the first
failure encountered in this order, across the whole batch: one bad decision
among several good ones rejects the entire import. There is no partial
apply and no `--force` (design §24, Milestone 1 constraint).

## The "no persisted report_id" gap -- a documented judgment call

Task 12's `ReportGenerator.generate()` computes `report_id` and
`catalog_revision` fresh on every call and writes them into
`manifest.json`/`index.html`, but never persists either to a database row --
there is no `Report`/`ReportRun` table to look a `report_id` up against
(and this task's file list deliberately budgets no new migration/table for
one; `.claude/rules/curation-persistence.md` explicitly says not to
introduce schema beyond what the current task's design section specifies).

So design §24's literal "reject an unknown/stale report_id" cannot be a real
row lookup here. The resolution used below: `compute_catalog_revision`
(Task 12 -- deterministic, already sensitive to the latest `ScanRun`'s
public_id, every `DuplicateGroup`'s `(public_id, status, confidence)`, and
`AppState.last_exported_curation_sequence`) is treated as the actual
staleness oracle. If the file's claimed `catalog_revision` does not
byte-for-byte match a freshly recomputed one, the whole import is rejected
as stale. This single check subsumes:

* "the catalog changed since the report was generated" -- any `djlib scan`,
  even a fully no-op rescan, inserts a new `ScanRun` row with a fresh
  `public_id`, and that public_id is itself hashed into the revision, so
  *any* rescan already invalidates a prior report's revision;
* "a group changed status/confidence since report generation" -- each
  group's own `public_id:status:confidence` triple is hashed in directly;
* "unknown/forged report_id", practically -- a real `report_id` is only
  ever handed out by `generate()` paired with its own freshly-computed
  `catalog_revision`; guessing a `report_id` from thin air would also
  require guessing a matching SHA-256-derived revision string, which is not
  feasible. `report_id`'s own `^rpt_` structural shape (already enforced by
  the JSON Schema, and re-checked explicitly below) is the only check this
  module performs on `report_id` in isolation.

## REJECT/DEFER durability -- confirmed, not assumed

`DuplicateStatus.REJECTED` and `DuplicateStatus.DEFERRED` are already
excluded from `DuplicateService._ANALYZABLE_STATUSES` (Task 10), so simply
setting a group's status to one of those on import durably prevents a later
`duplicates analyze`/`run` from silently reclassifying or re-merging it --
no extra code is needed here for that guarantee. See
`tests/integration/test_decision_import.py::
test_reject_decision_is_durable_against_a_later_automatic_run`, which
exercises this rather than assuming it.
"""

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import jsonschema
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from djlib.catalog.queries import active_track_for_file
from djlib.config import DjlibConfig
from djlib.db.enums import DecisionSource, DuplicateStatus
from djlib.db.models import CurationEvent, DuplicateGroup, DuplicateGroupMember, FileRecord
from djlib.duplicates.service import DuplicateService
from djlib.report.generator import compute_catalog_revision

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / 'report' / 'decision-schema.json'
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding='utf-8'))

# Only a group still awaiting human review may receive a decision. A group
# already CONFIRMED/REJECTED/DEFERRED by a prior import is stale for a
# second import attempt (design §24).
_RESOLVABLE_STATUSES = (DuplicateStatus.REVIEW_REQUIRED,)

_EVENT_TYPE_BY_DECISION = {
    'CONFIRM': 'DUPLICATE_GROUP_CONFIRMED',
    'CHANGE_PREFERRED': 'DUPLICATE_GROUP_PREFERRED_CHANGED',
    'REJECT': 'DUPLICATE_GROUP_REJECTED',
    'DEFER': 'DUPLICATE_GROUP_DEFERRED',
}


class DecisionImportError(Exception):
    """The whole decisions.json import was rejected; nothing was written."""


@dataclass(frozen=True)
class ImportSummary:
    report_id: str
    catalog_revision: str
    accepted: int
    group_ids: tuple[str, ...]


@dataclass(frozen=True)
class _ResolvedDecision:
    raw: dict
    group: DuplicateGroup
    preferred_file: FileRecord | None


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class DecisionImporter:
    def __init__(self, config: DjlibConfig, session: Session) -> None:
        self._config = config
        self._session = session
        self._duplicate_service = DuplicateService(config, session)

    def import_file(self, path: Path) -> ImportSummary:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise DecisionImportError(f'could not read decisions file {path}: {exc}') from exc

        self._validate_schema(data)
        self._validate_schema_version(data)
        self._validate_report_id(data)
        self._validate_catalog_revision(data)

        # Every decision is resolved (read-only) against the current
        # database state *before* any write happens -- the first invalid
        # decision anywhere in the batch aborts the whole import.
        resolved = [self._resolve_decision(entry) for entry in data['decisions']]

        try:
            for item in resolved:
                self._apply_decision(item)
        except Exception:
            self._session.rollback()
            raise
        self._session.commit()

        return ImportSummary(
            report_id=data['report_id'],
            catalog_revision=data['catalog_revision'],
            accepted=len(resolved),
            group_ids=tuple(item.group.public_id for item in resolved),
        )

    # -- validation (read-only; no write happens until every check passes) --

    def _validate_schema(self, data: dict) -> None:
        try:
            jsonschema.validate(instance=data, schema=_SCHEMA)
        except jsonschema.ValidationError as exc:
            raise DecisionImportError(
                f'decisions file failed schema validation: {exc.message}'
            ) from exc

    def _validate_schema_version(self, data: dict) -> None:
        if data.get('schema_version') != 1:
            raise DecisionImportError(
                f'unsupported schema_version {data.get("schema_version")!r} '
                '(only schema_version 1 is supported)'
            )

    def _validate_report_id(self, data: dict) -> None:
        if not str(data.get('report_id', '')).startswith('rpt_'):
            raise DecisionImportError(f'malformed report_id {data.get("report_id")!r}')

    def _validate_catalog_revision(self, data: dict) -> None:
        current = compute_catalog_revision(self._session)
        claimed = data['catalog_revision']
        if claimed != current:
            raise DecisionImportError(
                f'stale decisions file: catalog_revision {claimed!r} does not match the '
                f'current catalog_revision {current!r} -- the catalogue changed (a rescan, '
                'a duplicates analyze/run, or a prior decision import) since this report was '
                'generated; re-generate the report and re-review before importing '
                '(no --force in Milestone 1)'
            )

    def _resolve_decision(self, entry: dict) -> _ResolvedDecision:
        group_public_id = entry['group_id']
        group = self._session.execute(
            select(DuplicateGroup).where(DuplicateGroup.public_id == group_public_id)
        ).scalar_one_or_none()
        if group is None:
            raise DecisionImportError(f'unknown group_id {group_public_id!r}')

        preferred_file: FileRecord | None = None
        preferred_public_id = entry.get('preferred_file_id')
        if preferred_public_id is not None:
            preferred_file = self._session.execute(
                select(FileRecord).where(FileRecord.public_id == preferred_public_id)
            ).scalar_one_or_none()
            if preferred_file is None:
                raise DecisionImportError(f'unknown preferred_file_id {preferred_public_id!r}')

        if group.status not in _RESOLVABLE_STATUSES:
            raise DecisionImportError(
                f'group {group_public_id!r} is no longer REVIEW_REQUIRED (current status '
                f'{group.status.value}) -- it was already resolved by a prior import'
            )

        if entry['decision'] == 'CHANGE_PREFERRED':
            assert preferred_file is not None  # schema requires this for CHANGE_PREFERRED
            member_file_ids = self._member_file_ids(group.id)
            if preferred_file.id not in member_file_ids:
                raise DecisionImportError(
                    f'preferred_file_id {preferred_public_id!r} is not a member of group '
                    f'{group_public_id!r}'
                )
            if active_track_for_file(self._session, preferred_file.id) is None:
                raise DecisionImportError(
                    f'preferred_file_id {preferred_public_id!r} has no active track link and '
                    f'cannot be made preferred for group {group_public_id!r}'
                )

        if entry['decision'] == 'CONFIRM' and group.proposed_preferred_file_id is None:
            raise DecisionImportError(
                f'group {group_public_id!r} has no proposed preferred file to CONFIRM '
                '(analysis never proposed one -- re-run `duplicates analyze`)'
            )

        return _ResolvedDecision(raw=entry, group=group, preferred_file=preferred_file)

    def _member_file_ids(self, group_id: int) -> set[int]:
        return set(
            self._session.execute(
                select(DuplicateGroupMember.file_id).where(
                    DuplicateGroupMember.group_id == group_id
                )
            ).scalars()
        )

    # -- apply (only reached once every decision above has validated clean) --

    def _apply_decision(self, item: _ResolvedDecision) -> None:
        action = item.raw['decision']
        group = item.group

        if action == 'CONFIRM':
            preferred_file_id = group.proposed_preferred_file_id
            assert preferred_file_id is not None
            self._duplicate_service.consolidate_group(
                group, preferred_file_id, DecisionSource.HUMAN
            )
            group.status = DuplicateStatus.CONFIRMED
        elif action == 'CHANGE_PREFERRED':
            assert item.preferred_file is not None
            self._duplicate_service.consolidate_group(
                group, item.preferred_file.id, DecisionSource.HUMAN
            )
            group.status = DuplicateStatus.CONFIRMED
        elif action == 'REJECT':
            group.status = DuplicateStatus.REJECTED
            group.resolved_at = _now()
        elif action == 'DEFER':
            group.status = DuplicateStatus.DEFERRED
            group.resolved_at = _now()
        else:  # pragma: no cover -- unreachable: schema restricts to these four actions
            raise DecisionImportError(f'unsupported decision {action!r}')

        self._record_curation_event(item)

    def _record_curation_event(self, item: _ResolvedDecision) -> CurationEvent:
        next_sequence = (
            self._session.execute(select(func.max(CurationEvent.sequence))).scalar_one() or 0
        ) + 1
        event = CurationEvent(
            sequence=next_sequence,
            event_uuid=str(uuid.uuid4()),
            event_type=_EVENT_TYPE_BY_DECISION[item.raw['decision']],
            track_public_id=None,
            file_public_id=item.preferred_file.public_id if item.preferred_file else None,
            payload_json={
                'group_id': item.group.public_id,
                'decision': item.raw['decision'],
                'preferred_file_id': (
                    item.preferred_file.public_id if item.preferred_file else None
                ),
                'reviewed_at': item.raw['reviewed_at'],
            },
        )
        self._session.add(event)
        self._session.flush()
        return event
