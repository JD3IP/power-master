#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
# Power Master — Cross-build ARM64 image for Raspberry Pi
# ─────────────────────────────────────────────────────────
# Run this on your fast x86 dev machine (not the Pi).
# Produces a tarball you can transfer and load on the Pi.
#
# Prerequisites:
#   - Docker Desktop (with buildx) OR Docker Engine + QEMU
#   - Project root must be the working directory
#
# Usage:
#   bash deploy/build-pi-image.sh
#
# Output:
#   power-master-pi.tar  (in project root)
# ─────────────────────────────────────────────────────────
set -euo pipefail

IMAGE_NAME="power-master:pi"
TARBALL="power-master-pi.tar"
PLATFORM="linux/arm64"
BUILDER_NAME="pi-builder"

echo "========================================"
echo " Power Master — Cross-build for Pi"
echo " Platform: ${PLATFORM}"
echo "========================================"

# ── 1. Ensure we're in the project root ──
if [ ! -f "Dockerfile" ]; then
    echo "[ERROR] Run this script from the project root (where Dockerfile is)."
    exit 1
fi

# ── 2. Set up QEMU for ARM emulation ──
echo "[1/4] Setting up QEMU for ARM emulation..."
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes 2>/dev/null || true

# ── 3. Create buildx builder (if needed) ──
if ! docker buildx inspect "$BUILDER_NAME" &>/dev/null; then
    echo "[2/4] Creating buildx builder '${BUILDER_NAME}'..."
    docker buildx create --name "$BUILDER_NAME" --use --bootstrap
else
    echo "[2/4] Using existing buildx builder '${BUILDER_NAME}'"
    docker buildx use "$BUILDER_NAME"
fi

# ── 4. Build the ARM64 image ──
echo "[3/4] Building ${IMAGE_NAME} for ${PLATFORM}..."
echo "       (This may take 10-15 minutes with QEMU emulation)"
docker buildx build \
    --platform "$PLATFORM" \
    --tag "$IMAGE_NAME" \
    --output "type=docker,dest=${TARBALL}" \
    .

echo "[4/4] Image saved to ${TARBALL}"
ls -lh "$TARBALL"

echo ""
echo "========================================"
echo " Build complete!"
echo ""
echo " Transfer to Pi:"
echo "   scp ${TARBALL} pi@<pi-ip>:~/"
echo ""
echo " On the Pi:"
echo "   docker load -i ~/${TARBALL}"
echo "   docker compose -f docker-compose.pi.yml up -d"
echo "========================================"
