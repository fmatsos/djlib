from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

import pytest
import typer

from djlib import cli
from djlib.config import DjlibConfig
from djlib.db.enums import RunStatus
from djlib.db.models import OperationRun
from djlib.db.session import session_factory
from djlib.logging import configure_logging


def _operation_runs(session_maker: sessionmaker[Session]) -> list[OperationRun]:
    with session_maker() as session:
        return list(session.execute(select(OperationRun)).scalars())


def _log_text(config: DjlibConfig) -> str:
    return (config.data_root / 'logs' / 'djlib.log').read_text(encoding='utf-8')


def test_scan_persists_one_operation_run_and_logs_it(
    config: DjlibConfig, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.music_root.mkdir(parents=True)
    monkeypatch.setattr(cli, '_load_config', lambda: config)
    configure_logging(config)

    cli.scan(full=False)

    session_maker = session_factory(engine)
    runs = _operation_runs(session_maker)
    assert len(runs) == 1
    run = runs[0]
    assert run.command == 'scan'
    assert run.status == RunStatus.SUCCESS
    assert run.started_at is not None
    assert run.ended_at is not None
    assert run.started_at <= run.ended_at
    assert run.summary_json is not None
    assert run.summary_json['files_seen'] == 0
    assert run.error_summary is None

    log_text = _log_text(config)
    assert 'scan' in log_text
    assert run.public_id in log_text
    assert run.public_id.startswith('scan_')


def test_duplicates_detect_persists_operation_run_with_dup_prefix(
    config: DjlibConfig, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.music_root.mkdir(parents=True)
    monkeypatch.setattr(cli, '_load_config', lambda: config)
    configure_logging(config)

    cli.duplicates_detect()

    session_maker = session_factory(engine)
    runs = _operation_runs(session_maker)
    assert len(runs) == 1
    run = runs[0]
    assert run.command == 'duplicates detect'
    assert run.status == RunStatus.SUCCESS
    assert run.public_id.startswith('dup_')
    assert run.summary_json == {'groups': 0}

    log_text = _log_text(config)
    assert 'duplicates detect' in log_text or run.public_id in log_text


def test_failed_command_still_persists_one_failed_operation_run(
    config: DjlibConfig, engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config.music_root.mkdir(parents=True)
    monkeypatch.setattr(cli, '_load_config', lambda: config)
    configure_logging(config)

    missing_path = tmp_path / 'does-not-exist.json'
    with pytest.raises(typer.Exit):
        cli.duplicates_import_decisions(path=missing_path)

    session_maker = session_factory(engine)
    runs = _operation_runs(session_maker)
    assert len(runs) == 1
    run = runs[0]
    assert run.command == 'duplicates import-decisions'
    assert run.status == RunStatus.FAILED
    assert run.public_id.startswith('import_')
    assert run.error_summary is not None
    assert 'does-not-exist.json' in run.error_summary

    log_text = _log_text(config)
    assert run.public_id in log_text
