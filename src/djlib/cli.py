import json
import os
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import typer
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn
from sqlalchemy import select
from sqlalchemy.orm import Session

from djlib.catalog.queries import (
    active_files_for_track,
    active_track_for_file,
    compute_catalog_stats,
    find_file_by_public_id,
    find_track_by_public_id,
)
from djlib.catalog.service import CatalogService, EffectiveIdentity
from djlib.config import DjlibConfig
from djlib.curation.decisions import DecisionImportError, DecisionImporter
from djlib.curation.journal import CurationJournal
from djlib.curation.rebuild import RebuildError, RebuildService
from djlib.db.engine import create_engine_for_config
from djlib.db.enums import PairClassification
from djlib.db.models import (
    CurationEvent,
    DuplicateGroup,
    DuplicateGroupMember,
    DuplicatePairEvidence,
    FileQualityAnalysis,
    FileRecord,
    OperationRun,
    Track,
)
from djlib.db.session import session_factory
from djlib.doctor import Doctor
from djlib.duplicates.calibration import (
    collect_calibration_rows,
    write_calibration_csv,
    write_calibration_json,
)
from djlib.duplicates.chromaprint import ChromaprintService
from djlib.duplicates.hashing import HashService
from djlib.duplicates.preferred import PreferredCandidate, PreferredChoice, PreferredFileSelector
from djlib.duplicates.quality import QualityResult
from djlib.duplicates.service import DuplicateService
from djlib.logging import configure_logging
from djlib.metadata.types import SubprocessCommandRunner
from djlib.progress import ProgressReporter
from djlib.report.generator import ReportGenerator
from djlib.runs import operation_run
from djlib.scan.service import ScanService

# Mirrors `duplicates/groups.py::DuplicateGroupBuilder`'s own inconsistency
# set (design Sec.16) -- kept as a small local copy here rather than importing
# a private name across module boundaries, the same judgment call
# `report/generator.py::_group_reasons` already made for the same reason.
_INCONSISTENT_CLASSIFICATIONS = frozenset(
    {PairClassification.DIFFERENT, PairClassification.CONFLICT}
)

app = typer.Typer(no_args_is_help=True, help='Local DJ-library catalogue and deduplication tool.')
catalog_app = typer.Typer(no_args_is_help=True, help='Inspect the catalogue.')
duplicates_app = typer.Typer(no_args_is_help=True, help='Duplicate-detection utilities.')
runs_app = typer.Typer(no_args_is_help=True, help='Inspect operation runs.')
app.add_typer(catalog_app, name='catalog')
app.add_typer(duplicates_app, name='duplicates')
app.add_typer(runs_app, name='runs')


@app.callback()
def main(
    verbose: int = typer.Option(
        0, '-v', count=True, help='Increase console log verbosity (-v info, -vv debug).'
    ),
    log_level: str | None = typer.Option(
        None, '--log-level', help='Explicit console log level (overrides -v/-vv).'
    ),
) -> None:
    """Local DJ-library catalogue and deduplication tool."""
    configure_logging(_load_config(), verbosity=verbose, log_level=log_level)


def _load_config() -> DjlibConfig:
    config_path = os.environ.get('DJLIB_CONFIG')
    return DjlibConfig.load(Path(config_path)) if config_path else DjlibConfig.defaults()


@contextmanager
def _progress_bar() -> Iterator[ProgressReporter]:
    """A single reusable progress bar shared across a command's stages.

    Reports a stage name and a current/total count -- never a file path
    (`scan`/`duplicates run`/`rebuild` can touch thousands of files, and a
    scrolling wall of full paths is more noise than signal in a terminal).
    """
    with Progress(
        TextColumn('[progress.description]{task.description}'),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    ) as bar:
        task_id = bar.add_task('starting...', total=None)

        def report(stage: str, current: int, total: int) -> None:
            bar.update(task_id, description=stage, completed=current, total=total or None)

        yield report


