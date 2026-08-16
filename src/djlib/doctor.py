"""`djlib doctor` health checks (design §27).

Each check is a small, independently testable module-level function
returning a `CheckResult`; `Doctor.run()` just calls them in order and
collects the results into a `DoctorReport`. Keeping them as free functions
(rather than only private methods) lets tests monkeypatch an individual
check -- e.g. `shutil.which` for a missing executable, or the BLAKE3 check
itself, since the package is actually installed in this environment and
can't be made to fail import for real.
"""

import contextlib
import importlib
import os
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from djlib.config import DjlibConfig
from djlib.curation.journal import CurationJournal
from djlib.db.models import AppState, CurationEvent, FileRecord, Track, TrackFile

REQUIRED_EXECUTABLES = ('exiftool', 'ffprobe', 'fpcalc')
PROBE_PREFIX = '.djlib-doctor-probe-'


class CheckStatus(StrEnum):
    PASS = 'PASS'
    FAIL = 'FAIL'


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    message: str


@dataclass(frozen=True)
class DoctorReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.status == CheckStatus.PASS for check in self.checks)


def check_music_root_exists(config: DjlibConfig) -> CheckResult:
    if config.music_root.is_dir():
        return CheckResult(
            'music_root_exists', CheckStatus.PASS, f'{config.music_root} exists'
        )
    return CheckResult(
        'music_root_exists', CheckStatus.FAIL, f'{config.music_root} does not exist'
    )


def check_music_root_read_only(config: DjlibConfig) -> CheckResult:
    """A controlled, randomized-name write probe under `music_root`.

    Per `.claude/rules/source-read-only.md`: the probe name is a fresh
    uuid4 hex, re-rolled in the vanishingly unlikely case it already exists,
    so this can never collide with -- let alone overwrite -- a real media
    file. `open(..., 'x')` is exclusive-create, so even a name collision
    would raise rather than truncate an existing file. If creation
    unexpectedly succeeds, the probe file is deleted immediately and the
    check is a FAIL; if it fails (permission error / read-only filesystem),
    that failure is exactly what a PASS looks like here.
    """
    music_root = config.music_root
    if not music_root.is_dir():
        return CheckResult(
            'music_root_read_only',
            CheckStatus.FAIL,
            f'{music_root} does not exist; cannot probe writability',
        )

    probe_path = music_root / f'{PROBE_PREFIX}{uuid.uuid4().hex}'
    while probe_path.exists():
        probe_path = music_root / f'{PROBE_PREFIX}{uuid.uuid4().hex}'

    try:
        with probe_path.open('x'):
            pass
    except OSError:
        return CheckResult(
            'music_root_read_only',
            CheckStatus.PASS,
            f'write attempt under {music_root} failed as expected',
        )

    probe_path.unlink()
    return CheckResult(
        'music_root_read_only',
        CheckStatus.FAIL,
        f'write attempt under {music_root} unexpectedly succeeded (probe file removed)',
    )


def check_data_root_writable(config: DjlibConfig) -> CheckResult:
    data_root = config.data_root
    probe_path = data_root / f'{PROBE_PREFIX}{uuid.uuid4().hex}'
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        with probe_path.open('x') as handle:
            handle.write('')
    except OSError as exc:
        return CheckResult(
            'data_root_writable', CheckStatus.FAIL, f'{data_root} is not writable: {exc}'
        )
    finally:
        with contextlib.suppress(OSError):
            probe_path.unlink()
    return CheckResult('data_root_writable', CheckStatus.PASS, f'{data_root} is writable')


def check_sqlite_readable(session_maker: sessionmaker[Session]) -> CheckResult:
    try:
        with session_maker() as session:
            session.execute(text('SELECT 1')).scalar_one()
    except Exception as exc:
        return CheckResult('sqlite_readable', CheckStatus.FAIL, f'SQLite is not readable: {exc}')
    return CheckResult('sqlite_readable', CheckStatus.PASS, 'SQLite is readable')


REPO_ROOT_MARKER_NAME = '.djlib-repo-root'


