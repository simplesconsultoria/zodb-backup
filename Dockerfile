ARG PYTHON_VERSION=3.14
FROM python:${PYTHON_VERSION}-slim-bookworm AS base
FROM base AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /src

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN <<EOT
  set -e
  uv build --wheel --out-dir /dist
EOT

FROM base

# Build-time facts. VERSION defaults to the packaged version; the rest are only
# known to whoever runs the build, so they fall back to "unknown" rather than
# claiming something untrue.
ARG VERSION=0.0.0
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG PYTHON_VERSION

LABEL org.opencontainers.image.title="zodb-backup" \
      org.opencontainers.image.description="Container-native backup and restore for ZODB FileStorage and blobstorage" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.url="https://github.com/simplesconsultoria/zodb-backup" \
      org.opencontainers.image.source="https://github.com/simplesconsultoria/zodb-backup" \
      org.opencontainers.image.documentation="https://github.com/simplesconsultoria/zodb-backup#readme" \
      org.opencontainers.image.licenses="GPL-2.0-only" \
      org.opencontainers.image.vendor="Simples Consultoria" \
      org.opencontainers.image.authors="Simples Consultoria <contato@simplesconsultoria.com.br>" \
      org.opencontainers.image.base.name="docker.io/library/python:${PYTHON_VERSION}-slim-bookworm"

# rsync is what makes hard-linked blob backups possible. GNU tar is deliberately
# not installed: archives are written with Python's tarfile.
RUN <<EOT
  set -e
  apt-get update
  apt-get install --yes --no-install-recommends rsync
  rm -rf /var/lib/apt/lists/*
EOT

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=builder /dist/*.whl /tmp/
RUN <<EOT
  set -e
  uv pip install --system --no-cache /tmp/*.whl
  rm -rf /tmp/*.whl
EOT

# Run as a non-root user. The uid is arbitrary; override it with `--user` (or
# compose's `user:`) to match whoever owns the data being backed up — the
# official Plone images use 500.
RUN useradd --create-home --uid 500 --user-group zodb
USER 500:500

# Paths match the layout of the official plone/plone-zeo image.
ENV DATAFS=/data/filestorage/Data.fs \
    BLOBSTORAGE=/data/blobstorage \
    BACKUP_LOCATION=/backups/filestorage \
    BLOB_BACKUP_LOCATION=/backups/blobstorage \
    SNAPSHOT_LOCATION=/backups/snapshots \
    BLOB_SNAPSHOT_LOCATION=/backups/blobstoragesnapshots

VOLUME ["/data", "/backups"]

ENTRYPOINT ["zodb-backup"]
CMD ["backup"]