@app.command()
def scan(full: bool = typer.Option(False, '--full', help='Force a full rescan.')) -> None:
    """Scan music_root and incrementally update the catalogue."""
    config = _load_config()
    engine = create_engine_for_config(config)
    session_maker = session_factory(engine)
    with operation_run(session_maker, 'scan', 'scan') as run, _progress_bar() as progress:
        service = ScanService(config, session_maker)
        summary = service.scan(full=full, progress=progress)
        run.summary = {
            'scan_public_id': summary.public_id,
            'status': summary.status.value,
            'files_seen': summary.files_seen,
            'files_new': summary.files_new,
            'files_changed': summary.files_changed,
            'files_unchanged': summary.files_unchanged,
            'files_missing': summary.files_missing,
            'files_failed': summary.files_failed,
        }
    typer.echo(
        f'scan {summary.public_id} (run {run.public_id}): seen={summary.files_seen} '
        f'new={summary.files_new} changed={summary.files_changed} '
        f'unchanged={summary.files_unchanged} missing={summary.files_missing} '
        f'failed={summary.files_failed}'
    )


@app.command()
def rebuild() -> None:
    """Rebuild catalog.sqlite from music_root plus the curation journal (design §25).

    Sequence: health-check music_root -> back up catalog.sqlite -> fresh
    migrate -> full scan -> replay events.jsonl -> run doctor's invariants.
    Aborts before touching anything if music_root is missing/misconfigured.
    The pre-rebuild backup is retained even on success. Never modifies
    music_root (`.claude/rules/source-read-only.md`).
    """
    config = _load_config()
    try:
        with _progress_bar() as progress:
            summary = RebuildService(config).rebuild(progress=progress)
    except RebuildError as exc:
        typer.echo(f'rebuild: aborted: {exc}', err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        f'rebuild: backup={summary.backup_path or "-"} '
        f'scan(seen={summary.scan_summary.files_seen} new={summary.scan_summary.files_new} '
        f'failed={summary.scan_summary.files_failed}) '
        f'replay(events={summary.replay_summary.events_replayed} '
        f'overrides={summary.replay_summary.overrides_applied} '
        f'merges={summary.replay_summary.merges_applied} '
        f'splits={summary.replay_summary.splits_applied} '
        f'decisions={summary.replay_summary.duplicate_decisions_applied} '
        f'auto_preferred_files={summary.replay_summary.automatic_preferred_file_applied}) '
        f'invariants_ok={summary.invariants_ok}'
    )
    if not summary.invariants_ok:
        typer.echo(f'rebuild: failed invariant checks: {list(summary.failed_checks)}', err=True)
        raise typer.Exit(code=1)


@app.command()
def doctor(
    repair_journal: bool = typer.Option(
        False, '--repair-journal', help='Export pending curation events before reporting.'
    ),
) -> None:
    """Run djlib health checks (design §27)."""
    config = _load_config()
    engine = create_engine_for_config(config)
    session_maker = session_factory(engine)
    report = Doctor(config, session_maker).run(repair_journal=repair_journal)
    for check in report.checks:
        typer.echo(f'[{check.status.value}] {check.name}: {check.message}')
    if not report.ok:
        raise typer.Exit(code=1)


@runs_app.command('show')
def runs_show(
    run_id: str = typer.Argument(..., help='An operation run public ID (scan_/dup_/report_/import_...).'),
) -> None:
    """Show a single `OperationRun`'s command/status/timestamps/summary."""
    config = _load_config()
    engine = create_engine_for_config(config)
    with session_factory(engine)() as session:
        run = session.execute(
            select(OperationRun).where(OperationRun.public_id == run_id)
        ).scalar_one_or_none()
    if run is None:
        raise typer.BadParameter(f'no operation run with id {run_id}')

    typer.echo(f'run: {run.public_id} command={run.command!r} status={run.status.value}')
    typer.echo(
        f'started_at={run.started_at.isoformat()} '
        f'ended_at={run.ended_at.isoformat() if run.ended_at else "-"}'
    )
    if run.summary_json is not None:
        typer.echo(f'summary: {json.dumps(run.summary_json, sort_keys=True)}')
    if run.error_summary:
        typer.echo(f'error: {run.error_summary}')


