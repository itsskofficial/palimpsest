# palimpsest — the review app and the worker in one image.
#
# Multi-stage so the runtime layer carries no compiler and no build cache.
#
#   docker build -f deploy/Dockerfile -t palimpsest .
#   docker run --rm -p 8100:8100 -e PALIMPSEST_ALLOW_INSECURE=1 palimpsest
#
# The same image runs both processes; the compose file and the ECS task definition just
# override the command:
#
#   app  -> uvicorn palimpsest.serve.asgi:app   (the default CMD)
#   sync -> palimpsest sync

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install ".[anthropic,serve,postgres,aws,pdf,tabular]"

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Containers get their configuration from the environment, and a container
    # filesystem does not survive a redeploy — so JSON logs and a non-local bind are
    # defaults here rather than in the library.
    PALIMPSEST_HOST=0.0.0.0 \
    PALIMPSEST_PORT=8100 \
    PALIMPSEST_LOG_JSON=1 \
    PALIMPSEST_ENV=container \
    # Deliberate: a deployed instance proposes and does not write until you say so.
    # Turning this on is a change to the task definition, which is a reviewable act.
    PALIMPSEST_APPLY=0 \
    PALIMPSEST_AUTONOMY=none

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 palimpsest

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
USER palimpsest

EXPOSE 8100

# Hits /healthz, which deliberately does not touch the database — a liveness probe that
# fails on a database blip gets a healthy process killed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8100/healthz || exit 1

CMD ["uvicorn", "palimpsest.serve.asgi:app", "--host", "0.0.0.0", "--port", "8100", \
     "--workers", "2", "--timeout-keep-alive", "65"]
