"""Full catalogue rebuild (design §25/§33, Task 15's own literal Step 6).

`RebuildService.rebuild()` is the concrete proof of the milestone's central
promise: the catalogue is fully reconstructible from `/music` plus
`/data/curation/events.jsonl` alone. Sequence:

    health-check music_root
    -> back up the current catalog.sqlite (retained even on success)
    -> create/migrate a fresh, empty database in place
    -> full scan of music_root
    -> replay events.jsonl (`curation/replay.py`)
    -> run doctor's DB invariants as a final check

Never touches `music_root` (`.claude/rules/source-read-only.md`) -- every
step above only *reads* from it (the health check, the scan). Aborts before
any destructive step if the health check fails.
"""

import datetime as dt
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from djlib.config import DjlibConfig
from djlib.curation.replay import ReplaySummary
from djlib.curation.replay import CurationReplay
from djlib.db.engine import create_engine_for_config
from djlib.db.session import session_factory
from djlib.doctor import CheckStatus, Doctor, check_music_root_exists
from djlib.scan.service import ScanService, ScanSummary

_REPO_ROOT = Path(__file__).resolve().parents[3]


class RebuildError(Exception):
    """Rebuild was aborted before touching anything destructive."""


@dataclass(frozen=True)
class RebuildSummary:
    backup_path: Path | None
    scan_summary: ScanSummary
    replay_summary: ReplaySummary
    invariants_ok: bool
    failed_checks: tuple[str, ...]


class RebuildService:
    def __init__(self, config: DjlibConfig) -> None:
        self._config = config

    def rebuild(self) -> RebuildSummary:
        self._health_check_music_root()
        backup_path = self._backup_database()
        self._recreate_database()

        engine = create_engine_for_config(self._config)
        try:
            session_maker = session_factory(engine)
            scan_summary = ScanService(self._config, session_maker).scan(full=True)

            events_path = self._config.data_root / 'curation' / 'events.jsonl'
            with session_maker() as session:
                replay_summary = CurationReplay(self._config, session).replay(events_path)

            report = Doctor(self._config, session_maker).run()
        finally:
            engine.dispose()

        failed_checks = tuple(
            check.name for check in report.checks if check.status != CheckStatus.PASS
        )
        return RebuildSummary(
            backup_path=backup_path,
            scan_summary=scan_summary,
            replay_summary=replay_summary,
            invariants_ok=not failed_checks,
            failed_checks=failed_checks,
        )

    def _health_check_music_root(self) -> None:
        result = check_music_root_exists(self._config)
        if result.status != CheckStatus.PASS:
            raise RebuildError(f'aborting rebuild before touching anything: {result.message}')

    def _database_path(self) -> Path:
        return self._config.data_root / 'catalog.sqlite'

    def _backup_database(self) -> Path | None:
        db_path = self._database_path()
        if not db_path.exists():
            return None

        # Fold WAL content into the main file first so the backup is a
        # single, self-consistent snapshot rather than a main file that
        # depends on a sidecar `-wal` file to be meaningful.
        engine = create_engine_for_config(self._config)
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            engine.dispose()

        timestamp = dt.datetime.now(dt.UTC).strftime('%Y%m%d-%H%M%S')
        backup_path = self._config.data_root / f'catalog.sqlite.pre-rebuild-{timestamp}'
        shutil.copy2(db_path, backup_path)
        return backup_path

    def _recreate_database(self) -> None:
        db_path = self._database_path()
        for suffix in ('', '-wal', '-shm'):
            candidate = db_path.parent / f'{db_path.name}{suffix}'
            if candidate.exists():
                candidate.unlink()

        self._config.data_root.mkdir(parents=True, exist_ok=True)
        alembic_config_path = self._config.data_root / '.rebuild-alembic-config.toml'
        alembic_config_path.write_text(
            f'[paths]\nmusic_root = "{self._config.music_root}"\n'
            f'data_root = "{self._config.data_root}"\n',
            encoding='utf-8',
        )
        try:
            subprocess.run(
                [sys.executable, '-m', 'alembic', 'upgrade', 'head'],
                cwd=_REPO_ROOT,
                env={**os.environ, 'DJLIB_CONFIG': str(alembic_config_path)},
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RebuildError(
                f'failed to migrate a fresh database: {exc.stderr or exc.stdout}'
            ) from exc
        finally:
            alembic_config_path.unlink(missing_ok=True)
