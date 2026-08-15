"""Task 16: one comprehensive end-to-end test proving the whole Milestone 1
pipeline works together against the deterministic synthetic-audio fixture
library (`tests/fixtures/build_audio_fixtures.py`), matching the realistic
operator workflow and literal acceptance wording of design Sec.33:

    djlib doctor
    djlib scan
    djlib duplicates run
    djlib duplicates stats
    djlib duplicates report
    <hand-authored decisions.json, from the real report's manifest.json>
    djlib duplicates import-decisions
    djlib catalog inspect <public-id>
    djlib rebuild

## Why real `djlib` CLI subprocesses, not direct service calls

Every step in the list above is invoked as a real subprocess of the actual
installed `djlib` console-script entry point (`pyproject.toml`'s
`[project.scripts] djlib = "djlib.cli:app"`), with `DJLIB_CONFIG` pointing at
a real TOML file -- exactly how a real operator runs it, matching design
Sec.33's literal command list rather than reproducing its effects by calling
`ScanService`/`DuplicateService`/etc directly. This is the strongest, most
convincing proof this test can offer: if `djlib scan` (the actual entry
point, actual argument parsing, actual `typer` wiring, actual per-invocation
`OperationRun` bookkeeping) didn't work end to end, no amount of direct
service-class testing would have caught it.

Three narrow exceptions, each a deliberate judgment call:

1. `alembic upgrade head` (setup, not itself part of the operator workflow --
   a real deployment runs this once, before ever using `djlib`) is invoked
   the same way `tests/integration/test_doctor.py::_run_alembic_upgrade` and
   `RebuildService` itself already do: `alembic/env.py` reads its database
   URL solely from the `DJLIB_CONFIG` env var, so this has to be its own
   subprocess regardless.
2. Reading `manifest.json` from the real `duplicates report` output
   directory (to build a hand-authored `decisions.json`) is not a service
   call at all -- it is reading a real artifact off disk exactly as a human
   reviewer's browser would before exporting their own decisions file.
3. One direct call to `QualityAnalyzer.analyze()` (`corrupt/`'s fixture) is
   the one deliberate exception: there is no `djlib`-level command that
   invokes quality analysis on a single, specific file in isolation --
   `duplicates run` only ever runs it *inside* an already-detected duplicate
   group, and the corrupt fixture is deliberately standalone (no duplicate
   candidate partner) so Task 9's integrity-check `FAILED` path can be
   proven without inventing an artificial duplicate relationship for a file
   that has none. Every other assertion about persisted state (track/group
   statuses, effective identity, evidence, curation events, ...) reads
   already-committed rows straight out of the same SQLite database the CLI
   subprocesses just wrote to -- necessary because this milestone's CLI has
   no machine-readable output mode, and it is *reading*, not a service call
   that reproduces business logic the CLI itself already ran.

## The central assertion: zero source mutation across the *entire* chain

Every fixture file (both the committed generator's own output under
`tests/fixtures/library/`, and the working copy this test actually points
`music_root` at) is BLAKE3-hashed before the very first command runs and
again after the very last one (`rebuild`) completes -- see
`.claude/rules/source-read-only.md`. Nothing in between is allowed to change
a single byte.

## The sandboxed "read-only /music" caveat

Design Sec.33 requires "`/music` physically read-only" as a real invariant,
enforced in production by a genuine `ro=1` LXC bind mount. This sandbox runs
as root, which bypasses ordinary Unix write permissions entirely (confirmed:
`chmod 555` on a directory does not stop root from writing into it) -- so,
exactly like `tests/integration/test_doctor.py` and
`tests/integration/test_rebuild.py` already document for the very same
reason, `music_root_read_only` is expected to FAIL here and is explicitly
excluded from the "every other invariant must hold" assertions below. The
real proof of no-mutation in this environment is the hash comparison above,
not a permission bit that this sandbox cannot make meaningful.
"""

import ast
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from blake3 import blake3
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker, Session

from djlib.catalog.queries import active_track_for_file
from djlib.catalog.service import CatalogService
from djlib.config import DjlibConfig
from djlib.db.enums import DuplicateStatus, PairClassification, TrackStatus
from djlib.db.models import DuplicateGroup, FileRecord, Track
from djlib.db.session import session_factory
from djlib.duplicates.quality import QualityAnalyzer
from djlib.metadata.types import SubprocessCommandRunner

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_LIBRARY_ROOT = REPO_ROOT / 'tests' / 'fixtures' / 'library'
DJLIB_BIN = Path(sys.executable).parent / 'djlib'

