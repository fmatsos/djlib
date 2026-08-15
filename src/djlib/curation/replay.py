"""Curation journal replay (design §25, Task 15 -- the milestone's final
major review gate).

`/data/curation/events.jsonl` plus `/music` must be *sufficient* to
reconstruct all curated state after SQLite is lost or a full rebuild deletes
it (`.claude/rules/curation-persistence.md`). The obstacle: every file/track
gets a brand-new random `public_id` on every fresh scan (`ids.new_public_id`,
`scan/service.py`, `catalog/service.py::create_provisional_track`), so a
`public_id` recorded in an event is *not* a stable anchor across a rebuild --
only a file's `relative_path` under `music_root` is. `CurationEvent.payload_json`
therefore carries `relative_path` references (see `catalog/service.py`'s and
`curation/decisions.py`'s event-writing sites) that this module resolves
against the *freshly re-scanned* database, then re-applies the recorded
action, restoring the *original* `public_id` onto whatever fresh entity now
corresponds to it.

Overwriting a `Track`'s `public_id` here is deliberately the one place in the
codebase where that is correct, not a violation of "public IDs are immutable"
(`.claude/rules/curation-persistence.md`): that invariant is about a *live*
system never recycling or reassigning an ID that has already been referenced
externally. During a rebuild, the freshly-scanned `public_id` was never
referenced by anything outside this in-progress rebuild -- replay's entire
job is to reconstruct the exact prior state, ID included.

## Strict mapping -- never guess

If an event's `relative_path` reference no longer resolves to a real,
currently-active file/track (deleted from `music_root`, or multiple files
resolve to conflicting tracks in a way that cannot be disambiguated), the
*whole* replay is aborted with a `ReplayError` naming the offending event's
`sequence`/`event_uuid` and the reason -- there is no partial apply and no
best-effort fallback (`.claude/rules/curation-persistence.md`: "Replay must
fail loudly on an event it cannot safely map to a file identity -- never
guess among ambiguous candidates").

## Duplicate-decision events reuse `DuplicateService.consolidate_group`

A CONFIRM/CHANGE_PREFERRED/REJECT/DEFER event's `DuplicateGroup` does not
exist yet after a fresh scan (a plain `djlib scan` never runs duplicate
detection) -- replay recreates a minimal group (its own member-file rows)
purely as the vehicle `consolidate_group` needs, then restores the group's
original `public_id` on top. This deliberately skips `DecisionImporter`'s
whole staleness-validation pipeline (report ID, catalog revision, current
group status): replay is a more privileged code path than a live decision
import, trusting its own journal rather than re-validating against a report
that no longer exists post-rebuild.
"""

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from djlib.catalog.queries import active_track_for_file
from djlib.catalog.service import (
    EVENT_TYPE_TRACK_MERGE,
    EVENT_TYPE_TRACK_OVERRIDE_SET,
    EVENT_TYPE_TRACK_SPLIT,
    CatalogService,
)
from djlib.config import DjlibConfig
from djlib.db.enums import DecisionSource, DuplicateStatus, RelationshipType
from djlib.db.models import (
    AppState,
    CurationEvent,
    DuplicateGroup,
    DuplicateGroupMember,
    FileRecord,
    Track,
)
from djlib.duplicates.service import DuplicateService
from djlib.ids import new_public_id

# Must match `curation/decisions.py::_EVENT_TYPE_BY_DECISION`'s values
# byte-for-byte -- that module owns the canonical spelling, this is the
# reader side of the same contract.
_DUPLICATE_DECISION_EVENT_TYPES = frozenset(
    {
        'DUPLICATE_GROUP_CONFIRMED',
        'DUPLICATE_GROUP_PREFERRED_CHANGED',
        'DUPLICATE_GROUP_REJECTED',
        'DUPLICATE_GROUP_DEFERRED',
    }
)


class ReplayError(Exception):
    """An event could not be safely/unambiguously mapped -- replay was
    aborted and the whole attempt rolled back. Never raised for a merely
    "already applied" no-op; only for a genuinely ambiguous or missing
    reference.
    """


