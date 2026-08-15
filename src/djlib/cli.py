import os
from pathlib import Path

import typer

from djlib.config import DjlibConfig
from djlib.db.engine import create_engine_for_config
from djlib.db.session import session_factory
from djlib.scan.service import ScanService

app = typer.Typer(no_args_is_help=True, help='Local DJ-library catalogue and deduplication tool.')


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