EXPECTED_FIXTURE_RELATIVE_PATHS = {
    'exact-copy/crate_a/Aurora Vale - Parallel Skies.wav',
    'exact-copy/crate_b/Aurora Vale - Parallel Skies.wav',
    'mp3-vs-flac/Nova Kessler - Night Current.mp3',
    'mp3-vs-flac/Nova Kessler - Night Current.flac',
    'remix/Solace Drift - Glass Horizon (Original Mix).wav',
    'remix/Solace Drift - Glass Horizon (Midnight Remix).wav',
    'radio-edit/Halcyon Reef - Tidal Bloom (Original Mix).wav',
    'radio-edit/Halcyon Reef - Tidal Bloom (Radio Edit).wav',
    'malformed-tags/session_final_mix.m4a',
    'filename-fallback/Juno Ashford - Velvet Static.m4a',
    'corrupt/Corrupt Archive - Static Fragment.mp3',
}

# The one invariant `djlib doctor`/`djlib rebuild` cannot actually enforce in
# this root sandbox (see module docstring) -- never silently tolerate any
# *other* FAIL alongside it.
_KNOWN_SANDBOX_LIMITATIONS = frozenset({'music_root_read_only'})


def _require_fixture_library() -> None:
    if not FIXTURE_LIBRARY_ROOT.is_dir():
        pytest.fail(
            'tests/fixtures/library/ has not been generated yet -- run '
            '`python tests/fixtures/build_audio_fixtures.py` first, then re-run this test.',
            pytrace=False,
        )
    present = {
        str(path.relative_to(FIXTURE_LIBRARY_ROOT))
        for path in FIXTURE_LIBRARY_ROOT.rglob('*')
        if path.is_file()
    }
    missing = EXPECTED_FIXTURE_RELATIVE_PATHS - present
    if missing:
        pytest.fail(
            'tests/fixtures/library/ exists but is missing expected fixture file(s): '
            f'{sorted(missing)} -- run `python tests/fixtures/build_audio_fixtures.py` '
            'again (it deletes and regenerates the whole tree) and re-run this test.',
            pytrace=False,
        )


def _hash_tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): blake3(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob('*'))
        if path.is_file()
    }


def _write_config(config: DjlibConfig, path: Path) -> None:
    config.data_root.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'[paths]\nmusic_root = "{config.music_root}"\ndata_root = "{config.data_root}"\n',
        encoding='utf-8',
    )


def _run_alembic_upgrade(config_path: Path) -> None:
    """One-time schema setup -- not itself part of the operator workflow this
    test otherwise exercises purely via CLI subprocess (see module docstring,
    reason 1). Mirrors `tests/integration/test_doctor.py::_run_alembic_upgrade`
    and `RebuildService._recreate_database` exactly.
    """
    subprocess.run(
        [sys.executable, '-m', 'alembic', 'upgrade', 'head'],
        cwd=REPO_ROOT,
        env={**os.environ, 'DJLIB_CONFIG': str(config_path)},
        check=True,
        capture_output=True,
        text=True,
    )


def _run_cli(config_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(DJLIB_BIN), *args],
        env={**os.environ, 'DJLIB_CONFIG': str(config_path)},
        capture_output=True,
        text=True,
    )


def _parse_doctor_lines(stdout: str) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for line in stdout.splitlines():
        match = re.match(r'^\[(?P<status>PASS|FAIL)\]\s+(?P<name>\S+):', line)
        if match:
            statuses[match.group('name')] = match.group('status')
    return statuses


def _assert_only_known_sandbox_limitation_fails(check_statuses: dict[str, str]) -> None:
    assert check_statuses, 'expected at least one doctor check line to have been parsed'
    failing = {name for name, status in check_statuses.items() if status == 'FAIL'}
    assert failing <= _KNOWN_SANDBOX_LIMITATIONS, (
        f'unexpected doctor check failure(s): {sorted(failing - _KNOWN_SANDBOX_LIMITATIONS)} '
        f'(full statuses: {check_statuses})'
    )


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _group_by_path_prefix(manifest: dict, prefix: str) -> dict:
    for group in manifest['groups']:
        if any(f['relative_path'].startswith(prefix) for f in group['files']):
            return group
    raise AssertionError(f'no report group has a file under {prefix!r}: {manifest["groups"]}')