@duplicates_app.command('calibrate')
def duplicates_calibrate(
    output: Path | None = typer.Option(
        None, '--output', help='Write rows to this file instead of stdout.'
    ),
    as_json: bool = typer.Option(False, '--json', help='Emit JSON instead of CSV.'),
) -> None:
    """Export pairwise duplicate-candidate evidence for human threshold calibration.

    For each conservatively-blocked candidate pair (Task 7), computes the
    BLAKE3 binary hash of both files and -- only when the hashes differ --
    their Chromaprint fingerprints and similarity. This command only reports:
    it never writes `duplicate_groups`/`duplicate_pair_evidence` and never
    rewrites any threshold or config value (design §18). A human samples this
    output -- exact binary duplicates, same-version different encodings,
    remixes, radio/extended edits, bootlegs, plausible false positives -- to
    decide on Task 10's classification thresholds.
    """
    config = _load_config()
    engine = create_engine_for_config(config)
    hash_service = HashService(config.music_root)
    chromaprint_service = ChromaprintService(config.music_root, SubprocessCommandRunner())

    with session_factory(engine)() as session:
        rows = collect_calibration_rows(session, hash_service, chromaprint_service)
        session.commit()

    if output is not None:
        with output.open('w', newline='') as handle:
            if as_json:
                write_calibration_json(rows, handle)
            else:
                write_calibration_csv(rows, handle)
    else:
        if as_json:
            write_calibration_json(rows, sys.stdout)
        else:
            write_calibration_csv(rows, sys.stdout)

    typer.echo(f'calibrate: pairs={len(rows)}', err=True)


@duplicates_app.command('detect')
def duplicates_detect() -> None:
    """Conservative metadata blocking only (design §14) -- no BLAKE3, no
    Chromaprint, no quality analysis. Persists `DETECTED` duplicate groups.
    """
    config = _load_config()
    engine = create_engine_for_config(config)
    session_maker = session_factory(engine)
    with operation_run(session_maker, 'duplicates detect', 'dup') as run:
        with session_maker() as session:
            groups = DuplicateService(config, session).detect()
        run.summary = {'groups': groups}
    typer.echo(f'detect (run {run.public_id}): groups={groups}')


@duplicates_app.command('analyze')
def duplicates_analyze() -> None:
    """Targeted BLAKE3/Chromaprint/quality evidence, classification and
    preferred-file proposal for already-detected groups (design §17-21).
    Never touches CONFIRMED/REJECTED/DEFERRED (human-decided) groups.
    """
    config = _load_config()
    engine = create_engine_for_config(config)
    session_maker = session_factory(engine)
    with operation_run(session_maker, 'duplicates analyze', 'dup') as run:
        with session_maker() as session:
            groups = DuplicateService(config, session).analyze()
        run.summary = {'groups_analyzed': groups}
    typer.echo(f'analyze (run {run.public_id}): groups_analyzed={groups}')


@duplicates_app.command('run')
def duplicates_run() -> None:
    """detect + analyze + safe automatic consolidation of AUTO_CONFIRMED
    groups only. Does not generate an HTML report (see `duplicates report`,
    a later task).
    """
    config = _load_config()
    engine = create_engine_for_config(config)
    session_maker = session_factory(engine)
    with operation_run(session_maker, 'duplicates run', 'dup') as run, _progress_bar() as progress:
        with session_maker() as session:
            summary = DuplicateService(config, session).run(progress=progress)
        run.summary = {
            'groups_detected': summary.groups_detected,
            'groups_analyzed': summary.groups_analyzed,
            'groups_consolidated': summary.groups_consolidated,
        }
    typer.echo(
        f'run (run {run.public_id}): detected={summary.groups_detected} '
        f'analyzed={summary.groups_analyzed} consolidated={summary.groups_consolidated}'
    )