def _repo_root() -> Path:
    """Locates the checkout containing `alembic/`.

    `Path(__file__).resolve().parents[2]` only works for an editable/
    source-checkout install, where `__file__` still lives inside the real
    repo tree. A real (non-editable) install copies `doctor.py` into some
    venv's `site-packages`, with no `alembic/` directory anywhere nearby.
    Tried in order:

    1. `DJLIB_REPO_ROOT` env var, an explicit override.
    2. A `.djlib-repo-root` marker file at the venv root (`sys.prefix`),
       written by `infra/lxc/install-djlib.sh` to the checkout it manages.
       Unlike an env var, this is intrinsic to the running interpreter, so it
       works no matter how djlib ends up invoked -- including an interactive
       `pct enter` shell, which (being a non-login `lxc-attach` shell) never
       sources `/etc/profile` and so never sees `DJLIB_REPO_ROOT` either,
       even though `install-djlib.sh` did export it there.
    3. The `parents[2]` guess (editable installs).
    """
    override = os.environ.get('DJLIB_REPO_ROOT')
    if override:
        return Path(override)
    marker = Path(sys.prefix) / REPO_ROOT_MARKER_NAME
    if marker.is_file():
        return Path(marker.read_text(encoding='utf-8').strip())
    return Path(__file__).resolve().parents[2]


def _alembic_script_directory() -> ScriptDirectory:
    return ScriptDirectory(str(_repo_root() / 'alembic'))


def check_migrations_current(session_maker: sessionmaker[Session]) -> CheckResult:
    try:
        heads = set(_alembic_script_directory().get_heads())
        with session_maker() as session:
            context = MigrationContext.configure(session.connection())
            current = set(context.get_current_heads())
    except Exception as exc:
        return CheckResult(
            'migrations_current', CheckStatus.FAIL, f'could not determine migration state: {exc}'
        )
    if current == heads:
        return CheckResult(
            'migrations_current', CheckStatus.PASS, f'database is at head {sorted(heads)}'
        )
    return CheckResult(
        'migrations_current',
        CheckStatus.FAIL,
        f'database at revision(s) {sorted(current)}, expected head {sorted(heads)} '
        '(run `alembic upgrade head`)',
    )


def check_executable(name: str) -> CheckResult:
    check_name = f'executable_{name}'
    path = shutil.which(name)
    if path is None:
        return CheckResult(check_name, CheckStatus.FAIL, f'{name} was not found on PATH')
    return CheckResult(check_name, CheckStatus.PASS, f'{name} found at {path}')


def check_blake3() -> CheckResult:
    try:
        blake3 = importlib.import_module('blake3')
        blake3.blake3(b'djlib doctor').hexdigest()
    except Exception as exc:
        return CheckResult('blake3_available', CheckStatus.FAIL, f'BLAKE3 is not usable: {exc}')
    return CheckResult('blake3_available', CheckStatus.PASS, 'BLAKE3 is importable and hashes')


def check_curation_sequence(session_maker: sessionmaker[Session]) -> CheckResult:
    """`/data/curation/events.jsonl` is exported strictly up through
    `AppState.last_exported_curation_sequence` (`CurationJournal.export_pending`
    always advances the watermark to exactly the last sequence it wrote, design
    §25). So the single invariant this check needs is: does that watermark
    equal the highest `CurationEvent.sequence` actually committed in SQLite?
    A mismatch means some accepted curation event exists in SQLite that was
    never (or was incorrectly) exported to the durable journal -- a FAIL, not
    a WARN, since `curation-persistence.md`'s invariant is that the journal
    plus `/music` must be *sufficient* to reconstruct all curated state, and
    that guarantee is broken for any un-exported event.
    """
    try:
        with session_maker() as session:
            app_state = session.execute(
                select(AppState).order_by(AppState.id).limit(1)
            ).scalar_one_or_none()
            watermark = app_state.last_exported_curation_sequence if app_state else 0
            max_sequence = (
                session.execute(select(func.max(CurationEvent.sequence))).scalar() or 0
            )
    except Exception as exc:
        return CheckResult(
            'curation_sequence', CheckStatus.FAIL, f'could not read curation state: {exc}'
        )

    if watermark == max_sequence:
        return CheckResult(
            'curation_sequence',
            CheckStatus.PASS,
            f'journal watermark ({watermark}) matches SQLite ({max_sequence} curation events)',
        )
    return CheckResult(
        'curation_sequence',
        CheckStatus.FAIL,
        f'journal watermark ({watermark}) does not match SQLite '
        f'({max_sequence} curation events); run `djlib doctor --repair-journal`',
    )


