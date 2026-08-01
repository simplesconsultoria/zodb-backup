"""Single entry point for running external commands.

Every external process this package starts — ``rsync`` today, user hooks later —
goes through :func:`run`. Centralising it means one place logs the exact command
line, one place decides how failures are reported, and no caller has to remember
to check a return code.

Commands are executed without a shell, from an argument list, so nothing has to
be quoted and a path containing spaces or quotes cannot change the command's
meaning.
"""

from collections.abc import Sequence
from pathlib import Path
from zodb_backup.errors import CommandError

import logging
import shutil
import subprocess


logger = logging.getLogger("zodb_backup.commands")


def require(program: str) -> str:
    """Resolve an external program, failing with a clear message if absent.

    :param program: program name to look up on ``PATH``.
    :returns: the resolved absolute path.
    :raises CommandError: if the program is not installed.
    """
    resolved = shutil.which(program)
    if resolved is None:
        raise CommandError(
            f"{program!r} is not installed or not on PATH; "
            "install it or choose a mode that does not need it"
        )
    return resolved


def run(argv: Sequence[str | Path], *, cwd: Path | None = None) -> str:
    """Run a command and return its standard output.

    :param argv: the command and its arguments. Never a string: the command runs
        without a shell, so no quoting or escaping is involved.
    :param cwd: working directory for the command.
    :returns: the command's standard output, decoded and stripped.
    :raises CommandError: if the command exits non-zero, carrying its stderr.
    """
    command = [str(part) for part in argv]
    logger.debug("running: %s", " ".join(command))
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"{command[0]!r} is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise CommandError(
            f"command failed with exit status {exc.returncode}: "
            f"{' '.join(command)}" + (f"\n{detail}" if detail else "")
        ) from exc
    return completed.stdout.strip()


def run_shell(command: str, *, label: str) -> str:
    """Run a user-supplied command line through the shell.

    This is the one place a shell is used, and it exists only for the
    ``PRE_COMMAND`` / ``POST_COMMAND`` hooks. Those are written by the operator
    in their own deployment configuration and are expected to support pipes,
    redirection and ``&&``, so passing them to a shell is the point rather than a
    risk: the command is already as trusted as the environment it comes from.

    :param command: the command line to execute.
    :param label: human-readable name used in log and error messages.
    :returns: the command's standard output, decoded and stripped.
    :raises CommandError: if the command exits non-zero.
    """
    logger.info("running %s: %s", label, command)
    completed = subprocess.run(
        command,
        shell=True,  # deliberate, and the only shell use; see the docstring
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.stdout:
        logger.info("%s output: %s", label, completed.stdout.strip())
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise CommandError(
            f"{label} failed with exit status {completed.returncode}"
            + (f": {detail}" if detail else "")
        )
    return completed.stdout.strip()
