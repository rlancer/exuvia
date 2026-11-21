# One image, three roles:
#   executor (default):  docker run -p 8080:8080 -e EXUVIA_EXECUTOR_TOKEN=... exuvia
#   control plane:       docker run -p 7420:7420 -v $PWD/my-project:/project exuvia \
#                          exuvia serve /project --host 0.0.0.0 --port 7420
#   headless hub:        docker run -p 7420:7420 -v exuvia-data:/data exuvia \
#                          exuvia hub /data --host 0.0.0.0 --port 7420
#
# `npm run build` also builds the views runtime (exuvia/static/view-runtime/),
# and static/ is copied in before `uv sync`, so the installed wheel ships it and
# every role serves /api/view-runtime/runtime.js. tests/e2e/container_e2e.py
# builds this image and drives the hub role over HTTP.
#
# Nodes need their own packages in the *executor* image. Extend it (the venv
# belongs to root, so install as root and switch back):
#   FROM ghcr.io/rlancer/exuvia:0.3
#   USER root
#   RUN uv pip install --python /opt/venv/bin/python scipy statsmodels yfinance
#   USER exuvia

# The UI bundle is the same on every platform, so it builds natively even in a
# multi-arch build (see .github/workflows/image.yml).
FROM --platform=$BUILDPLATFORM node:24-slim AS ui
WORKDIR /src/ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build   # writes ../exuvia/static

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.2 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    EXUVIA_EXECUTOR_DIR=/tmp/exuvia-executor
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY exuvia/ ./exuvia/
COPY --from=ui /src/exuvia/static ./exuvia/static
# The build context has no .git, so the version comes in as an argument
# (image.yml passes the release tag's); a plain `docker build` gets 0.0.0.
ARG EXUVIA_VERSION=0.0.0
RUN SETUPTOOLS_SCM_PRETEND_VERSION=$EXUVIA_VERSION uv sync --frozen --no-editable && rm -rf /root/.cache

# /data: a writable home for `exuvia hub /data` (mount a volume over it to keep it).
RUN useradd --create-home --uid 10001 exuvia && mkdir /data && chown exuvia:exuvia /data
USER exuvia
EXPOSE 8080 7420
CMD ["exuvia", "executor", "--host", "0.0.0.0", "--port", "8080"]