@duplicates_app.command('stats')
def duplicates_stats() -> None:
    """Counts of duplicate groups by status and pairwise evidence by classification."""
    config = _load_config()
    engine = create_engine_for_config(config)
    with session_factory(engine)() as session:
        stats = DuplicateService(config, session).stats()
    group_line = ' '.join(
        f'{name.lower()}={count}' for name, count in stats.group_status_counts.items()
    )
    pair_line = ' '.join(
        f'{name.lower()}={count}' for name, count in stats.pair_classification_counts.items()
    )
    typer.echo(f'groups: {group_line}')
    typer.echo(f'pairs: {pair_line}')


@duplicates_app.command('report')
def duplicates_report() -> None:
    """Generate a static, serverless HTML duplicate-review report (design
    §22) under `/data/reports/duplicates-review-YYYYMMDD-HHMMSS/`. Read-only:
    never mutates `duplicate_groups`/`files`/etc, and performs no persistence
    of its own -- review decisions are made and exported entirely in the
    browser (design §23; see `djlib duplicates import-decisions`, Task 13,
    for applying an exported `decisions.json` back to the database).
    """
    config = _load_config()
    engine = create_engine_for_config(config)
    session_maker = session_factory(engine)
    with operation_run(session_maker, 'duplicates report', 'report') as run:
        with session_maker() as session:
            artifact = ReportGenerator(config, session).generate()
        run.summary = {'output_dir': str(artifact.output_dir), 'group_count': artifact.group_count}
    typer.echo(f'report (run {run.public_id}): {artifact.output_dir}')


@duplicates_app.command('import-decisions')
def duplicates_import_decisions(
    path: Path = typer.Argument(..., help='Path to an exported decisions.json file.'),
) -> None:
    """Atomically import a browser-exported `decisions.json` (design §24,
    Task 13 -- a major review gate). Validates JSON Schema, schema version,
    report ID shape, catalog revision freshness, group/file existence,
    current group state and (for CHANGE_PREFERRED) group membership before
    writing anything; the whole file is rejected -- with zero database
    writes -- on the first failure. There is no `--force` in Milestone 1.

    On success, applies CONFIRM/CHANGE_PREFERRED (consolidating the group)
    and REJECT/DEFER (recording the human decision without consolidation),
    commits, then exports any newly-committed `CurationEvent` rows to
    `/data/curation/events.jsonl` (`CurationJournal.export_pending()`) as a
    separate, retriable step.
    """
    config = _load_config()
    engine = create_engine_for_config(config)
    session_maker = session_factory(engine)
    with operation_run(session_maker, 'duplicates import-decisions', 'import') as run:
        with session_maker() as session:
            try:
                summary = DecisionImporter(config, session).import_file(path)
            except DecisionImportError as exc:
                run.error_summary = str(exc)
                typer.echo(f'import-decisions: rejected: {exc}', err=True)
                raise typer.Exit(code=1) from exc
            exported = CurationJournal(config).export_pending(session)
        run.summary = {
            'accepted': summary.accepted,
            'groups': list(summary.group_ids),
            'journal_exported': exported,
        }

    typer.echo(
        f'import-decisions (run {run.public_id}): accepted={summary.accepted} '
        f'groups={list(summary.group_ids)} journal_exported={exported}'
    )


@catalog_app.command('stats')
def catalog_stats() -> None:
    """Show file presence, track status and metadata failure counts."""
    config = _load_config()
    engine = create_engine_for_config(config)
    with session_factory(engine)() as session:
        stats = compute_catalog_stats(session)

    typer.echo(
        f'files: total={stats.files_total} present={stats.files_present} '
        f'missing={stats.files_missing}'
    )
    track_line = ' '.join(f'{name.lower()}={count}' for name, count in stats.track_status_counts.items())
    typer.echo(f'tracks: {track_line}')
    typer.echo(
        f'scans: runs={stats.scan_runs_total} failed_files_total={stats.scan_files_failed_total} '
        f'latest={stats.latest_scan_public_id or "-"} ({stats.latest_scan_status or "-"})'
    )


