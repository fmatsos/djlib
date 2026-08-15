"""Persistent, structured logging (design §26).

`configure_logging` is idempotent and safe to call once per CLI invocation
(the top-level Typer callback in `cli.py` does exactly that): it always
attaches a console handler, whose level is controlled by `-v`/`-vv`/
`--log-level`, and a rotating file handler writing to
`/data/logs/djlib.log`.

The file handler's level is fixed at INFO regardless of console verbosity --
it is the durable, always-on record (`djlib runs show`/postmortem debugging
read it, not the console), while `-v`/`-vv`/`--log-level DEBUG` only widen
what a human watching the terminal sees right now. Every record -- from
either handler -- carries `command`/`run_id`/`ref_id` fields (defaulted to
`-` when a log call doesn't supply them via `extra=`), timestamp and level,
so a log line can always be tied back to the CLI invocation and, where
applicable, the file or duplicate-group it concerns.
"""

import logging
import logging.handlers
from pathlib import Path

from djlib.config import DjlibConfig

LOGGER_NAME = 'djlib'
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5

FILE_LOG_FORMAT = (
    '%(asctime)s %(levelname)s command=%(command)s run_id=%(run_id)s '
    'ref_id=%(ref_id)s %(message)s'
)
CONSOLE_LOG_FORMAT = '%(levelname)s %(message)s'

_CONTEXT_DEFAULTS = {'command': '-', 'run_id': '-', 'ref_id': '-'}


class _ContextDefaultsFilter(logging.Filter):
    """Fills in `command`/`run_id`/`ref_id` for log calls that don't set them.

    Without this, `FILE_LOG_FORMAT` would raise `KeyError` on any log call
    that omits `extra={'command': ..., 'run_id': ..., 'ref_id': ...}`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key, default in _CONTEXT_DEFAULTS.items():
            if not hasattr(record, key):
                setattr(record, key, default)
        return True


def _console_level(verbosity: int, log_level: str | None) -> int:
    if log_level is not None:
        return logging.getLevelName(log_level.upper())
    if verbosity >= 2:
        return logging.DEBUG
    if verbosity >= 1:
        return logging.INFO
    return logging.WARNING


def configure_logging(
    config: DjlibConfig, verbosity: int = 0, log_level: str | None = None
) -> logging.Logger:
    """(Re)configure the `djlib` logger for one CLI invocation.

    Clears and replaces any handlers from a prior call in the same process
    (tests invoke the CLI repeatedly against different configs) rather than
    accumulating duplicate handlers.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    # `logging.config.fileConfig` (as alembic's `env.py` calls whenever a
    # real migration runs in-process, `disable_existing_loggers` defaults to
    # True) disables every *existing* logger it doesn't know about --
    # including 'djlib' and any 'djlib.*' child such as 'djlib.runs'.
    # Reconfiguring must always re-enable the whole tree rather than leaving
    # a stale disablement from some earlier, unrelated `fileConfig` call in
    # the same process.
    logger.disabled = False
    for name, existing in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(existing, logging.Logger) and (
            name == LOGGER_NAME or name.startswith(f'{LOGGER_NAME}.')
        ):
            existing.disabled = False
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    context_filter = _ContextDefaultsFilter()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(_console_level(verbosity, log_level))
    console_handler.setFormatter(logging.Formatter(CONSOLE_LOG_FORMAT))
    console_handler.addFilter(context_filter)
    logger.addHandler(console_handler)

    log_path = _log_path(config)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding='utf-8'
        )
    except OSError as exc:
        # `djlib doctor` must itself be able to run (and report the problem)
        # against a broken/unwritable `/data` -- console-only logging here,
        # rather than a crash, is what makes that possible.
        console_handler.handle(
            logger.makeRecord(
                LOGGER_NAME, logging.WARNING, __file__, 0,
                f'could not open log file {log_path}: {exc}', (), None,
            )
        )
        return logger

    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(FILE_LOG_FORMAT))
    file_handler.addFilter(context_filter)
    logger.addHandler(file_handler)

    return logger


def _log_path(config: DjlibConfig) -> Path:
    return config.data_root / 'logs' / 'djlib.log'
