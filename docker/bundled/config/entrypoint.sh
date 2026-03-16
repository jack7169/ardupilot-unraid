#!/bin/sh
set -e

# Ensure data directories exist
mkdir -p /data/custombuild-base/configs \
         /data/autotest-workdir \
         /data/autotest-results \
         /data/buildlogs

# Fix ownership of data directories for ardupilot user
find /data \! -user ardupilot -exec chown ardupilot '{}' + 2>/dev/null || true

# Set default env vars for supervisord %(ENV_*) interpolation
export CBS_LOG_LEVEL="${CBS_LOG_LEVEL:-INFO}"
export CBS_BUILD_TIMEOUT_SEC="${CBS_BUILD_TIMEOUT_SEC:-900}"
export CBS_REMOTES_RELOAD_TOKEN="${CBS_REMOTES_RELOAD_TOKEN:-}"

exec "$@"
