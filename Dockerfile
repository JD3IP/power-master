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

# CBC solver for ARM (PuLP bundles x86_64 only; coinor-cbc covers ARM/aarch64)
# Also install libatomic1 needed by some packages on ARMv7
RUN apt-get update && \
    apt-get install -y --no-install-recommends coinor-cbc libatomic1 && \
    rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# ── Version info baked at build time ──
ARG BUILD_VERSION=dev
ARG BUILD_SHA=unknown
RUN printf '{"version":"%s","sha":"%s","built_at":"%s"}\n' \
    "$BUILD_VERSION" "$BUILD_SHA" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    > /opt/power-master/version.json

LABEL org.opencontainers.image.version="$BUILD_VERSION" \
      org.opencontainers.image.revision="$BUILD_SHA" \
      org.opencontainers.image.source="https://github.com/JD3IP/power-master"

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
