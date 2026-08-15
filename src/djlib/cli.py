import os
import sys
from pathlib import Path

import typer

from djlib.catalog.queries import (
    active_files_for_track,
    active_track_for_file,
    compute_catalog_stats,
    find_file_by_public_id,
    find_track_by_public_id,
)
from djlib.catalog.service import CatalogService, EffectiveIdentity
from djlib.config import DjlibConfig
from djlib.db.engine import create_engine_for_config
from djlib.db.models import FileRecord
from djlib.db.session import session_factory
from djlib.duplicates.calibration import (
    collect_calibration_rows,
    write_calibration_csv,
    write_calibration_json,
)
from djlib.duplicates.chromaprint import ChromaprintService
from djlib.duplicates.hashing import HashService
from djlib.duplicates.service import DuplicateService
from djlib.metadata.types import SubprocessCommandRunner
from djlib.scan.service import ScanService

app = typer.Typer(no_args_is_help=True, help='Local DJ-library catalogue and deduplication tool.')
catalog_app = typer.Typer(no_args_is_help=True, help='Inspect the catalogue.')
duplicates_app = typer.Typer(no_args_is_help=True, help='Duplicate-detection utilities.')
app.add_typer(catalog_app, name='catalog')
app.add_typer(duplicates_app, name='duplicates')


@app.callback()
def main() -> None:
    """Local DJ-library catalogue and deduplication tool."""


def _load_config() -> DjlibConfig:
    config_path = os.environ.get('DJLIB_CONFIG')
    return DjlibConfig.load(Path(config_path)) if config_path else DjlibConfig.defaults()


@app.command()
def scan(full: bool = typer.Option(False, '--full', help='Force a full rescan.')) -> None:
    """Scan music_root and incrementally update the catalogue."""
    config = _load_config()
    engine = create_engine_for_config(config)
    service = ScanService(config, session_factory(engine))
    summary = service.scan(full=full)
    typer.echo(
        f'scan {summary.public_id}: seen={summary.files_seen} new={summary.files_new} '
        f'changed={summary.files_changed} unchanged={summary.files_unchanged} '
        f'missing={summary.files_missing} failed={summary.files_failed}'
    )


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
    with session_factory(engine)() as session:
        groups = DuplicateService(config, session).detect()
    typer.echo(f'detect: groups={groups}')


@duplicates_app.command('analyze')
def duplicates_analyze() -> None:
    """Targeted BLAKE3/Chromaprint/quality evidence, classification and
    preferred-file proposal for already-detected groups (design §17-21).
    Never touches CONFIRMED/REJECTED/DEFERRED (human-decided) groups.
    """
    config = _load_config()
    engine = create_engine_for_config(config)
    with session_factory(engine)() as session:
        groups = DuplicateService(config, session).analyze()
    typer.echo(f'analyze: groups_analyzed={groups}')


@duplicates_app.command('run')
def duplicates_run() -> None:
    """detect + analyze + safe automatic consolidation of AUTO_CONFIRMED
    groups only. Does not generate an HTML report (see `duplicates report`,
    a later task).
    """
    config = _load_config()
    engine = create_engine_for_config(config)
    with session_factory(engine)() as session:
        summary = DuplicateService(config, session).run()
    typer.echo(
        f'run: detected={summary.groups_detected} analyzed={summary.groups_analyzed} '
        f'consolidated={summary.groups_consolidated}'
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
        elif public_id.startswith('trk_'):
            track = find_track_by_public_id(session, public_id)
            if track is None:
                raise typer.BadParameter(f'no track with public id {public_id}')
            typer.echo(f'track: {track.public_id} status={track.status.value}')
            _print_effective_identity(CatalogService(session).effective_identity(track))
            for file in active_files_for_track(session, track.id):
                typer.echo('')
                _print_file(file)
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