def check_no_duplicate_active_track_files(session_maker: sessionmaker[Session]) -> CheckResult:
    """No `file_id` should have more than one active `TrackFile` row.

    Structurally impossible given `uq_track_files_one_active_per_file`
    (Task 2's partial unique index) -- this check exists anyway as
    defense-in-depth (design §27 asks for it explicitly), so a schema
    regression or a future migration weakening that index would still be
    caught here.
    """
    try:
        with session_maker() as session:
            offenders = session.execute(
                select(TrackFile.file_id)
                .where(TrackFile.is_active.is_(True))
                .group_by(TrackFile.file_id)
                .having(func.count() > 1)
            ).scalars().all()
    except Exception as exc:
        return CheckResult(
            'active_track_file_uniqueness',
            CheckStatus.FAIL,
            f'could not check active TrackFile rows: {exc}',
        )

    if not offenders:
        return CheckResult(
            'active_track_file_uniqueness',
            CheckStatus.PASS,
            'no file has more than one active TrackFile row',
        )
    return CheckResult(
        'active_track_file_uniqueness',
        CheckStatus.FAIL,
        f'file_id(s) with multiple active TrackFile rows: {sorted(offenders)}',
    )


def check_preferred_files_exist(session_maker: sessionmaker[Session]) -> CheckResult:
    """`Track.preferred_file_id`, where set, must reference a real `FileRecord`.

    `FileRecord` rows are never deleted in this design, so a preferred file
    that is simply absent from disk shows up as `is_present=False` on a
    *still-existing* row -- that resolves fine and is not a failure here.
    Only a `preferred_file_id` with no matching `files.id` row at all (which
    the `files.id` foreign key should already make impossible in normal
    operation) is a genuine FAIL.
    """
    try:
        with session_maker() as session:
            offenders = session.execute(
                select(Track.public_id, Track.preferred_file_id)
                .where(Track.preferred_file_id.is_not(None))
                .where(
                    ~select(FileRecord.id)
                    .where(FileRecord.id == Track.preferred_file_id)
                    .exists()
                )
            ).all()
    except Exception as exc:
        return CheckResult(
            'preferred_file_exists', CheckStatus.FAIL, f'could not check preferred files: {exc}'
        )

    if not offenders:
        return CheckResult(
            'preferred_file_exists',
            CheckStatus.PASS,
            'every preferred_file_id resolves to a FileRecord',
        )
    offending = ', '.join(f'{public_id} -> file {file_id}' for public_id, file_id in offenders)
    return CheckResult(
        'preferred_file_exists',
        CheckStatus.FAIL,
        f'preferred_file_id with no matching FileRecord row: {offending}',
    )


class Doctor:
    def __init__(self, config: DjlibConfig, session_maker: sessionmaker[Session]) -> None:
        self._config = config
        self._session_maker = session_maker

    def run(self, repair_journal: bool = False) -> DoctorReport:
        if repair_journal:
            with contextlib.suppress(Exception):
                # A broken/unmigrated database can't be repaired either --
                # `check_curation_sequence` below still reports that clearly.
                with self._session_maker() as session:
                    CurationJournal(self._config).export_pending(session)

        checks = [
            check_music_root_exists(self._config),
            check_music_root_read_only(self._config),
            check_data_root_writable(self._config),
            check_sqlite_readable(self._session_maker),
            check_migrations_current(self._session_maker),
            *(check_executable(name) for name in REQUIRED_EXECUTABLES),
            check_blake3(),
            check_curation_sequence(self._session_maker),
            check_no_duplicate_active_track_files(self._session_maker),
            check_preferred_files_exist(self._session_maker),
        ]
        return DoctorReport(checks=checks)
