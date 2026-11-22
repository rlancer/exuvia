# One image, three roles:
#   executor (default):  docker run -p 8080:8080 -e EXUVIA_EXECUTOR_TOKEN=... exuvia
#   control plane:       docker run -p 7420:7420 -v $PWD/my-project:/project exuvia \
#                          exuvia serve /project --host 0.0.0.0 --port 7420
#   hub:                 docker run -p 7420:7420 -v exuvia-data:/data exuvia \
#                          exuvia hub /data --host 0.0.0.0 --port 7420
#                        (every project's API, and its UI at /p/<id>/)
#
# `bun run build` also builds the views runtime (exuvia/static/view-runtime/),
# and static/ is copied in before `uv sync`, so the installed wheel ships it and
# every role serves /api/view-runtime/runtime.js. tests/e2e/container_e2e.py
# builds this image and drives the hub role over HTTP.
#
# Nodes import the packages their project lists in its pyproject.toml, which
# the image installs before they run. To build packages every project gets
# into an image of your own, install them with `uv pip install -r` from a
# pyproject.toml of the image's own, held at exuvia's versions (-c), never
# with `uv add` or `uv sync`: README.md, "Packages your nodes import".

# The UI bundle is the same on every platform, so it builds natively even in a
# multi-arch build (see .github/workflows/image.yml), with the bun mise.toml pins.
FROM --platform=$BUILDPLATFORM oven/bun:1.4.5-slim AS ui
WORKDIR /src/ui
COPY ui/package.json ui/bun.lock ui/bunfig.toml ./
RUN bun install --frozen-lockfile
COPY ui/ ./
RUN bun run build   # writes ../exuvia/static

# Wheels for packages PyPI has only as source on Linux, because they link a
# system library, built here so projects can list them without a compiler in
# the image: gssapi (Kerberos; under requests-kerberos, pyspnego, Trino,
# Impala, Hive and SQL Server drivers). The wheels land in /opt/exuvia/wheels,
# not in exuvia's environment: a project that lists the package installs it
# from there, and one that doesn't pays nothing. README.md, "Kerberos".
#
# The base is pinned by digest so this stage stays in the layer cache: arm64
# builds under QEMU (image.yml), where installing a compiler and building
# gssapi takes many minutes, so it must only run when GSSAPI_VERSION or this
# pin changes, not whenever python:3.12-slim is republished. The wheel links
# libgssapi_krb5.so.2, which the runtime stage installs from its own Debian.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS wheels
ARG GSSAPI_VERSION=1.12.0
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libkrb5-dev \
 && pip wheel --no-cache-dir --no-deps --wheel-dir /wheels "gssapi==${GSSAPI_VERSION}"

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.2 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    EXUVIA_EXECUTOR_DIR=/tmp/exuvia-executor \
    EXUVIA_DEPLOYED=1
# EXUVIA_DEPLOYED: the help menu tells people that whoever runs this server
# updates it (a new image), instead of a command for an install they don't have.
WORKDIR /app
# Dependencies first, in their own layer: it depends only on the lockfile, so
# it builds while the ui stage does and stays cached when only code changes
# (ci.yml keeps the layer cache between runs).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-editable && rm -rf /root/.cache
# libgssapi-krb5-2: the Kerberos library the gssapi wheel links (a few MB).
# After the dependencies, so adding it left their cached layer as it was.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgssapi-krb5-2 \
 && rm -rf /var/lib/apt/lists/*
COPY README.md LICENSE ./
COPY exuvia/ ./exuvia/
COPY --from=ui /src/exuvia/static ./exuvia/static
# The build context has no .git, so the version comes in as an argument
# (image.yml passes the release tag's); a plain `docker build` gets 0.0.0.
ARG EXUVIA_VERSION=0.0.0
RUN SETUPTOOLS_SCM_PRETEND_VERSION=$EXUVIA_VERSION uv sync --frozen --no-editable && rm -rf /root/.cache

# uv's system-wide settings (uv merges them under a project's own): find the
# wheels built above, and never build those packages from source. A newer
# release on PyPI is source only, and building it fails here (no compiler),
# so a project that doesn't pin gets the wheel, and one that pins a version
# the image lacks fails at once with uv's explanation. `uv pip` (projects'
# installs) reads only-binary; uv's project commands read no-build-package.
COPY --from=wheels /wheels /opt/exuvia/wheels
RUN mkdir -p /etc/uv && printf '%s\n' \
      'find-links = ["/opt/exuvia/wheels"]' \
      'no-build-package = ["gssapi"]' \
      '' \
      '[pip]' \
      'only-binary = ["gssapi"]' \
      > /etc/uv/uv.toml

# /data: a writable home for `exuvia hub /data` (mount a volume over it to keep it).
RUN useradd --create-home --uid 10001 exuvia && mkdir /data && chown exuvia:exuvia /data
USER exuvia
# uv installs the packages a project lists in its pyproject.toml before its
# nodes run (exuvia/packages.py), and exuvia picks uv's cache: a hub keeps it
# on its volume (/data/.exuvia/packages/.uv-cache), so installs survive
# restarts; elsewhere it is uv's default, or the temp dir when `docker run
# --user` leaves HOME pointing at a folder the user can't write.
EXPOSE 8080 7420
CMD ["exuvia", "executor", "--host", "0.0.0.0", "--port", "8080"]
