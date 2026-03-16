# ArduPilot Firmware Webtools

Self-hosted ArduPilot custom firmware builder and SITL autotest runner on Unraid. Public access at [jforbes.us](https://jforbes.us) via Cloudflare Tunnel with Zero Trust authentication.

## Architecture

All services run inside a single Docker container (`ardupilot-bundled`) managed by supervisord. Cloudflare Tunnel runs as a sidecar container for public ingress.

```
                         ┌──────────────────────┐
     Internet ──────────▶│  Cloudflare Tunnel   │
                         │  (Zero Trust Auth)   │
                         └──────────┬───────────┘
                                    │
     LAN ───────────────────────────┤
                                    │
    ┌───────────────────────────────▼───────────────────────────────┐
    │  ardupilot-bundled (supervisord)                              │
    │                                                               │
    │   ┌─────────────────────────────────────────────────────┐    │
    │   │  Caddy (reverse proxy + static files)  :8000        │    │
    │   └──────┬────────┬────────┬────────────────────────────┘    │
    │          │        │        │                                  │
    │   ┌──────▼──┐ ┌───▼────┐ ┌─▼────────┐                       │
    │   │CustomBld│ │ Admin  │ │ Autotest  │                       │
    │   │  App    │ │Service │ │ Service   │                       │
    │   │ :8080   │ │ :8090  │ │  :8091    │                       │
    │   └────┬────┘ └────────┘ └───────────┘                       │
    │        │                                                      │
    │   ┌────▼────┐    ┌─────────┐                                 │
    │   │CustomBld│    │  Redis  │                                  │
    │   │ Builder │◄──▶│  :6379  │                                  │
    │   │ (worker)│    └─────────┘                                  │
    │   └─────────┘                                                 │
    │                                                               │
    │   Volumes: /data/custombuild-base, /data/autotest-workdir,   │
    │            /data/autotest-results, /data/buildlogs           │
    └───────────────────────────────────────────────────────────────┘
```

### Services (inside single container)

| Process | Port | Description |
|---------|------|-------------|
| **Caddy** | 8000 | Reverse proxy routing to all services (only exposed port) |
| **CustomBuild App** | 8080 | Web UI and REST API for custom firmware builds |
| **CustomBuild Builder** | — | Worker that compiles firmware from Redis queue |
| **Redis** | 6379 | In-memory job queue between web app and builder |
| **Admin** | 8090 | Remotes management, status dashboard, docs, results viewer |
| **Autotest** | 8091 | SITL test execution with concurrent instance pool |

The **Cloudflare Tunnel** (`ardupilot-cloudflared`) runs as a separate sidecar container for public ingress via Zero Trust.

### URL Routing (Caddyfile)

All routing is internal via localhost within the bundled container:

| Path | Backend | Description |
|------|---------|-------------|
| `/` | 127.0.0.1:8080 | Build dashboard and firmware builder |
| `/add_build` | 127.0.0.1:8080 | Create new firmware build |
| `/admin` | 127.0.0.1:8090 | Remotes/branch management |
| `/autotest` | 127.0.0.1:8090 | Test submission UI |
| `/autotest/api/*` | 127.0.0.1:8091 | Test execution API |
| `/status` | 127.0.0.1:8090 | System status dashboard |
| `/docs` | 127.0.0.1:8090 | Documentation |
| `/results/` | 127.0.0.1:8090 | Test results and build logs |
| `/api/capabilities` | 127.0.0.1:8090 | Machine-readable API discovery |

## Web UI

| Page | Description |
|------|-------------|
| **Builder** (`/`) | View active/completed builds, download firmware artifacts |
| **Add Build** (`/add_build`) | Select vehicle, version, board, and toggle features |
| **Tests** (`/autotest`) | Submit SITL tests, view results, select from dropdowns |
| **Admin** (`/admin`) | Add/remove git remotes, vehicles, and release branches |
| **Results** (`/results/`) | Browse autotest output logs, dataflash files, tlog files |
| **Status** (`/status`) | Live health check for all services with response times |
| **Docs** (`/docs`) | Full documentation and API reference |

All pages include a live system metrics ticker in the navbar showing CPU%, memory%, and running test count with animated icons at high load.

## Source Repositories

| Repository | Description |
|------------|-------------|
| [jack7169/ardupilot-unraid](https://github.com/jack7169/ardupilot-unraid) | This repo — server infrastructure and deployment |
| [jack7169/ardupilot-jack](https://github.com/jack7169/ardupilot-jack) | Custom ArduPilot fork with ExtPos/EKF3 branches |
| [ArduPilot/CustomBuild](https://github.com/ArduPilot/CustomBuild) | Upstream custom firmware builder framework |
| [ArduPilot/ardupilot](https://github.com/ArduPilot/ardupilot) | Upstream ArduPilot firmware |

Both `ardupilot-jack` and `custombuild` are included as git submodules.

## CLI Tool (`ap-build`)

A full-featured command-line interface for builds, tests, git management, and batch operations. Every action taken via CLI appears on the web dashboard.

### Installation

```bash
# Copy to PATH
cp ap-build /usr/local/bin/

# Or use directly
./ap-build help
```

Requires `curl` and `jq`.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AP_BUILD_URL` | `http://192.168.50.45:8000` | Server base URL |

For public access: `AP_BUILD_URL=https://jforbes.us`

### Build Commands

```bash
# List available options
ap-build list vehicles                              # Vehicle types (Plane, Copter, etc.)
ap-build list versions plane                        # Versions/branches for a vehicle
ap-build list boards plane <version_id>             # Supported hardware boards
ap-build list features plane <version_id> CubeOrange # Features with defaults
ap-build list builds                                # Recent builds
ap-build list builds --state RUNNING                # Filter by state

# Submit a build
ap-build submit plane <version_id> CubeOrange
ap-build submit plane <version_id> CubeOrange --all-features
ap-build submit plane <version_id> CubeOrange --no-features
ap-build submit plane <version_id> CubeOrange --features HAL_ADSB_ENABLED,GPS

# Monitor and download
ap-build status <build_id>
ap-build logs <build_id> --follow
ap-build logs <build_id> --tail 50
ap-build download <build_id>
ap-build download <build_id> --output firmware.tar.gz
```

### Test Commands

```bash
# Submit a single test
ap-build test submit Plane test.QuadPlane.GPSDeniedQLoiterExtPos \
    --remote jack7169 --ref feature/extpos-kalman-fusion

# Submit multiple tests (auto-generates batch ID)
ap-build test submit Plane \
    test.QuadPlane.GPSDeniedQLoiterExtPos \
    test.QuadPlane.GPSDeniedVTOLTransitionExtPos \
    test.Plane.ExtPosGPSToExtPosTransition \
    --remote jack7169 --ref feature/extpos-kalman-fusion

# Pin an exact commit SHA (requires --ref for fetching, uses --commit for checkout)
ap-build test submit Plane \
    test.QuadPlane.GPSDeniedQLoiterExtPos \
    test.QuadPlane.GPSDeniedVTOLTransitionExtPos \
    --remote jack7169 --ref feature/extpos-kalman-fusion \
    --commit 423c00fc139f70eb3c7e52808f4dd3e56a1d016a

# Extra waf flags
ap-build test submit Plane test.Plane.MainFlight \
    --waf-configure "--debug" --waf-build "-j8"

# Monitor tests
ap-build test list
ap-build test status <test_id>
ap-build test logs <test_id> --follow
ap-build test logs <test_id> --tail 20
ap-build test cancel <test_id>
```

### Batch Commands

When submitting multiple tests, a batch ID is auto-generated (e.g., `batch-20260316-041500-a3f2`).

```bash
# Submit a batch (2+ tests auto-generates a batch ID)
ap-build test submit Plane \
    test.QuadPlane.GPSDeniedQLoiterExtPos \
    test.QuadPlane.GPSDeniedVTOLTransitionExtPos \
    test.QuadPlane.GPSDeniedExtPosDropout \
    --remote jack7169 --ref feature/extpos-kalman-fusion \
    --commit 423c00fc139f
# Output:
#   Batch: batch-20260316-041500-a3f2
#   Track progress:
#     ap-build batch status batch-20260316-041500-a3f2
#     ap-build batch logs batch-20260316-041500-a3f2

# Monitor the batch
ap-build batch list                     # List all batches with pass/fail counts
ap-build batch status <batch_id>        # Detailed status of every test in batch
ap-build batch summary <batch_id>       # Compact pass/fail summary
ap-build batch logs <batch_id>          # All logs for every test in batch
ap-build batch wait <batch_id>          # Block until batch completes (default 600s timeout)
ap-build batch wait <batch_id> --timeout 1200  # Custom timeout
```

### Git Management

```bash
ap-build git remotes                            # List configured remotes
ap-build git branches --remote jack7169         # List remote branches
ap-build git tags --remote jack7169             # List tags
ap-build git add-remote myremote https://github.com/user/ardupilot.git
ap-build git update --remote jack7169 --ref feature/extpos-kalman-fusion
```

### Example Workflow

```bash
# 1. Find the version for your branch
ap-build list versions plane | grep jack7169

# 2. Submit a custom firmware build
ap-build submit plane <version_id> CubeOrangePlus --all-features

# 3. Follow the build
ap-build logs <build_id> --follow

# 4. Download firmware
ap-build download <build_id> --output my-firmware.tar.gz

# 5. Run all ExtPos tests against a pinned commit
ap-build test submit Plane \
    test.QuadPlane.GPSDeniedQLoiterExtPos \
    test.QuadPlane.GPSDeniedVTOLTransitionExtPos \
    test.QuadPlane.GPSDeniedExtPosDropout \
    test.Plane.ExtPosGPSToExtPosTransition \
    --remote jack7169 --ref feature/extpos-kalman-fusion \
    --commit 423c00fc139f

# 6. Check batch results
ap-build batch summary batch-20260316-...
```

## REST API

### Build API (`/api/v1`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/vehicles` | List vehicle types |
| `GET` | `/api/v1/vehicles/{id}/versions` | List versions for vehicle |
| `GET` | `/api/v1/vehicles/{id}/versions/{vid}/boards` | List boards |
| `GET` | `/api/v1/vehicles/{id}/versions/{vid}/boards/{bid}/features` | List features |
| `POST` | `/api/v1/builds` | Submit build |
| `GET` | `/api/v1/builds` | List builds (filter: `?state=`, `?vehicle_id=`) |
| `GET` | `/api/v1/builds/{id}` | Build status |
| `GET` | `/api/v1/builds/{id}/logs` | Build logs (`?tail=N`) |
| `GET` | `/api/v1/builds/{id}/artifact` | Download firmware (.tar.gz) |

### Autotest API (`/autotest/api`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/autotest/api/status` | Service status (busy/idle, running count) |
| `GET` | `/autotest/api/metrics` | System metrics (CPU, memory, load, test counts) |
| `GET` | `/autotest/api/vehicles` | List testable vehicles |
| `GET` | `/autotest/api/subtests?vehicle=Plane` | List available subtests |
| `GET` | `/autotest/api/test-suites` | List top-level test suites |
| `POST` | `/autotest/api/tests` | Submit test |
| `GET` | `/autotest/api/tests` | List tests (`?batch_id=`, `?limit=`) |
| `GET` | `/autotest/api/tests/{id}` | Test details |
| `GET` | `/autotest/api/tests/{id}/logs` | Test logs (`?tail=N`) |
| `POST` | `/autotest/api/tests/{id}/cancel` | Cancel test |
| `GET` | `/autotest/api/batches` | List batches |
| `GET` | `/autotest/api/batches/{id}` | Batch details |
| `GET` | `/autotest/api/batches/{id}/logs` | All batch logs |

### Admin API (`/admin/api`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/admin/api/remotes` | List remotes |
| `POST` | `/admin/api/remotes` | Add remote |
| `PUT` | `/admin/api/remotes/{name}` | Update remote |
| `DELETE` | `/admin/api/remotes/{name}` | Delete remote |
| `POST` | `/admin/api/remotes/{name}/vehicles` | Add vehicle |
| `DELETE` | `/admin/api/remotes/{name}/vehicles/{vname}` | Delete vehicle |
| `POST` | `/admin/api/remotes/{name}/vehicles/{vname}/releases` | Add release |
| `DELETE` | `/admin/api/remotes/{name}/vehicles/{vname}/releases/{idx}` | Delete release |
| `POST` | `/admin/api/refresh` | Reload CustomBuild remotes |

### Git API (`/autotest/api/git`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/autotest/api/git/remotes` | List git remotes |
| `GET` | `/autotest/api/git/branches?remote=X` | List branches |
| `GET` | `/autotest/api/git/tags?remote=X` | List tags |
| `POST` | `/autotest/api/git/add-remote` | Add remote |
| `POST` | `/autotest/api/git/update` | Fetch and checkout |

Interactive API docs: [`/autotest/api/docs`](https://jforbes.us/autotest/api/docs) (Swagger UI)

### API Discovery

`GET /api/capabilities` returns a machine-readable JSON spec for AI agent integration.

## Autotest System

### Concurrency

- **50 concurrent SITL instances** via instance pool with unique port offsets
- Each instance gets ports at `base + instance_num * 10` to avoid collisions
- The autotest framework is patched at runtime to match the port offsets

### Caching

Two-layer cache eliminates redundant work when running batched tests:

| Layer | Key | What it caches | Effect |
|-------|-----|---------------|--------|
| **Source template** | Commit SHA | Git checkout + submodules | Skip `git fetch` + `submodule update` |
| **Build template** | Commit + vehicle + waf args | Compiled SITL binary | Skip `waf configure` + `waf build` |

For a batch of 20 tests against the same branch: only 1 fetch and 1 build, then 20 instant copies.

### Test Lifecycle

```
PENDING → UPDATING → BUILDING → QUEUED → TESTING → SUCCESS/FAILURE/ERROR
                                                  → CANCELLED (if cancelled)
```

### Artifact Collection

After each test, logs are collected to `/results/{test_id}/`:
- `test.log` — full autotest output
- `meta.json` — test metadata and state
- `index.html` — autotest summary page
- `autotest-badge.svg` — pass/fail badge
- `dataflash/` — BIN flight logs
- `*.tlog` — MAVLink telemetry logs

## Access Control

Public access at `https://jforbes.us` is gated by Cloudflare Zero Trust:

- **Authentication**: OTP email verification
- **Allowed domains**: `@s2va.mil`, `@tyrlaboratories.com`
- **LAN bypass**: `http://carthagenas.local:8000` (no auth required)

## Deployment

### Bundled Container (Recommended)

The bundled deployment runs all services in a single container managed by supervisord. Cloudflare Tunnel runs as a sidecar.

#### Prerequisites

- Docker host with `docker-compose`
- 24+ CPU cores recommended (SITL tests are CPU-intensive)
- 16GB+ RAM minimum
- Cloudflare account with a domain (for public access)

#### Directory Structure on Server

```
/mnt/user/appdata/ardupilot/
├── servers-repo/                     # This repo (git clone)
│   ├── docker/bundled/               # Bundled deployment
│   │   ├── Dockerfile
│   │   ├── docker-compose.yml
│   │   ├── .env
│   │   └── config/
│   │       ├── supervisord.conf
│   │       ├── Caddyfile
│   │       ├── config.yaml
│   │       └── entrypoint.sh
│   ├── docker/admin/                 # Admin service source
│   ├── docker/autotest/              # Autotest service source
│   ├── docker/templates/             # Custom HTML templates
│   └── custombuild/                  # CustomBuild submodule
├── bundled-data/                     # Data volume
│   ├── custombuild-base/             # Build configs, remotes.json, artifacts
│   ├── autotest-workdir/             # Git repos and worktrees
│   └── autotest-results/             # Test output
└── buildlogs/                        # Shared build/test logs
```

#### Initial Setup

```bash
# 1. Clone this repo with submodules
ssh root@carthagenas.local
cd /mnt/user/appdata/ardupilot
git clone git@github.com:jack7169/ardupilot-unraid.git servers-repo
cd servers-repo
git submodule update --init custombuild

# 2. Create data directories
mkdir -p /mnt/user/appdata/ardupilot/bundled-data/{custombuild-base/configs,autotest-workdir,autotest-results}
mkdir -p /mnt/user/appdata/ardupilot/buildlogs

# 3. Configure environment
cd docker/bundled
cp .env.example .env
# Edit .env with your Cloudflare tunnel token and paths

# 4. Build and start
docker-compose up -d --build

# 5. Verify (wait ~30s for initial startup)
curl http://localhost:8000/status
```

#### Environment Variables (`.env`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CLOUDFLARE_TUNNEL_TOKEN` | Yes (for public access) | — | Cloudflare Tunnel token |
| `CBS_LOG_LEVEL` | No | `INFO` | Log level |
| `CBS_BUILD_TIMEOUT_SEC` | No | `900` | Build timeout in seconds |
| `CBS_REMOTES_RELOAD_TOKEN` | No | — | Token for triggering CustomBuild reload |
| `DATA_DIR` | No | `/mnt/user/appdata/ardupilot/bundled-data` | Data volume host path |
| `BUILDLOGS_DIR` | No | `/mnt/user/appdata/ardupilot/buildlogs` | Build logs host path |

#### Updating

```bash
ssh root@carthagenas.local
cd /mnt/user/appdata/ardupilot/servers-repo

# Pull latest code
git pull
git submodule update

# Rebuild and restart
cd docker/bundled
docker-compose up -d --build
```

#### Monitoring

```bash
# All process logs (via supervisord)
docker logs -f ardupilot-bundled

# Individual service logs
docker exec ardupilot-bundled cat /var/log/supervisor/admin.log
docker exec ardupilot-bundled cat /var/log/supervisor/autotest.log
docker exec ardupilot-bundled cat /var/log/supervisor/custombuild-app.log
docker exec ardupilot-bundled cat /var/log/supervisor/custombuild-builder.log

# System metrics
curl http://localhost:8000/autotest/api/metrics

# Service health
curl http://localhost:8000/status/api
```

#### Disk Management

The autotest service auto-cleans worktrees after tests and evicts old templates via LRU caching, but if space gets low:

```bash
# Check disk usage
docker exec ardupilot-bundled df -h /data

# Manual cleanup of worktrees
docker exec ardupilot-bundled rm -rf /data/autotest-workdir/worktrees/*

# Prune Docker build cache
docker builder prune -f
docker image prune -f
```

### Legacy Multi-Container Deployment

The original 7-container docker-compose stack is still available at `docker/docker-compose.yml` for reference. See git history for setup instructions.
