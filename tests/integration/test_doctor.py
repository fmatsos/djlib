import importlib
import os
import pathlib
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from djlib import doctor
from djlib.config import DjlibConfig
from djlib.db.enums import DecisionSource, RelationshipType, TrackStatus
from djlib.db.models import CurationEvent, FileRecord, Track, TrackFile
from djlib.db.session import session_factory
from djlib.doctor import CheckStatus, Doctor
from djlib.ids import new_public_id

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_alembic_upgrade(music_root: Path, data_root: Path) -> None:
    """Runs the real `alembic upgrade head` in its own subprocess.

    Deliberately a subprocess rather than `alembic.command.upgrade()`
    in-process: `alembic/env.py` derives its database URL solely from the
    `DJLIB_CONFIG` env var (matching how migrations are actually applied in
    production, as a separate `alembic upgrade head` invocation -- never in
    the same process as a `djlib` command), and its `fileConfig(...)` call
    disables every pre-existing logger not listed in `alembic.ini` --
    including `djlib`'s -- which would otherwise leak into every other test
    sharing this pytest process.
    """
    data_root.mkdir(parents=True, exist_ok=True)
    config_path = data_root / 'alembic-test-config.toml'
    config_path.write_text(
        f'[paths]\nmusic_root = "{music_root}"\ndata_root = "{data_root}"\n',
        encoding='utf-8',
    )
    subprocess.run(
        [sys.executable, '-m', 'alembic', 'upgrade', 'head'],
        cwd=REPO_ROOT,
        env={**os.environ, 'DJLIB_CONFIG': str(config_path)},
        check=True,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# music_root exists / is read-only
# ---------------------------------------------------------------------------


def test_music_root_missing_fails(config: DjlibConfig) -> None:
    assert not config.music_root.exists()
    result = doctor.check_music_root_exists(config)
    assert result.status == CheckStatus.FAIL


def test_music_root_read_only_check_fails_and_cleans_up_when_write_succeeds(
    config: DjlibConfig,
) -> None:
    """This sandbox has no real read-only bind mount, so a plain writable
    directory is the natural "write unexpectedly succeeds" case -- the check
    must report FAIL and must not leave the probe file behind.
    """
    config.music_root.mkdir(parents=True)
    before = set(config.music_root.iterdir())

    result = doctor.check_music_root_read_only(config)

    assert result.status == CheckStatus.FAIL
    after = set(config.music_root.iterdir())
    assert after == before, 'probe file must be cleaned up, and no other file touched'


def test_music_root_read_only_check_passes_when_write_fails(
    config: DjlibConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the real read-only bind mount (`ro=1`) by making the probe's
    own `open(..., 'x')` raise, without relying on filesystem permission bits
    (this sandbox runs as root, so `chmod` alone would not actually block a
    write). Real `open` is used for every other path, so this never touches
    an unrelated file.
    """
    config.music_root.mkdir(parents=True)
    real_open = pathlib.Path.open

    def fake_open(self: pathlib.Path, mode: str = 'r', *args: object, **kwargs: object):
        if doctor.PROBE_PREFIX in self.name and 'x' in mode:
            raise PermissionError('simulated read-only mount')
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, 'open', fake_open)

    result = doctor.check_music_root_read_only(config)

    assert result.status == CheckStatus.PASS
    assert list(config.music_root.iterdir()) == []


def test_music_root_read_only_check_fails_when_directory_missing(config: DjlibConfig) -> None:
    result = doctor.check_music_root_read_only(config)
    assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# data_root writable
# ---------------------------------------------------------------------------


def test_data_root_writable_check_passes(config: DjlibConfig) -> None:
    result = doctor.check_data_root_writable(config)
    assert result.status == CheckStatus.PASS
    assert list(config.data_root.iterdir()) == []


def test_data_root_writable_check_fails_when_write_fails(
    config: DjlibConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.data_root.mkdir(parents=True)
    real_open = pathlib.Path.open

    def fake_open(self: pathlib.Path, mode: str = 'r', *args: object, **kwargs: object):
        if doctor.PROBE_PREFIX in self.name and 'x' in mode:
            raise PermissionError('simulated unwritable /data')
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, 'open', fake_open)

    result = doctor.check_data_root_writable(config)
    assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# SQLite readable / migrations current
# ---------------------------------------------------------------------------


def test_sqlite_readable_check_passes(engine: Engine) -> None:
    result = doctor.check_sqlite_readable(session_factory(engine))
    assert result.status == CheckStatus.PASS


def test_sqlite_readable_check_fails_for_broken_connection(tmp_path: Path) -> None:
    bogus_url = f"sqlite:///{tmp_path / 'no-such-dir' / 'catalog.sqlite'}"
    bogus_engine = create_engine(bogus_url, future=True)
    result = doctor.check_sqlite_readable(session_factory(bogus_engine))
    assert result.status == CheckStatus.FAIL
    bogus_engine.dispose()


def test_migrations_current_check_fails_without_alembic_stamp(engine: Engine) -> None:
    """`engine` (conftest) is seeded via `Base.metadata.create_all`, not via
    `alembic upgrade head` -- there is no `alembic_version` row, which is
    exactly the "migrations not current" scenario.
    """
    result = doctor.check_migrations_current(session_factory(engine))
    assert result.status == CheckStatus.FAIL


def test_migrations_current_check_passes_after_real_alembic_upgrade(tmp_path: Path) -> None:
    music_root = tmp_path / 'srcaudio'
    music_root.mkdir()
    data_root = tmp_path / 'data'
    _run_alembic_upgrade(music_root, data_root)

    database_url = f"sqlite:///{data_root / 'catalog.sqlite'}"
    real_engine = create_engine(database_url, future=True)
    result = doctor.check_migrations_current(session_factory(real_engine))
    assert result.status == CheckStatus.PASS
    real_engine.dispose()


def test_doctor_run_reports_failures_gracefully_on_completely_missing_schema(
    tmp_path: Path,
) -> None:
    """A SQLite file with no tables at all (migrations never run) must not
    crash `Doctor.run()` -- every DB-dependent check past `migrations_current`
    should degrade to a clear FAIL rather than an unhandled exception, since
    `djlib doctor` is the tool meant to diagnose exactly this situation.
    """
    config = DjlibConfig(music_root=tmp_path / 'srcaudio', data_root=tmp_path / 'data')
    bare_engine = create_engine(config.database_url, future=True)
    session_maker = session_factory(bare_engine)

    report = Doctor(config, session_maker).run()

    by_name = {check.name: check for check in report.checks}
    assert by_name['migrations_current'].status == CheckStatus.FAIL
    assert by_name['curation_sequence'].status == CheckStatus.FAIL
    assert by_name['active_track_file_uniqueness'].status == CheckStatus.FAIL
    assert by_name['preferred_file_exists'].status == CheckStatus.FAIL
    bare_engine.dispose()


# ---------------------------------------------------------------------------
# exiftool / ffprobe / fpcalc / BLAKE3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('name', ['exiftool', 'ffprobe', 'fpcalc'])
def test_check_executable_passes_when_present(name: str) -> None:
    result = doctor.check_executable(name)
    assert result.status == CheckStatus.PASS


@pytest.mark.parametrize('name', ['exiftool', 'ffprobe', 'fpcalc'])
def test_check_executable_fails_when_missing(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, 'which', lambda _name: None)
    result = doctor.check_executable(name)
    assert result.status == CheckStatus.FAIL


def test_check_blake3_passes() -> None:
    result = doctor.check_blake3()
    assert result.status == CheckStatus.PASS


def test_check_blake3_fails_when_import_is_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    """BLAKE3 is actually installed in this environment, so the import itself
    can't be made to fail for real -- this patches `importlib.import_module`
    (as used by `check_blake3`) to simulate that failure instead.
    """

    def fake_import_module(name: str):
        if name == 'blake3':
            raise ImportError('simulated missing blake3')
        return importlib.import_module(name)

    monkeypatch.setattr(doctor.importlib, 'import_module', fake_import_module)
    result = doctor.check_blake3()
    assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# curation JSONL sequence vs SQLite
# ---------------------------------------------------------------------------


def test_curation_sequence_check_passes_when_nothing_pending(engine: Engine) -> None:
    result = doctor.check_curation_sequence(session_factory(engine))
    assert result.status == CheckStatus.PASS


def test_curation_sequence_gap_fails_and_repair_journal_fixes_it(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    session_maker = session_factory(engine)
    with session_maker() as session:
        session.add(
            CurationEvent(
                sequence=1,
                event_uuid=str(uuid.uuid4()),
                event_type='CONFIRM',
                track_public_id='trk_test',
                file_public_id=None,
                payload_json={'group_id': 'dgp_test'},
            )
        )
        session.commit()

    plain_report = Doctor(config, session_maker).run(repair_journal=False)
    plain_check = next(c for c in plain_report.checks if c.name == 'curation_sequence')
    assert plain_check.status == CheckStatus.FAIL
    journal_path = config.data_root / 'curation' / 'events.jsonl'
    assert not journal_path.exists(), 'plain doctor must not repair anything'

    repaired_report = Doctor(config, session_maker).run(repair_journal=True)
    repaired_check = next(c for c in repaired_report.checks if c.name == 'curation_sequence')
    assert repaired_check.status == CheckStatus.PASS
    assert journal_path.exists()
    assert '"sequence": 1' in journal_path.read_text(encoding='utf-8')


# ---------------------------------------------------------------------------
# no active file belongs to multiple active tracks
# ---------------------------------------------------------------------------


def test_no_duplicate_active_track_files_check_passes(engine: Engine) -> None:
    result = doctor.check_no_duplicate_active_track_files(session_factory(engine))
    assert result.status == CheckStatus.PASS


def test_no_duplicate_active_track_files_check_fails_on_hand_crafted_violation(
    engine: Engine,
) -> None:
    """`uq_track_files_one_active_per_file` (Task 2's partial unique index)
    makes this structurally impossible through normal inserts -- the index
    is dropped here purely to hand-craft a violation and prove the *query*
    the check runs is correct, per the schema being only the last line of
    defense (design §27 asks doctor to verify this invariant explicitly).
    """
    with engine.connect() as conn:
        conn.execute(text('DROP INDEX uq_track_files_one_active_per_file'))
        conn.commit()

    session_maker = session_factory(engine)
    with session_maker() as session:
        file = FileRecord(
            public_id=new_public_id('fil'),
            relative_path='a.mp3',
            size_bytes=1,
            mtime_ns=1,
            extension='.mp3',
        )
        session.add(file)
        session.flush()
        file_id = file.id
        track_a = Track(public_id=new_public_id('trk'), status=TrackStatus.ACTIVE)
        track_b = Track(public_id=new_public_id('trk'), status=TrackStatus.ACTIVE)
        session.add_all([track_a, track_b])
        session.flush()
        session.add_all(
            [
                TrackFile(
                    track_id=track_a.id,
                    file_id=file.id,
                    relationship=RelationshipType.PRIMARY,
                    decision_source=DecisionSource.AUTOMATIC,
                    is_active=True,
                ),
                TrackFile(
                    track_id=track_b.id,
                    file_id=file.id,
                    relationship=RelationshipType.PRIMARY,
                    decision_source=DecisionSource.AUTOMATIC,
                    is_active=True,
                ),
            ]
        )
        session.commit()

    result = doctor.check_no_duplicate_active_track_files(session_maker)
    assert result.status == CheckStatus.FAIL
    assert str(file_id) in result.message


# ---------------------------------------------------------------------------
# preferred_file_id resolves to a real FileRecord
# ---------------------------------------------------------------------------


def test_preferred_file_exists_check_passes_when_unset(engine: Engine) -> None:
    result = doctor.check_preferred_files_exist(session_factory(engine))
    assert result.status == CheckStatus.PASS


def test_preferred_file_exists_check_passes_when_file_present_but_missing_from_disk(
    engine: Engine,
) -> None:
    """A preferred file marked `is_present=False` still resolves to a real
    `FileRecord` row -- `FileRecord` rows are never deleted in this design,
    so this is fine and must not be reported as a failure.
    """
    session_maker = session_factory(engine)
    with session_maker() as session:
        file = FileRecord(
            public_id=new_public_id('fil'),
            relative_path='gone.mp3',
            size_bytes=1,
            mtime_ns=1,
            extension='.mp3',
            is_present=False,
        )
        session.add(file)
        session.flush()
        track = Track(
            public_id=new_public_id('trk'), status=TrackStatus.ACTIVE, preferred_file_id=file.id
        )
        session.add(track)
        session.commit()

    result = doctor.check_preferred_files_exist(session_maker)
    assert result.status == CheckStatus.PASS


def test_preferred_file_exists_check_fails_on_dangling_reference(engine: Engine) -> None:
    """A `preferred_file_id` with no matching `files.id` row at all should be
    impossible in normal operation (the column has a `files.id` foreign key,
    and this engine enforces `PRAGMA foreign_keys = ON`) -- so, like the
    active-track-files check above, this hand-crafts the violation by
    disabling FK enforcement on a raw connection just long enough to write
    the dangling reference, purely to prove the check's query is correct.
    """
    session_maker = session_factory(engine)
    with session_maker() as session:
        track = Track(public_id=new_public_id('trk'), status=TrackStatus.ACTIVE)
        session.add(track)
        session.commit()
        track_id = track.id

    with engine.connect() as conn:
        conn.execute(text('PRAGMA foreign_keys = OFF'))
        conn.execute(
            text('UPDATE tracks SET preferred_file_id = :fid WHERE id = :tid'),
            {'fid': 999_999, 'tid': track_id},
        )
        conn.commit()

    result = doctor.check_preferred_files_exist(session_maker)
    assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# a healthy, freshly-scanned library -> everything PASS
# ---------------------------------------------------------------------------


def test_healthy_fixture_library_passes_every_check(tmp_path: Path) -> None:
    music_root = tmp_path / 'srcaudio'
    music_root.mkdir()
    data_root = tmp_path / 'data'
    _run_alembic_upgrade(music_root, data_root)
    database_url = f"sqlite:///{data_root / 'catalog.sqlite'}"

    config = DjlibConfig(music_root=music_root, data_root=data_root)
    engine = create_engine(database_url, future=True)
    session_maker: sessionmaker[Session] = session_factory(engine)

    real_open = pathlib.Path.open

    def fake_open(self: pathlib.Path, mode: str = 'r', *args: object, **kwargs: object):
        if doctor.PROBE_PREFIX in self.name and 'x' in mode and music_root in self.parents:
            raise PermissionError('simulated read-only bind mount')
        return real_open(self, mode, *args, **kwargs)

    import pytest as _pytest  # local import to build a scoped MonkeyPatch

    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(pathlib.Path, 'open', fake_open)
        report = Doctor(config, session_maker).run()
    finally:
        mp.undo()
        engine.dispose()

    failing = [c for c in report.checks if c.status != CheckStatus.PASS]
    assert failing == []
    assert report.ok is True
