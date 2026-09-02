"""Shared test safety net.

No test in this suite may reach the real Apple Notes app. Every Notes
interaction is supposed to go through a hand-written fake, but a wiring
mistake -- a new code path that constructs a real runner, or a default that
changes underneath an existing test -- can silently turn a unit test into
something that creates folders and notes in the developer's own Notes library.
That has happened once; this fixture makes it impossible to happen quietly
again.

Any `osascript` invocation, or any `open` handing a file to Notes, fails the
test loudly instead of running.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any

import pytest


class RealNotesAutomationAttempted(AssertionError):
    """A test tried to drive the real Notes app."""


_FORBIDDEN_EXECUTABLES = frozenset({"osascript"})


def _is_notes_automation(command: object) -> bool:
    if isinstance(command, str):
        parts = command.split()
    elif isinstance(command, Sequence):
        parts = [str(part) for part in command]
    else:
        return False
    if not parts:
        return False
    executable = parts[0].rsplit("/", maxsplit=1)[-1]
    if executable in _FORBIDDEN_EXECUTABLES:
        return True
    return executable == "open" and any("Notes" in part for part in parts[1:])


#: Every way this codebase could start a subprocess. Guarding only
#: `subprocess.run` would leave the net with holes: a future call through
#: `Popen` or `os.system` would sail straight past it and reach the real app.
_GUARDED_SUBPROCESS_CALLS = ("run", "Popen", "call", "check_call", "check_output")


@pytest.fixture(autouse=True)
def _block_real_notes_automation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that shells out to Notes instead of using a fake."""

    def guard(name: str, real: Any) -> Any:
        def guarded(command: object, *args: Any, **kwargs: Any) -> Any:
            if _is_notes_automation(command):
                raise RealNotesAutomationAttempted(
                    "this test tried to drive the real Notes app "
                    f"({command!r}) via subprocess.{name}. Use a fake runner instead."
                )
            return real(command, *args, **kwargs)

        return guarded

    for name in _GUARDED_SUBPROCESS_CALLS:
        monkeypatch.setattr(subprocess, name, guard(name, getattr(subprocess, name)))