# The three file-pairs `duplicates run`/`import-decisions` consolidate onto
# one surviving track each -- two purely `AUTOMATIC` (exact-copy, mp3-vs-flac)
# and one `HUMAN`-confirmed (radio-edit, the CONFIRM decision above).
#
# ## A real gap this end-to-end test surfaced, and its fix
#
# `Track.public_id` survives `rebuild` for *all three* survivors alike --
# `merge_track_into` (`catalog/service.py`) always records a `TRACK_MERGE`
# `CurationEvent`, `AUTOMATIC` or `HUMAN`. But `Track.preferred_file_id` did
# NOT originally survive for the two purely-`AUTOMATIC` survivors:
# `_apply_merge` (`curation/replay.py`) calls `activate_track(target_track)`
# with no `preferred_file_id` argument, because the plain `TRACK_MERGE`
# event's payload never carries one -- only a `DUPLICATE_GROUP_CONFIRMED`/
# `CHANGE_PREFERRED` event did, via `preferred_file_relative_path`
# (`curation/decisions.py::_record_curation_event`), and that event is only
# ever written for a *human* decision. Fixed by having
# `DuplicateService._consolidate_auto_confirmed_groups` (the fully-automatic
# path) durably record its own `TRACK_PREFERRED_FILE_AUTO_SET` event
# (`CatalogService.record_automatic_preferred_file`), which
# `curation/replay.py::_apply_automatic_preferred_file` restores exactly like
# a human `CONFIRM`'s preferred file -- see `tests/integration/
# test_rebuild.py::test_rebuild_restores_preferred_file_for_a_fully_automatic_consolidation`.
#
# Separately, a file that was never part of any duplicate group at all (this
# fixture library's `malformed-tags/`, `filename-fallback/`, `corrupt/`, and
# the *rejected* `remix/` pair) has no `CurationEvent` anchoring it either, so
# its `Track.public_id` is not guaranteed stable across `rebuild` -- there is
# nothing in `events.jsonl` for replay to restore it from. This is expected,
# not a bug: nothing outside an in-progress rebuild ever observes that
# track's `public_id` before a curation event anchors it, so there is nothing
# to preserve (`curation/replay.py`'s own module docstring: overwriting a
# `public_id` during replay is only ever restoring a *previously externally
# referenced* identity).
_KNOWN_MERGE_PAIRS = (
    (
        'exact-copy/crate_a/Aurora Vale - Parallel Skies.wav',
        'exact-copy/crate_b/Aurora Vale - Parallel Skies.wav',
    ),
    (
        'mp3-vs-flac/Nova Kessler - Night Current.mp3',
        'mp3-vs-flac/Nova Kessler - Night Current.flac',
    ),
    (
        'radio-edit/Halcyon Reef - Tidal Bloom (Original Mix).wav',
        'radio-edit/Halcyon Reef - Tidal Bloom (Radio Edit).wav',
    ),
)


def _survivor_track_public_id(session_maker: sessionmaker[Session], relative_path: str) -> str:
    with session_maker() as session:
        file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == relative_path)
        ).scalar_one()
        track = active_track_for_file(session, file.id)
        assert track is not None, f'{relative_path} has no active track'
        return track.public_id


def _survivor_preferred_relative_path(
    session_maker: sessionmaker[Session], relative_path: str
) -> str:
    with session_maker() as session:
        file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == relative_path)
        ).scalar_one()
        track = active_track_for_file(session, file.id)
        assert track is not None, f'{relative_path} has no active track'
        assert track.preferred_file_id is not None, f'{relative_path}: no preferred file chosen'
        return session.get(FileRecord, track.preferred_file_id).relative_path


def _track_status_counts(session_maker: sessionmaker[Session]) -> dict[str, int]:
    with session_maker() as session:
        counts: dict[str, int] = {}
        for track in session.execute(select(Track)).scalars():
            counts[track.status.value] = counts.get(track.status.value, 0) + 1
        return counts


