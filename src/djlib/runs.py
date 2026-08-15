"""Cross-cutting `OperationRun` wrapper for CLI commands (design §26, Task 14).

`operation_run` is the one reusable context manager every relevant CLI
command (`scan`, `duplicates detect/analyze/run/report`, `duplicates
import-decisions`) uses instead of hand-rolling run bookkeeping six times.
It persists exactly one `OperationRun` row per invocation -- on success *or*
on an exception -- in its own transaction, separate from whatever the
command body does with its own session (the same "write the durable record
as a distinct step" shape as `CurationJournal.export_pending`, design §25):
a crash inside the body simply leaves no `OperationRun` row rather than a
half-written one.
"""

import contextlib
import datetime as dt
import logging
from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from djlib.db.enums import RunStatus
from djlib.db.models import OperationRun
from djlib.ids import new_public_id

logger = logging.getLogger('djlib.runs')


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


@dataclass
class RunHandle:
    """Yielded to a command body; it fills in `summary`/`error_summary`."""

    public_id: str
    summary: dict | None = None
    error_summary: str | None = None


@contextlib.contextmanager
def operation_run(
    session_maker: sessionmaker[Session], command: str, prefix: str
) -> Iterator[RunHandle]:
    """Wrap one CLI invocation's body in a persisted `OperationRun` row.

    `prefix` is one of the design's `scan_`/`dup_`/`report_`/`import_` run-ID
    prefixes (design §26); `command` is a short human-readable label such as
    `'duplicates import-decisions'` stored verbatim in `OperationRun.command`.
    """
    handle = RunHandle(public_id=new_public_id(prefix))
    started_at = _now()
    logger.info(
        'run started', extra={'command': command, 'run_id': handle.public_id}
    )
    try:
        yield handle
    except Exception as exc:
        _persist(
            session_maker, handle, command, RunStatus.FAILED, started_at,
            error=handle.error_summary or str(exc) or exc.__class__.__name__,
        )
        logger.error(
            'run failed', extra={'command': command, 'run_id': handle.public_id},
            exc_info=True,
        )
        raise
    else:
        _persist(session_maker, handle, command, RunStatus.SUCCESS, started_at)
        logger.info(
            'run finished', extra={'command': command, 'run_id': handle.public_id}
        )


def _persist(
    session_maker: sessionmaker[Session],
    handle: RunHandle,
    command: str,
    status: RunStatus,
    started_at: dt.datetime,
    error: str | None = None,
) -> None:
    with session_maker() as session:
        session.add(
            OperationRun(
                public_id=handle.public_id,
                command=command,
                status=status,
                started_at=started_at,
                ended_at=_now(),
                summary_json=handle.summary,
                error_summary=error,
            )
        )
        session.commit()
