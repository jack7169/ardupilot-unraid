#!/bin/sh
set -e

# Ensure data subdirectories exist
mkdir -p /data/custombuild-base/configs /data/buildlogs

# Fix ownership of volume mount points for ardupilot user
for dir in /data/custombuild-base /data/autotest-workdir /data/autotest-results /data/buildlogs /workdir; do
    chown ardupilot:ardupilot "$dir" 2>/dev/null || true
done

# Set default env vars for supervisord %(ENV_*) interpolation
export CBS_LOG_LEVEL="${CBS_LOG_LEVEL:-INFO}"
export CBS_BUILD_TIMEOUT_SEC="${CBS_BUILD_TIMEOUT_SEC:-900}"
export CBS_REMOTES_RELOAD_TOKEN="${CBS_REMOTES_RELOAD_TOKEN:-}"

exec "$@"
