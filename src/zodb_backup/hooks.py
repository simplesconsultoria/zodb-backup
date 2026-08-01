"""Operator-supplied commands run around a backup.

``PRE_COMMAND`` runs before anything is written; if it fails the run is abandoned
before a single backup file exists, so a failed pre-hook can never leave a
half-made backup behind. ``POST_COMMAND`` runs after the backup is complete and
is where off-site replication belongs (rclone, restic, borg).

A failing post-hook is reported and makes the process exit non-zero — an
off-site copy that did not happen is a real failure worth alerting on — but the
backup that was already written is left exactly where it is. Nothing is rolled
back.
"""

from zodb_backup.commands import run_shell
from zodb_backup.config import Settings

import logging


logger = logging.getLogger("zodb_backup.hooks")


def run_pre_command(settings: Settings) -> None:
    """Run the configured pre-backup hook, if any.

    :param settings: resolved settings for this run.
    :raises CommandError: if the hook fails, aborting the run before any backup
        file is written.
    """
    if not settings.pre_command:
        return
    run_shell(settings.pre_command, label="PRE_COMMAND")


def run_post_command(settings: Settings) -> None:
    """Run the configured post-backup hook, if any.

    :param settings: resolved settings for this run.
    :raises CommandError: if the hook fails. The completed backup is untouched.
    """
    if not settings.post_command:
        return
    run_shell(settings.post_command, label="POST_COMMAND")
