# ── Build stage ──────────────────────────────────────────
FROM python:3.11-slim AS builder

ARG CACHE_BUST=1
RUN echo "cache_bust=${CACHE_BUST}"

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml ./
COPY src/ ./src/

RUN pip install --no-cache-dir --prefix=/install .

# ── Runtime stage ────────────────────────────────────────
FROM python:3.11-slim

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Bake defaults into a fixed location (entrypoint copies to data dir)
COPY config.defaults.yaml /opt/power-master/config.defaults.yaml
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Persistent data: config.yaml, power_master.db
VOLUME /data
WORKDIR /data

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

ENTRYPOINT ["docker-entrypoint.sh"]