def test_milestone_one_end_to_end_operator_workflow(tmp_path: Path) -> None:
    _require_fixture_library()

    # -- fixture-library hashes, taken before anything runs: the committed
    # generator's own output, untouched for the whole test (nothing below
    # ever points djlib at this path directly), and the working copy djlib
    # actually operates against as `music_root`.
    canonical_hashes_before = _hash_tree(FIXTURE_LIBRARY_ROOT)

    music_root = tmp_path / 'music'
    data_root = tmp_path / 'data'
    shutil.copytree(FIXTURE_LIBRARY_ROOT, music_root)
    working_hashes_before = _hash_tree(music_root)
    assert working_hashes_before == canonical_hashes_before

    config = DjlibConfig(music_root=music_root, data_root=data_root)
    config_path = tmp_path / 'djlib.toml'
    _write_config(config, config_path)

    # -- one-time setup (see module docstring, exception 1) --------------
    _run_alembic_upgrade(config_path)

    # -- `djlib doctor`: a real operator's first sanity check -------------
    doctor_result = _run_cli(config_path, 'doctor')
    _assert_only_known_sandbox_limitation_fails(_parse_doctor_lines(doctor_result.stdout))

    # -- `djlib scan` -------------------------------------------------------
    scan_result = _run_cli(config_path, 'scan')
    assert scan_result.returncode == 0, scan_result.stderr
    scan_match = re.search(
        r'seen=(\d+) new=(\d+) changed=(\d+) unchanged=(\d+) missing=(\d+) failed=(\d+)',
        scan_result.stdout,
    )
    assert scan_match is not None, scan_result.stdout
    seen, new, changed, unchanged, missing, failed = (int(g) for g in scan_match.groups())
    assert seen == len(EXPECTED_FIXTURE_RELATIVE_PATHS)
    assert new == len(EXPECTED_FIXTURE_RELATIVE_PATHS)
    assert (changed, unchanged, missing, failed) == (0, 0, 0, 0)

    engine = create_engine(config.database_url, future=True)
    session_maker = session_factory(engine)

    with session_maker() as session:
        files_by_path = {
            f.relative_path: f for f in session.execute(select(FileRecord)).scalars()
        }
        assert set(files_by_path) == EXPECTED_FIXTURE_RELATIVE_PATHS

        malformed = files_by_path['malformed-tags/session_final_mix.m4a']
        assert (malformed.resolved_artist, malformed.artist_source) == (None, 'UNKNOWN')
        assert (malformed.resolved_title, malformed.title_source) == (None, 'UNKNOWN')

        fallback = files_by_path['filename-fallback/Juno Ashford - Velvet Static.m4a']
        assert (fallback.resolved_artist, fallback.artist_source) == ('Juno Ashford', 'FILENAME')
        assert (fallback.resolved_title, fallback.title_source) == ('Velvet Static', 'FILENAME')

        corrupt = files_by_path['corrupt/Corrupt Archive - Static Fragment.mp3']
        assert corrupt.is_present is True
        assert corrupt.metadata_updated_at is not None
        assert corrupt.duration_ms is not None  # ffprobe still reads the intact header

    # -- Task 9's integrity-check FAILED path (module docstring, exception 3) --
    with session_maker() as session:
        corrupt = session.execute(
            select(FileRecord).where(
                FileRecord.relative_path == 'corrupt/Corrupt Archive - Static Fragment.mp3'
            )
        ).scalar_one()
        result = QualityAnalyzer(SubprocessCommandRunner()).analyze(
            config.music_root / corrupt.relative_path, corrupt
        )
        assert result.integrity_ok is False
        session.commit()

    # -- `djlib duplicates run` (detect + analyze + safe auto-consolidation) --
    run_result = _run_cli(config_path, 'duplicates', 'run')
    assert run_result.returncode == 0, run_result.stderr
    run_match = re.search(r'detected=(\d+) analyzed=(\d+) consolidated=(\d+)', run_result.stdout)
    assert run_match is not None, run_result.stdout
    detected, analyzed, consolidated = (int(g) for g in run_match.groups())
    assert (detected, analyzed, consolidated) == (4, 4, 2)

    with session_maker() as session:
        groups = list(session.execute(select(DuplicateGroup)).scalars())
        assert len(groups) == 4
        status_counts: dict[str, int] = {}
        for group in groups:
            status_counts[group.status.value] = status_counts.get(group.status.value, 0) + 1
        assert status_counts == {
            DuplicateStatus.AUTO_CONFIRMED.value: 2,
            DuplicateStatus.REVIEW_REQUIRED.value: 2,
        }

        tracks = list(session.execute(select(Track)).scalars())
        track_status_counts: dict[str, int] = {}
        for track in tracks:
            track_status_counts[track.status.value] = track_status_counts.get(track.status.value, 0) + 1
        # exact-copy + mp3-vs-flac auto-consolidated (1 ACTIVE + 1 MERGED
        # each); malformed-tags/filename-fallback/corrupt/remix(x2)/
        # radio-edit(x2) still untouched, one PROVISIONAL track per file.
        assert track_status_counts == {
            TrackStatus.ACTIVE.value: 2,
            TrackStatus.MERGED.value: 2,
            TrackStatus.PROVISIONAL.value: 7,
        }

    # -- `djlib duplicates stats` --------------------------------------------
    stats_result = _run_cli(config_path, 'duplicates', 'stats')
    assert stats_result.returncode == 0, stats_result.stderr
    assert 'auto_confirmed=2' in stats_result.stdout
    assert 'review_required=2' in stats_result.stdout
    assert f'{PairClassification.EXACT.value.lower()}=1' in stats_result.stdout
    assert f'{PairClassification.AUDIO_EQUIVALENT.value.lower()}=1' in stats_result.stdout
    assert f'{PairClassification.DIFFERENT.value.lower()}=2' in stats_result.stdout

    # -- `djlib duplicates report` -- a real static HTML/manifest artifact --
    report_result = _run_cli(config_path, 'duplicates', 'report')
    assert report_result.returncode == 0, report_result.stderr
    report_match = re.search(r'report \(run \S+\): (?P<path>.+)$', report_result.stdout.strip())
    assert report_match is not None, report_result.stdout
    report_dir = Path(report_match.group('path'))
    manifest = json.loads((report_dir / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['report_id'].startswith('rpt_')

    remix_group = _group_by_path_prefix(manifest, 'remix/')
    radio_edit_group = _group_by_path_prefix(manifest, 'radio-edit/')
    assert remix_group['status'] == DuplicateStatus.REVIEW_REQUIRED.value
    assert radio_edit_group['status'] == DuplicateStatus.REVIEW_REQUIRED.value
    assert radio_edit_group['proposed_preferred_file_id'] is not None

    # -- hand-author decisions.json from the real report's manifest ---------
    # A human reviewer, working through this exact report, recognizes the
    # "radio-edit" pair was actually mislabeled (it is, in fact, the same
    # performance) and overrides the automatic DIFFERENT/REVIEW_REQUIRED
    # verdict with CONFIRM -- proving design Sec.33's "human decisions
    # override automation" invariant, not merely a case the automation
    # already agreed with. The "remix" pair, on listening, genuinely is a
    # different rendition, so the reviewer REJECTs the duplicate suggestion.
    decisions_path = tmp_path / 'decisions.json'
    decisions_path.write_text(
        json.dumps(
            {
                'schema_version': 1,
                'report_id': manifest['report_id'],
                'catalog_revision': manifest['catalog_revision'],
                'generated_at': _now_iso(),
                'decisions': [
                    {
                        'group_id': radio_edit_group['group_id'],
                        'decision': 'CONFIRM',
                        'reviewed_at': _now_iso(),
                    },
                    {
                        'group_id': remix_group['group_id'],
                        'decision': 'REJECT',
                        'reviewed_at': _now_iso(),
                    },
                ],
            }
        ),
        encoding='utf-8',
    )

    # -- `djlib duplicates import-decisions` ---------------------------------
    import_result = _run_cli(config_path, 'duplicates', 'import-decisions', str(decisions_path))
    assert import_result.returncode == 0, import_result.stderr
    assert 'accepted=2' in import_result.stdout
    assert 'journal_exported=' in import_result.stdout
    exported_match = re.search(r'journal_exported=(\d+)', import_result.stdout)
    assert exported_match is not None and int(exported_match.group(1)) > 0

    with session_maker() as session:
        radio_edit_db_group = session.execute(
            select(DuplicateGroup).where(DuplicateGroup.public_id == radio_edit_group['group_id'])
        ).scalar_one()
        assert radio_edit_db_group.status == DuplicateStatus.CONFIRMED
        assert radio_edit_db_group.resolved_at is not None

        remix_db_group = session.execute(
            select(DuplicateGroup).where(DuplicateGroup.public_id == remix_group['group_id'])
        ).scalar_one()
        assert remix_db_group.status == DuplicateStatus.REJECTED
        assert remix_db_group.resolved_at is not None

        remix_tracks = {
            active_track_for_file(session, f.id).status
            for f in session.execute(
                select(FileRecord).where(FileRecord.relative_path.like('remix/%'))
            ).scalars()
        }
        assert remix_tracks == {TrackStatus.PROVISIONAL}  # REJECT never consolidates

        radio_edit_original = session.execute(
            select(FileRecord).where(
                FileRecord.relative_path
                == 'radio-edit/Halcyon Reef - Tidal Bloom (Original Mix).wav'
            )
        ).scalar_one()
        survivor_track = active_track_for_file(session, radio_edit_original.id)
        assert survivor_track.status == TrackStatus.ACTIVE
        assert survivor_track.preferred_file_id is not None
        survivor_public_id = survivor_track.public_id

    # -- `djlib catalog inspect <public-id>` ---------------------------------
    # design Sec.33's literal acceptance wording: effective identity, source
    # metadata, duplicate relationships, evidence, preferred-file rationale,
    # human decision provenance -- all in one command's output.
    inspect_result = _run_cli(config_path, 'catalog', 'inspect', survivor_public_id)
    assert inspect_result.returncode == 0, inspect_result.stderr
    out = inspect_result.stdout
    assert f'track: {survivor_public_id}' in out
    assert 'status=ACTIVE' in out
    assert 'effective identity: artist=\'Halcyon Reef\' title=\'Tidal Bloom\'' in out
    assert 'raw:' in out and 'resolved:' in out  # source metadata, per file
    assert f"duplicate group: {radio_edit_group['group_id']}" in out
    assert 'status=CONFIRMED' in out
    assert 'evidence:' in out and 'classification=DIFFERENT' in out
    assert 'preferred file:' in out
    assert 'human decision provenance:' in out
    assert 'TRACK_MERGE' in out
    assert 'DUPLICATE_GROUP_CONFIRMED' in out
    assert 'decision=CONFIRM' in out

    # -- snapshot the curated projection before rebuild ----------------------
    # Every consolidated pair's survivor track carries a `TRACK_MERGE`
    # `CurationEvent` regardless of `AUTOMATIC` or `HUMAN` decision source
    # (`catalog/service.py::merge_track_into` always records one) -- so
    # `replay` restores *all three* survivors' `Track.public_id` durably.
    # Each survivor's `preferred_file_id` is durably recorded too: `radio-edit`
    # via its human `DUPLICATE_GROUP_CONFIRMED` event's
    # `preferred_file_relative_path`, and the two purely-`AUTOMATIC` pairs via
    # their own `TRACK_PREFERRED_FILE_AUTO_SET` event (see this module's
    # docstring above `_KNOWN_MERGE_PAIRS`).
    before_survivor_ids = {
        pair: _survivor_track_public_id(session_maker, pair[0]) for pair in _KNOWN_MERGE_PAIRS
    }
    for a, b in _KNOWN_MERGE_PAIRS:
        assert _survivor_track_public_id(session_maker, b) == before_survivor_ids[(a, b)]
    before_preferred_relative_paths = {
        pair: _survivor_preferred_relative_path(session_maker, pair[0])
        for pair in _KNOWN_MERGE_PAIRS
    }
    before_track_status_counts = _track_status_counts(session_maker)
    with session_maker() as session:
        radio_edit_original = session.execute(
            select(FileRecord).where(
                FileRecord.relative_path
                == 'radio-edit/Halcyon Reef - Tidal Bloom (Original Mix).wav'
            )
        ).scalar_one()
        before_survivor = active_track_for_file(session, radio_edit_original.id)
        before_preferred_relative_path = session.get(
            FileRecord, before_survivor.preferred_file_id
        ).relative_path

    engine.dispose()

    hashes_before_rebuild = _hash_tree(music_root)
    assert hashes_before_rebuild == working_hashes_before

    # -- `djlib rebuild` ------------------------------------------------------
    rebuild_result = _run_cli(config_path, 'rebuild')
    rebuild_match = re.search(
        r'scan\(seen=(\d+) new=(\d+) failed=(\d+)\) '
        r'replay\(events=(\d+) overrides=(\d+) merges=(\d+) splits=(\d+) decisions=(\d+) '
        r'auto_preferred_files=(\d+)\) '
        r'invariants_ok=(\S+)',
        rebuild_result.stdout,
    )
    assert rebuild_match is not None, rebuild_result.stdout
    (
        rb_seen, rb_new, rb_failed,
        rb_events, rb_overrides, rb_merges, rb_splits, rb_decisions, rb_auto_preferred_files,
        rb_invariants_ok,
    ) = rebuild_match.groups()
    assert (int(rb_seen), int(rb_new), int(rb_failed)) == (
        len(EXPECTED_FIXTURE_RELATIVE_PATHS), len(EXPECTED_FIXTURE_RELATIVE_PATHS), 0,
    )
    assert int(rb_events) > 0
    assert int(rb_overrides) == 0
    assert int(rb_merges) >= 3  # exact-copy + mp3-vs-flac (automatic) + radio-edit (human)
    assert int(rb_splits) == 0
    assert int(rb_decisions) == 2  # the CONFIRM + the REJECT
    assert int(rb_auto_preferred_files) == 2  # exact-copy + mp3-vs-flac, both AUTOMATIC

    if rb_invariants_ok != 'True':
        failed_match = re.search(r'failed invariant checks: (\[.*\])', rebuild_result.stderr)
        assert failed_match is not None, rebuild_result.stderr
        failed_checks = set(ast.literal_eval(failed_match.group(1)))
        assert failed_checks <= _KNOWN_SANDBOX_LIMITATIONS, failed_checks

    # -- rebuilt catalogue must reproduce the exact same curated projection --
    rebuilt_engine = create_engine(config.database_url, future=True)
    try:
        rebuilt_session_maker = session_factory(rebuilt_engine)

        # Every consolidated pair's survivor track keeps its exact original
        # public_id across a from-scratch rebuild -- true for the two
        # automatic merges just as much as the human-confirmed one.
        for a, b in _KNOWN_MERGE_PAIRS:
            expected = before_survivor_ids[(a, b)]
            assert _survivor_track_public_id(rebuilt_session_maker, a) == expected
            assert _survivor_track_public_id(rebuilt_session_maker, b) == expected

        # ...and so does the preferred-file choice, `AUTOMATIC` or `HUMAN`
        # alike (design Sec.33's "same preferred-file decisions").
        for pair in _KNOWN_MERGE_PAIRS:
            assert (
                _survivor_preferred_relative_path(rebuilt_session_maker, pair[0])
                == before_preferred_relative_paths[pair]
            )

        assert _track_status_counts(rebuilt_session_maker) == before_track_status_counts

        with rebuilt_session_maker() as session:
            radio_edit_original = session.execute(
                select(FileRecord).where(
                    FileRecord.relative_path
                    == 'radio-edit/Halcyon Reef - Tidal Bloom (Original Mix).wav'
                )
            ).scalar_one()
            rebuilt_survivor = active_track_for_file(session, radio_edit_original.id)
            assert rebuilt_survivor.public_id == survivor_public_id
            assert rebuilt_survivor.status == TrackStatus.ACTIVE
            rebuilt_identity = CatalogService(session).effective_identity(rebuilt_survivor)
            assert rebuilt_identity.artist == 'Halcyon Reef'
            assert rebuilt_identity.title == 'Tidal Bloom'

            # The *human*-confirmed preferred-file decision is durably
            # restored (its own `DUPLICATE_GROUP_CONFIRMED` event carries
            # `preferred_file_relative_path` explicitly -- design Sec.33's
            # "same preferred-file decisions").
            assert rebuilt_survivor.preferred_file_id is not None
            after_preferred_relative_path = session.get(
                FileRecord, rebuilt_survivor.preferred_file_id
            ).relative_path
            assert after_preferred_relative_path == before_preferred_relative_path
    finally:
        rebuilt_engine.dispose()

    # -- the central assertion: zero source mutation across the entire chain --
    assert _hash_tree(music_root) == working_hashes_before
    assert _hash_tree(FIXTURE_LIBRARY_ROOT) == canonical_hashes_before