@catalog_app.command('inspect')
def catalog_inspect(
    public_id: str = typer.Argument(..., help='A fil_... or trk_... public ID.'),
) -> None:
    """Show raw metadata, resolved metadata, effective identity and provenance for a file or track."""
    config = _load_config()
    engine = create_engine_for_config(config)
    with session_factory(engine)() as session:
        if public_id.startswith('fil_'):
            file = find_file_by_public_id(session, public_id)
            if file is None:
                raise typer.BadParameter(f'no file with public id {public_id}')
            _print_file(file)
            track = active_track_for_file(session, file.id)
            if track is not None:
                typer.echo('')
                typer.echo(f'track: {track.public_id} status={track.status.value}')
                _print_effective_identity(CatalogService(session).effective_identity(track))
                _print_duplicate_context(session, track)
        elif public_id.startswith('trk_'):
            track = find_track_by_public_id(session, public_id)
            if track is None:
                raise typer.BadParameter(f'no track with public id {public_id}')
            typer.echo(f'track: {track.public_id} status={track.status.value}')
            _print_effective_identity(CatalogService(session).effective_identity(track))
            for file in active_files_for_track(session, track.id):
                typer.echo('')
                _print_file(file)
            _print_duplicate_context(session, track)
        else:
            raise typer.BadParameter('public id must start with fil_ or trk_')


def _print_file(file: FileRecord) -> None:
    typer.echo(f'file: {file.public_id} path={file.relative_path}')
    typer.echo(
        'raw: '
        f'title={file.title_raw!r} artist={file.artist_raw!r} album={file.album_raw!r} '
        f'album_artist={file.album_artist_raw!r} genre={file.genre_raw!r} bpm={file.bpm_raw!r} '
        f'key={file.key_raw!r}'
    )
    typer.echo(
        'resolved: '
        f'artist={file.resolved_artist!r} ({file.artist_source}) '
        f'title={file.resolved_title!r} ({file.title_source}) '
        f'version={file.resolved_version!r} ({file.version_source}) '
        f'edition={file.resolved_edition!r} ({file.edition_source})'
    )
    typer.echo(
        'analysis: '
        f'binary_hash={file.binary_hash_status.value} '
        f'chromaprint={file.chromaprint_status.value} '
        f'quality={file.quality_status.value}'
    )


def _print_effective_identity(identity: EffectiveIdentity) -> None:
    typer.echo(
        'effective identity: '
        f'artist={identity.artist!r} title={identity.title!r} '
        f'version={identity.version!r} edition={identity.edition!r}'
    )
    if identity.featured_artists:
        featured = ', '.join(f'{fa.name} ({fa.source})' for fa in identity.featured_artists)
        typer.echo(f'featured artists: {featured}')


def _quality_result_from_row(row: FileQualityAnalysis) -> QualityResult:
    """A small local copy of `report/generator.py::_quality_result_from_row`
    (reconstructing a `QualityResult` from an already-persisted row, so this
    never re-invokes ffmpeg/fpcalc from a read-only inspect command) -- kept
    as its own copy across the module boundary for the same reason that
    module's own docstring gives for not sharing its private helpers.
    """
    details = row.details_json or {}
    return QualityResult(
        integrity_ok=row.integrity_status == 'OK',
        lossless=row.lossless_status == 'LOSSLESS',
        transcode_suspicion=row.transcode_suspicion,
        clipping_detected=row.clipping_status == 'CLIPPED',
        audio_quality_score=details.get('audio_quality_score', 0.0),
        metadata_completeness=details.get('metadata_completeness', 0.0),
        quality_score=row.quality_score if row.quality_score is not None else 0.0,
        details=details,
    )


def _group_reasons(pair_rows: list[DuplicatePairEvidence]) -> list[str]:
    """A small local copy of `report/generator.py::_group_reasons` (design
    Sec.16's own group-status rationale, mirrored read-only for display)."""
    classifications = {row.classification for row in pair_rows}
    if classifications & _INCONSISTENT_CLASSIFICATIONS:
        return [
            'conflicting or contradictory pairwise evidence within this group '
            '(design Sec.16: never rely on naive transitive closure)'
        ]
    if PairClassification.PROBABLE in classifications:
        return [
            'at least one PROBABLE pair -- plausible but not confident enough '
            'for automatic consolidation'
        ]
    if classifications:
        return ['every pairwise classification in this group is EXACT or AUDIO_EQUIVALENT']
    return ['no pairwise evidence available (fewer than two analyzed files)']


