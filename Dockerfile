# AWS Lambda Web Adapter: a Lambda extension that bridges the Lambda Runtime API
# to an ordinary HTTP server, so this stays a plain uvicorn app -- no Mangum, no
# handler function, no application code aware of Lambda at all. The same image
# runs under `docker run` and under Lambda.
#
# x86_64 to match the other two services; Graviton would be ~20% cheaper, which
# is nothing inside the always-free Lambda tier, and story-labeling-api cannot
# go arm64 at all (hdbscan publishes no aarch64 wheel).
ARG LWA_ARCH=x86_64
FROM public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0-${LWA_ARCH} AS lambda-adapter


# ---------------------------------------------------------------- build stage
# Separate from the runtime stage so uv, pip's cache and the build tooling never
# reach the shipped image. It is not only size: the runtime image having no
# package manager is one less thing for a compromised request handler to use.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_NO_SYNC=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
COPY app ./app

RUN uv sync --frozen --no-dev


# -------------------------------------------------------------- runtime stage
FROM python:3.12-slim

COPY --from=lambda-adapter /lambda-adapter /opt/extensions/lambda-adapter

# NO ffmpeg. It was installed here but never needed: podcast clips are WAV, and
# pydub reads and writes WAV through the standard library's `wave` module (see
# app/audio.py). PollyTTSClient wraps Polly's headerless PCM into WAV precisely
# to keep it that way -- switching the merge step to mp3 would drag ffmpeg back
# in, and this image ships to Lambda.

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # The adapter listens for Lambda and forwards to this port; its own default
    # is 8080. The app already serves 8000.
    AWS_LWA_PORT=8000 \
    # The adapter's default readiness path is `/`, which this app does not serve.
    # A 404 falls inside its healthy range (100-499), so the default would report
    # ready the moment uvicorn bound a socket rather than when the app could
    # answer -- the first real request would then race startup.
    AWS_LWA_READINESS_CHECK_PATH=/health

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 appuser

# Deliberately NOT chowned to appuser. `chown -R` on the virtualenv rewrites
# every file into the layer, which doubled this image (a 287 MB venv produced a
# 603 MB layer). Nothing here needs to be writable: the app does not write to
# its own install, and in Lambda the filesystem is read-only outside /tmp
# anyway, so root-owned and world-readable is both smaller and tighter.
COPY --from=builder /app/.venv /app/.venv
COPY app ./app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["/app/.venv/bin/python", "-c", "import json, sys, urllib.request; response = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3); payload = json.load(response); sys.exit(0 if payload.get('status') == 'ok' else 1)"]

# The venv's uvicorn directly, not `uv run`: uv is not in this image, and even
# where it is, it would re-resolve the project on every cold start and may try to
# write -- and in Lambda the filesystem is read-only outside /tmp.
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
