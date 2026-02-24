#!/bin/sh
set -e

# Copy config.defaults.yaml into the data directory if not present.
# This file is baked into the image; the data directory is a mounted volume.
if [ ! -f config.defaults.yaml ]; then
    cp /opt/power-master/config.defaults.yaml config.defaults.yaml
fi

exec python -m power_master "$@"