def _preferred_choice_among(
    session: Session, files: Iterable[FileRecord]
) -> PreferredChoice | None:
    """Reconstructs preferred-file rationale from already-persisted
    `FileQualityAnalysis` rows only -- never re-runs quality analysis from a
    read-only inspect command (same "recomputed, not re-run" judgment call as
    `report/generator.py`).
    """
    candidates: list[PreferredCandidate] = []
    for file in files:
        row = session.execute(
            select(FileQualityAnalysis)
            .where(FileQualityAnalysis.file_id == file.id)
            .order_by(FileQualityAnalysis.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            continue
        candidates.append(PreferredCandidate(file_id=file.id, quality=_quality_result_from_row(row)))
    if not candidates:
        return None
    return PreferredFileSelector().choose(candidates)


def _print_duplicate_context(session: Session, track: Track) -> None:
    """Shows duplicate relationships/evidence, preferred-file rationale and
    human decision provenance for `track` (design Sec.33's literal `catalog
    inspect` acceptance wording) -- entirely from already-persisted rows,
    read-only, exactly like `duplicates report` (Task 12).
    """
    files = active_files_for_track(session, track.id)
    if not files:
        return
    file_ids = [file.id for file in files]
    file_public_ids = {file.public_id for file in files}

    group_ids = list(
        session.execute(
            select(DuplicateGroupMember.group_id.distinct()).where(
                DuplicateGroupMember.file_id.in_(file_ids)
            )
        ).scalars()
    )
    groups = (
        list(session.execute(select(DuplicateGroup).where(DuplicateGroup.id.in_(group_ids))).scalars())
        if group_ids
        else []
    )

    for group in groups:
        typer.echo('')
        typer.echo(
            f'duplicate group: {group.public_id} status={group.status.value} '
            f'confidence={group.confidence}'
        )
        member_file_ids = list(
            session.execute(
                select(DuplicateGroupMember.file_id).where(DuplicateGroupMember.group_id == group.id)
            ).scalars()
        )
        member_files = {
            file.id: file
            for file in session.execute(
                select(FileRecord).where(FileRecord.id.in_(member_file_ids))
            ).scalars()
        }
        pair_rows = list(
            session.execute(
                select(DuplicatePairEvidence).where(DuplicatePairEvidence.group_id == group.id)
            ).scalars()
        )
        for reason in _group_reasons(pair_rows):
            typer.echo(f'  reason: {reason}')
        for pair in pair_rows:
            left = member_files.get(pair.left_file_id)
            right = member_files.get(pair.right_file_id)
            typer.echo(
                '  evidence: '
                f'{left.public_id if left else pair.left_file_id} vs '
                f'{right.public_id if right else pair.right_file_id} '
                f'classification={pair.classification.value} confidence={pair.confidence}'
            )
            for reason in (pair.evidence_json or {}).get('reasons', []):
                typer.echo(f'    - {reason}')

        choice = _preferred_choice_among(session, member_files.values())
        if choice is not None and choice.file_id in member_files:
            typer.echo(f'  preferred file: {member_files[choice.file_id].public_id}')
            for reason in choice.reasons:
                typer.echo(f'    - {reason}')

    events = list(
        session.execute(
            select(CurationEvent)
            .where(
                (CurationEvent.track_public_id == track.public_id)
                | (CurationEvent.file_public_id.in_(file_public_ids))
            )
            .order_by(CurationEvent.sequence)
        ).scalars()
    )
    if events:
        typer.echo('')
        typer.echo('human decision provenance:')
        for event in events:
            payload = event.payload_json or {}
            typer.echo(
                f'  [{event.sequence}] {event.event_type} '
                f'decision_source={payload.get("decision_source", "-")} '
                f'decision={payload.get("decision", "-")} '
                f'reviewed_at={payload.get("reviewed_at", "-")}'
            )
