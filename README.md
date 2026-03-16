# ArduPilot Build & Test Server

Self-hosted ArduPilot custom firmware builder and SITL autotest runner on Unraid. Public access at [jforbes.us](https://jforbes.us) via Cloudflare Tunnel with Zero Trust authentication.

## Architecture

```
                        ┌──────────────────────┐
    Internet ──────────▶│   Cloudflare Tunnel  │
                        │   (Zero Trust Auth)  │
                        └──────────┬───────────┘
                                   │
    LAN ───────────────────────────┤
                                   │
                        ┌──────────▼───────────┐
                        │   Caddy (reverse     │
                        │   proxy + static)    │
                        │        :8000         │
                        └──┬───┬───┬───┬───┬───┘
                           │   │   │   │   │
         ┌─────────────────┘   │   │   │   └─────────────────┐
         ▼                     ▼   │   ▼                     ▼
    ┌─────────┐      ┌──────────┐  │ ┌──────────┐       ┌───────────┐
    │CustomBld│      │  Admin   │  │ │Autotest  │       │ Cloudflare│
    │  App    │      │ Service  │  │ │ Service  │       │  Tunnel   │
    │ :8080   │      │  :8090   │  │ │  :8091   │       │           │
    └────┬────┘      └──────────┘  │ └─────┬────┘       └───────────┘
         │                         │       │
    ┌────▼────┐             ┌──────▼───────▼──────┐
    │CustomBld│             │   Shared Volumes    │
    │ Builder │             │ • custombuild-base  │
    │ (worker)│             │ • autotest-workdir  │
    └────┬────┘             │ • autotest-results  │
         │                  │ • ardupilot-logs    │
    ┌────▼────┐             └─────────────────────┘
    │  Redis  │
    │  :6379  │
    └─────────┘
```

### Services

| Service | Container | Port | Description |
|---------|-----------|------|-------------|
| **CustomBuild App** | `ardupilot-custombuild-app` | 8080 | Web UI and REST API for custom firmware builds |
| **CustomBuild Builder** | `ardupilot-custombuild-builder` | — | Worker that compiles firmware from Redis queue |
| **Redis** | `ardupilot-redis` | 6379 | Job queue between web app and builder |
| **Admin** | `ardupilot-admin` | 8090 | Remotes management, status dashboard, docs, results viewer |
| **Autotest** | `ardupilot-autotest` | 8091 | SITL test execution with concurrent instance pool |
| **Caddy** | `ardupilot-caddy` | 8000 | Reverse proxy routing to all services |
| **Cloudflare Tunnel** | `ardupilot-cloudflared` | — | Public ingress via Cloudflare Zero Trust |

### URL Routing (Caddyfile)

| Path | Backend | Description |
|------|---------|-------------|
| `/` | custombuild-app:8080 | Build dashboard and firmware builder |
| `/add_build` | custombuild-app:8080 | Create new firmware build |
| `/admin` | admin:8090 | Remotes/branch management |
| `/autotest` | admin:8090 | Test submission UI |
| `/autotest/api/*` | autotest:8091 | Test execution API |
| `/status` | admin:8090 | System status dashboard |
| `/docs` | admin:8090 | Documentation |
| `/results/` | admin:8090 | Test results and build logs |
| `/api/capabilities` | admin:8090 | Machine-readable API discovery |

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

# Pin an exact commit SHA (prevents branch drift between test submissions)
ap-build test submit Plane \
    test.QuadPlane.GPSDeniedQLoiterExtPos \
    test.QuadPlane.GPSDeniedVTOLTransitionExtPos \
    --remote jack7169 --commit 423c00fc139f70eb3c7e52808f4dd3e56a1d016a

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
    --remote jack7169 --commit 423c00fc139f
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
    --remote jack7169 --commit 423c00fc139f

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

## Deployment on Unraid

### Prerequisites

- Unraid server with Docker support
- 24+ CPU cores recommended (SITL tests are CPU-intensive)
- 16GB+ RAM minimum
- Cloudflare account with a domain (for public access)

### Directory Structure on Server

```
/mnt/user/appdata/ardupilot/
├── docker/                         # Docker Compose stack (this repo)
│   ├── docker-compose.yml
│   ├── .env
│   ├── admin/
│   ├── autotest/
│   └── caddy/
├── custombuild/                    # Upstream CustomBuild (git clone)
├── custombuild-base/               # Shared volume: build configs + remotes.json
│   └── configs/remotes.json
├── custombuild-templates/          # Patched HTML templates mounted into app
├── buildlogs/                      # Test results and build logs
└── docker/admin/static/            # Admin static assets
```

### Initial Setup

```bash
# 1. Clone this repo to the server
ssh root@carthagenas.local
cd /mnt/user/appdata/ardupilot
git clone git@github.com:jack7169/ardupilot-unraid.git docker

# 2. Clone upstream CustomBuild
git clone https://github.com/ArduPilot/CustomBuild.git custombuild

# 3. Create required directories
mkdir -p custombuild-base/configs custombuild-templates buildlogs

# 4. Copy patched templates
cp docker/templates/*.html custombuild-templates/

# 5. Configure environment
cd docker
cp .env.example .env    # Edit with your Cloudflare tunnel token

# 6. Start all services
docker-compose up -d --build

# 7. Verify
curl http://localhost:8000/status/api
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CLOUDFLARE_TUNNEL_TOKEN` | Yes (for public access) | Cloudflare Tunnel token |
| `CBS_LOG_LEVEL` | No | Log level (default: `INFO`) |
| `CBS_BUILD_TIMEOUT_SEC` | No | Build timeout in seconds (default: `900`) |
| `CBS_REMOTES_RELOAD_TOKEN` | No | Token for triggering CustomBuild reload |

### Updating

```bash
ssh root@carthagenas.local
cd /mnt/user/appdata/ardupilot/docker

# Pull latest code
git pull

# Copy updated templates
cp templates/*.html /mnt/user/appdata/ardupilot/custombuild-templates/

# Rebuild and restart
docker-compose up -d --build
```

### Deploying from Development Machine

```bash
# Deploy specific services
scp docker/autotest/app.py root@carthagenas.local:/mnt/user/appdata/ardupilot/docker/autotest/app.py
scp docker/admin/app.py root@carthagenas.local:/mnt/user/appdata/ardupilot/docker/admin/app.py
scp docker/templates/*.html root@carthagenas.local:/mnt/user/appdata/ardupilot/custombuild-templates/
scp docker/admin/templates/*.html root@carthagenas.local:/mnt/user/appdata/ardupilot/docker/admin/templates/

# Rebuild affected containers
ssh root@carthagenas.local "cd /mnt/user/appdata/ardupilot/docker && docker-compose up -d --build admin autotest && docker-compose up -d --force-recreate caddy"
```

### Monitoring

```bash
# Service logs
docker-compose logs -f autotest
docker-compose logs -f custombuild-builder

# System metrics
curl http://localhost:8000/autotest/api/metrics

# Service health
curl http://localhost:8000/status/api
```

### Docker vDisk Management

The autotest container can fill its 20GB vDisk with worktrees and build artifacts. The service auto-cleans worktrees after tests complete and evicts old templates, but if space gets low:

```bash
# Check disk usage
docker exec ardupilot-autotest df -h /

# Manual cleanup of worktrees
docker exec ardupilot-autotest bash -c 'rm -rf /workdir/worktrees/*'

# Prune Docker build cache
docker builder prune -f
docker image prune -f
```
