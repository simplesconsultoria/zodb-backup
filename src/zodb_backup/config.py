"""Configuration handling: environment variables and CLI flags to :class:`Settings`.

This is the single place environment variables are read. Precedence is
``CLI flag > environment variable > default``.

Every operation in this package takes a :class:`Settings` instance, so it can be
exercised in tests without going through Typer.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Any
from zodb_backup.errors import ConfigurationError

import os
import shlex


#: Values accepted as boolean true, case-insensitively.
TRUE_VALUES = frozenset({"true", "1", "yes", "on"})

#: Values accepted as boolean false, case-insensitively.
FALSE_VALUES = frozenset({"false", "0", "no", "off"})

DEFAULT_DATAFS = "/data/filestorage/Data.fs"
DEFAULT_BLOBSTORAGE = "/data/blobstorage"
DEFAULT_BACKUP_LOCATION = "/backups/filestorage"
DEFAULT_BLOB_BACKUP_LOCATION = "/backups/blobstorage"
DEFAULT_SNAPSHOT_LOCATION = "/backups/snapshots"
DEFAULT_BLOB_SNAPSHOT_LOCATION = "/backups/blobstoragesnapshots"


def parse_bool(value: str, *, name: str) -> bool:
    """Parse a boolean environment variable strictly.

    Anything outside :data:`TRUE_VALUES` / :data:`FALSE_VALUES` is a configuration
    error rather than a silent ``False`` — a typo in ``BACKUP_BLOBS`` must not
    quietly disable blob backups.

    :param value: raw string as read from the environment.
    :param name: variable name, used in the error message.
    :returns: the parsed boolean.
    :raises ConfigurationError: if the value is not a recognised boolean.
    """
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    accepted = ", ".join(sorted(TRUE_VALUES | FALSE_VALUES))
    raise ConfigurationError(
        f"{name}={value!r} is not a boolean; accepted values are: {accepted}"
    )


def parse_int(value: str, *, name: str, minimum: int = 0) -> int:
    """Parse a non-negative integer environment variable.

    :param value: raw string as read from the environment.
    :param name: variable name, used in the error message.
    :param minimum: smallest accepted value.
    :returns: the parsed integer.
    :raises ConfigurationError: if the value is not an integer or is below
        ``minimum``.
    """
    try:
        parsed = int(value.strip())
    except ValueError:
        raise ConfigurationError(f"{name}={value!r} is not an integer") from None
    if parsed < minimum:
        raise ConfigurationError(f"{name}={value!r} must be >= {minimum}")
    return parsed


@dataclass(frozen=True, slots=True)
class Settings:
    """Fully resolved, immutable configuration for a single run.

    Instances are produced by :meth:`from_env` and never mutated afterwards;
    use :func:`dataclasses.replace` to derive a variant.
    """

    datafs: Path
    blobstorage: Path | None
    backup_location: Path
    blob_backup_location: Path
    snapshot_location: Path
    blob_snapshot_location: Path
    full: bool
    quick: bool
    gzip: bool
    keep: int
    backup_blobs: bool
    only_blobs: bool
    archive_blob: bool
    compress_blob: bool
    use_rsync: bool
    rsync_options: tuple[str, ...]
    pre_command: str | None
    post_command: str | None
    assume_yes: bool
    debug: bool
    quiet: bool

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> Settings:
        """Build settings from the environment, then apply CLI overrides.

        :param environ: mapping to read variables from; defaults to
            :data:`os.environ`. Passing an explicit mapping keeps tests free of
            process-global state.
        :param overrides: CLI-provided values keyed by field name. Entries whose
            value is ``None`` are ignored, so an unset flag falls through to the
            environment variable and then to the default.
        :returns: a validated :class:`Settings` instance.
        :raises ConfigurationError: if any value is unparsable or the resulting
            combination is contradictory.
        """
        env = os.environ if environ is None else environ

        def flag(name: str, default: bool) -> bool:
            raw = env.get(name)
            return default if raw is None else parse_bool(raw, name=name)

        def path(name: str, default: str) -> Path:
            return Path(env.get(name, default))

        blobstorage_raw = env.get("BLOBSTORAGE", DEFAULT_BLOBSTORAGE).strip()

        settings = cls(
            datafs=path("DATAFS", DEFAULT_DATAFS),
            # An empty BLOBSTORAGE means "filestorage only".
            blobstorage=Path(blobstorage_raw) if blobstorage_raw else None,
            backup_location=path("BACKUP_LOCATION", DEFAULT_BACKUP_LOCATION),
            blob_backup_location=path(
                "BLOB_BACKUP_LOCATION", DEFAULT_BLOB_BACKUP_LOCATION
            ),
            snapshot_location=path("SNAPSHOT_LOCATION", DEFAULT_SNAPSHOT_LOCATION),
            blob_snapshot_location=path(
                "BLOB_SNAPSHOT_LOCATION", DEFAULT_BLOB_SNAPSHOT_LOCATION
            ),
            full=flag("FULL", False),
            quick=flag("QUICK", True),
            gzip=flag("GZIP", True),
            keep=parse_int(env.get("KEEP", "2"), name="KEEP"),
            backup_blobs=flag("BACKUP_BLOBS", True),
            only_blobs=flag("ONLY_BLOBS", False),
            archive_blob=flag("ARCHIVE_BLOB", False),
            compress_blob=flag("COMPRESS_BLOB", False),
            use_rsync=flag("USE_RSYNC", True),
            rsync_options=tuple(shlex.split(env.get("RSYNC_OPTIONS", ""))),
            pre_command=env.get("PRE_COMMAND") or None,
            post_command=env.get("POST_COMMAND") or None,
            assume_yes=flag("ASSUME_YES", False),
            debug=flag("DEBUG", False),
            quiet=flag("QUIET", False),
        )

        if overrides:
            applied = {k: v for k, v in overrides.items() if v is not None}
            unknown = applied.keys() - set(cls.__dataclass_fields__)
            if unknown:
                raise ConfigurationError(
                    f"unknown setting(s): {', '.join(sorted(unknown))}"
                )
            settings = replace(settings, **applied)

        settings.validate()
        return settings

    def validate(self) -> None:
        """Reject combinations that cannot be honoured.

        Contradictions are surfaced as configuration errors (exit 2) rather than
        being silently resolved, so a misconfigured container fails loudly on its
        first scheduled run instead of producing a backup nobody asked for.

        :raises ConfigurationError: if the settings are self-contradictory.
        """
        if self.only_blobs and not self.backup_blobs:
            raise ConfigurationError(
                "ONLY_BLOBS=true requires BACKUP_BLOBS=true; "
                "the combination would back up nothing"
            )
        if self.only_blobs and self.blobstorage is None:
            raise ConfigurationError("ONLY_BLOBS=true requires BLOBSTORAGE to be set")
        if self.compress_blob and not self.archive_blob:
            raise ConfigurationError(
                "COMPRESS_BLOB=true only applies with ARCHIVE_BLOB=true"
            )
        if self.debug and self.quiet:
            raise ConfigurationError("DEBUG and QUIET are mutually exclusive")

    @property
    def blobs_enabled(self) -> bool:
        """Whether this run should touch blobs at all.

        ``BACKUP_BLOBS`` is only meaningful when a blobstorage path is configured;
        an empty ``BLOBSTORAGE`` degrades the run to filestorage-only.
        """
        return self.backup_blobs and self.blobstorage is not None
