FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MUJOCO_GL=egl

# MuJoCo's headless renderer needs EGL/GL. OSMesa is a fallback when the
# host has no working EGL implementation.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libegl1 \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        libosmesa6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[service,mujoco]'

RUN useradd --create-home --uid 10001 runtime \
    && mkdir -p /data \
    && chown runtime:runtime /data

USER runtime
ENV SIM_RUNTIME_DATA_DIR=/data \
    SIM_RUNTIME_HOST=0.0.0.0 \
    SIM_RUNTIME_PORT=8080

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

CMD ["python", "-m", "simulation_runtime.service"]
