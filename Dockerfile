# One image, two roles:
#   executor (default):  docker run -p 8080:8080 -e EXUVIA_EXECUTOR_TOKEN=... exuvia
#   control plane:       docker run -p 7420:7420 -v $PWD/my-project:/project exuvia \
#                          exuvia serve /project --host 0.0.0.0 --port 7420
#
# Nodes need their own packages in the *executor* image. Extend it:
#   FROM exuvia
#   RUN uv pip install --python /opt/venv/bin/python scipy statsmodels yfinance

FROM node:24-slim AS ui
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
RUN uv sync --frozen --no-editable && rm -rf /root/.cache

RUN useradd --create-home --uid 10001 exuvia
USER exuvia
EXPOSE 8080 7420
CMD ["exuvia", "executor", "--host", "0.0.0.0", "--port", "8080"]
