"""Shared progress-reporting hook for long-running commands (`scan`,
`duplicates run`, `rebuild`): a plain callback so service layers stay
decoupled from how (or whether) progress is actually displayed.

Callbacks receive a short, human-readable stage name -- never a full
`music_root`-relative file path (those can be long and numerous enough to
flood a terminal; see the CLI's own progress-bar rendering in `cli.py`).
"""

from typing import Protocol


class ProgressReporter(Protocol):
    def __call__(self, stage: str, current: int, total: int) -> None: ...


def null_progress(stage: str, current: int, total: int) -> None:
    """Default no-op reporter: every progress-taking service works unchanged
    when the caller doesn't pass one (e.g. every existing test)."""