@dataclass(frozen=True)
class ReplaySummary:
    events_replayed: int
    overrides_applied: int
    merges_applied: int
    splits_applied: int
    duplicate_decisions_applied: int


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events: list[dict] = []
    with path.open('r', encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    events.sort(key=lambda event: event['sequence'])
    return events


class CurationReplay:
    def __init__(self, config: DjlibConfig, session: Session) -> None:
        self._config = config
        self._session = session
        self._catalog_service = CatalogService(session)
        self._duplicate_service = DuplicateService(config, session)

    def replay(self, path: Path) -> ReplaySummary:
        events = _read_events(path)
        counts = {'overrides': 0, 'merges': 0, 'splits': 0, 'decisions': 0}
        current_event: dict | None = None

        try:
            for event in events:
                current_event = event
                self._apply_one(event, counts)
            current_event = None
            if events:
                self._advance_watermark(max(event['sequence'] for event in events))
        except ReplayError:
            self._session.rollback()
            raise
        except Exception as exc:
            self._session.rollback()
            if current_event is None:
                raise ReplayError(f'replay failed after applying all events: {exc}') from exc
            raise ReplayError(
                f'replay failed at sequence={current_event.get("sequence")} '
                f'event_uuid={current_event.get("event_uuid")} '
                f'event_type={current_event.get("event_type")}: {exc}'
            ) from exc

        self._session.commit()
        return ReplaySummary(
            events_replayed=len(events),
            overrides_applied=counts['overrides'],
            merges_applied=counts['merges'],
            splits_applied=counts['splits'],
            duplicate_decisions_applied=counts['decisions'],
        )

    # -- dispatch ----------------------------------------------------------

    def _apply_one(self, event: dict, counts: dict[str, int]) -> None:
        max_id_before = self._session.execute(select(func.max(CurationEvent.id))).scalar() or 0

        event_type = event.get('event_type')
        if event_type == EVENT_TYPE_TRACK_OVERRIDE_SET:
            self._apply_override(event)
            counts['overrides'] += 1
        elif event_type == EVENT_TYPE_TRACK_MERGE:
            self._apply_merge(event)
            counts['merges'] += 1
        elif event_type == EVENT_TYPE_TRACK_SPLIT:
            self._apply_split(event)
            counts['splits'] += 1
        elif event_type in _DUPLICATE_DECISION_EVENT_TYPES:
            self._apply_duplicate_decision(event)
            counts['decisions'] += 1
        else:
            raise ReplayError(
                f'event {event.get("event_uuid")} (sequence {event.get("sequence")}) has '
                f'unknown event_type {event_type!r}; refusing to guess how to replay it'
            )

        self._adopt_event_identity(event, max_id_before)

    def _adopt_event_identity(self, event: dict, max_id_before: int) -> None:
        """Make the rebuilt DB's `curation_events` table an exact mirror of
        the journal being replayed: one row per replayed event, carrying
        that event's *original* `sequence`/`event_uuid`/payload -- whether
        applying it happened to insert a fresh `CurationEvent` row as a
        mechanical side effect (`set_override`/`merge_track_into`/
        `split_track` all write one) or was a pure state no-op (e.g. a
        duplicate decision whose constituent merge(s) were already replayed
        by their own, separate, earlier MERGE event(s) in the journal --
        `consolidate_group`'s own idempotency guard then skips calling
        `merge_track_into` again, so nothing gets mechanically inserted).

        Without this, the rebuilt DB's own sequence numbering would drift
        from the journal's (a no-op event contributes no row at all, while
        an inserted row gets a *fresh*, mechanically-assigned sequence/uuid
        instead of the historical one) -- breaking both `doctor`'s "watermark
        equals max(CurationEvent.sequence)" invariant and, worse, any *new*
        curation action after rebuild computing its own `next_sequence`
        against a table that no longer agrees with the journal on disk.

        In the ordinary case at most one row is ever mechanically inserted
        per replayed event, because every real `merge_track_into` call --
        including ones nested inside `consolidate_group` -- already writes
        its own `CurationEvent` (this module's whole retrofit), so a
        multi-member group's constituent merges are always their own,
        separate, earlier journal events by the time a decision event is
        replayed.
        """
        newly_inserted = list(
            self._session.execute(
                select(CurationEvent)
                .where(CurationEvent.id > max_id_before)
                .order_by(CurationEvent.id)
            ).scalars()
        )
        row = newly_inserted[-1] if newly_inserted else CurationEvent()
        if not newly_inserted:
            self._session.add(row)
        row.sequence = event['sequence']
        row.event_uuid = event['event_uuid']
        row.event_type = event['event_type']
        row.track_public_id = event.get('track_public_id')
        row.file_public_id = event.get('file_public_id')
        row.payload_json = event.get('payload')
        self._session.flush()

    # -- override ------------------------------------------------------------

    def _apply_override(self, event: dict) -> None:
        payload = event['payload']
        track = self._resolve_single_track(payload.get('track_relative_paths') or [], event)
        # Restore the historical public_id onto the freshly-rescanned track
        # *before* recording the override, so a track that only ever received
        # an override (never a merge/split) still comes back with its
        # original, stable identity after a rebuild -- the same restoration
        # `_apply_merge`/`_apply_split` already do for their own tracks.
        track.public_id = payload['track_public_id']
        self._session.flush()
        self._catalog_service.set_override(track.public_id, payload['field'], payload['value'])

    # -- merge -----------------------------------------------------------

    def _apply_merge(self, event: dict) -> None:
        payload = event['payload']
        target_track = self._resolve_single_track(
            payload['target_file_relative_paths'], event
        )
        source_track = self._resolve_single_track(
            payload['source_file_relative_paths'], event
        )
        if target_track.id == source_track.id:
            # Already consolidated by an earlier replayed event -- idempotent
            # no-op, matching `consolidate_group`'s own guard.
            return

        relationships: dict[int, RelationshipType] = {}
        for relative_path, relationship_value in payload.get('file_relationships', {}).items():
            file = self._file_by_relative_path(relative_path, event)
            relationships[file.id] = RelationshipType(relationship_value)

        self._catalog_service.merge_track_into(
            survivor=target_track,
            absorbed=source_track,
            relationships=relationships,
            decision_source=DecisionSource(payload['decision_source']),
        )
        # `merge_track_into` is deliberately silent on activation -- both of
        # its real callers (`merge_tracks`, `consolidate_group`) always
        # activate the survivor themselves afterward, so replay must too or
        # the rebuilt survivor would incorrectly stay PROVISIONAL.
        self._catalog_service.activate_track(target_track)
        target_track.public_id = payload['target_track_public_id']
        source_track.public_id = payload['source_track_public_id']
        self._session.flush()

    # -- split -------------------------------------------------------------

    def _apply_split(self, event: dict) -> None:
        payload = event['payload']
        moved_paths = payload['moved_file_relative_paths']
        remaining_paths = payload['remaining_file_relative_paths']
        source_track = self._resolve_single_track(list(moved_paths) + list(remaining_paths), event)
        moved_files = [self._file_by_relative_path(rp, event) for rp in moved_paths]

        new_track = self._catalog_service.split_track(
            source_track.public_id, file_public_ids=[file.public_id for file in moved_files]
        )
        new_track.public_id = payload['new_track_public_id']
        source_track.public_id = payload['source_track_public_id']
        self._session.flush()

    # -- duplicate decision (CONFIRM / CHANGE_PREFERRED / REJECT / DEFER) --

    def _apply_duplicate_decision(self, event: dict) -> None:
        payload = event['payload']
        member_paths = payload.get('member_file_relative_paths') or []
        if not member_paths:
            raise ReplayError(
                f'event {event["event_uuid"]}: duplicate decision has no '
                'member_file_relative_paths to resolve'
            )
        member_files = [self._file_by_relative_path(rp, event) for rp in member_paths]
        for file in member_files:
            if active_track_for_file(self._session, file.id) is None:
                raise ReplayError(
                    f'event {event["event_uuid"]}: file {file.relative_path!r} has no '
                    'active track; cannot replay this duplicate decision'
                )

        group = self._resolve_or_create_group([file.id for file in member_files])
        action = payload['decision']

        if action in ('CONFIRM', 'CHANGE_PREFERRED'):
            preferred_relative_path = payload.get('preferred_file_relative_path')
            if not preferred_relative_path:
                raise ReplayError(
                    f'event {event["event_uuid"]}: {action} has no '
                    'preferred_file_relative_path to resolve'
                )
            preferred_file = self._file_by_relative_path(preferred_relative_path, event)
            self._duplicate_service.consolidate_group(
                group, preferred_file.id, DecisionSource.HUMAN
            )
            # `consolidate_group` no-ops (skips its own `activate_track` call
            # entirely) when the group's members are *already* on one track --
            # exactly the common replay case, since each constituent merge
            # typically already replayed as its own, earlier MERGE event. The
            # survivor must still end up ACTIVE with this preferred file, so
            # replay asserts that outcome directly rather than depending on
            # `consolidate_group`'s idempotency shortcut to have done it.
            survivor = active_track_for_file(self._session, preferred_file.id)
            assert survivor is not None
            self._catalog_service.activate_track(survivor, preferred_file_id=preferred_file.id)
            group.status = DuplicateStatus.CONFIRMED
            group.resolved_at = _now()
        elif action == 'REJECT':
            group.status = DuplicateStatus.REJECTED
            group.resolved_at = _now()
        elif action == 'DEFER':
            group.status = DuplicateStatus.DEFERRED
            group.resolved_at = _now()
        else:
            raise ReplayError(
                f'event {event["event_uuid"]}: unsupported duplicate decision {action!r}'
            )

        group.public_id = payload['group_id']
        self._session.flush()

    def _resolve_or_create_group(self, member_file_ids: list[int]) -> DuplicateGroup:
        wanted = frozenset(member_file_ids)
        candidate_group_ids = set(
            self._session.execute(
                select(DuplicateGroupMember.group_id).where(
                    DuplicateGroupMember.file_id.in_(member_file_ids)
                )
            ).scalars()
        )
        for group_id in candidate_group_ids:
            members = frozenset(
                self._session.execute(
                    select(DuplicateGroupMember.file_id).where(
                        DuplicateGroupMember.group_id == group_id
                    )
                ).scalars()
            )
            if members == wanted:
                group = self._session.get(DuplicateGroup, group_id)
                assert group is not None
                return group

        group = DuplicateGroup(
            public_id=new_public_id('dup'), status=DuplicateStatus.DETECTED, matcher_version='replay'
        )
        self._session.add(group)
        self._session.flush()
        for file_id in member_file_ids:
            self._session.add(DuplicateGroupMember(group_id=group.id, file_id=file_id))
        self._session.flush()
        return group

    # -- shared path/track resolution --------------------------------------

    def _file_by_relative_path(self, relative_path: str, event: dict) -> FileRecord:
        file = self._session.execute(
            select(FileRecord).where(FileRecord.relative_path == relative_path)
        ).scalar_one_or_none()
        if file is None:
            raise ReplayError(
                f'event {event["event_uuid"]} (sequence {event["sequence"]}): no file at '
                f'relative_path {relative_path!r} -- missing from the freshly-scanned '
                'catalogue; refusing to guess'
            )
        return file

    def _resolve_single_track(self, relative_paths: list[str], event: dict) -> Track:
        if not relative_paths:
            raise ReplayError(
                f'event {event["event_uuid"]}: no relative_path references to resolve'
            )
        track_ids: set[int] = set()
        tracks: dict[int, Track] = {}
        for relative_path in relative_paths:
            file = self._file_by_relative_path(relative_path, event)
            track = active_track_for_file(self._session, file.id)
            if track is None:
                raise ReplayError(
                    f'event {event["event_uuid"]}: file {relative_path!r} has no active '
                    'track; cannot resolve'
                )
            track_ids.add(track.id)
            tracks[track.id] = track
        if len(track_ids) != 1:
            raise ReplayError(
                f'event {event["event_uuid"]}: relative_paths {relative_paths} resolve to '
                f'{len(track_ids)} distinct tracks; ambiguous, refusing to guess'
            )
        return next(iter(tracks.values()))

    def _advance_watermark(self, max_sequence_in_file: int) -> None:
        """After replay, the rebuilt DB's watermark must already agree with
        `check_curation_sequence`'s invariant (`doctor.py`) -- these events
        came *from* the durable journal, so nothing about them is "pending
        export". `current_max` also covers any `CurationEvent` rows replay
        itself inserted as a mechanical side effect of reusing
        `CatalogService`/`DuplicateService` methods (e.g. a MERGE event
        replayed via `consolidate_group` also writes its own fresh
        `TRACK_MERGE` event) -- those aren't a second, un-exported layer of
        curation either.
        """
        current_max = self._session.execute(select(func.max(CurationEvent.sequence))).scalar() or 0
        watermark = max(max_sequence_in_file, current_max)
        app_state = self._session.execute(
            select(AppState).order_by(AppState.id).limit(1)
        ).scalar_one_or_none()
        if app_state is None:
            app_state = AppState(last_exported_curation_sequence=watermark)
            self._session.add(app_state)
        else:
            app_state.last_exported_curation_sequence = watermark
        self._session.flush()
