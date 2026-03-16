# ArduPilot Build Server

Self-hosted ArduPilot custom firmware build server running on Unraid, accessible via Cloudflare Tunnel at [jforbes.us](https://jforbes.us).

## Architecture

| Service | Container | Description |
|---------|-----------|-------------|
| **Custom Firmware Builder** | `ardupilot-custombuild-app` | FastAPI web UI and REST API for submitting and managing builds |
| **Build Worker** | `ardupilot-custombuild-builder` | Processes firmware build jobs from the Redis queue |
| **Redis** | `ardupilot-redis` | Message queue and job broker between the web app and builder |
| **Admin** | `ardupilot-admin` | Remotes/branch management UI and system status dashboard |
| **Caddy** | `ardupilot-caddy` | Reverse proxy routing traffic to all services on port 8000 |
| **Cloudflare Tunnel** | `ardupilot-cloudflared` | Secure public ingress via Cloudflare Zero Trust |

## Web UI

| Page | URL | Description |
|------|-----|-------------|
| **Builder** | `/` | View active/completed builds, download artifacts |
| **Add Build** | `/add_build` | Select vehicle, version, board, and features to build |
| **Admin** | `/admin` | Add/remove git remotes, vehicles, and release branches |
| **Results** | `/results/` | Autotest results and build logs |
| **Status** | `/status` | Live system status dashboard for all components |
| **Docs** | `/docs` | This documentation page |

## Source Repositories

| Repository | Description |
|------------|-------------|
| [jack7169/ardupilot-jack](https://github.com/jack7169/ardupilot-jack) | Custom ArduPilot fork with ExtPos/EKF3 branches |
| [ArduPilot/CustomBuild](https://github.com/ArduPilot/CustomBuild) | Upstream custom firmware builder framework |
| [ArduPilot/ardupilot](https://github.com/ArduPilot/ardupilot) | Upstream ArduPilot firmware |

## CLI Tool (`ap-build`)

A command-line interface that talks to the same API as the web UI. Every build submitted via CLI appears on the web dashboard.

### Installation

Copy `ap-build` to your PATH:

```bash
cp ap-build /usr/local/bin/
```

Requires `curl` and `jq`.

### Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `AP_BUILD_API` | `http://carthagenas.local:8000/api/v1` | API base URL |

For public access: `AP_BUILD_API=https://jforbes.us/api/v1`

### Commands

#### List available options

```bash
# List all vehicle types
ap-build list vehicles

# List versions for a vehicle (shows remote, type, and version ID)
ap-build list versions plane

# List boards for a specific version
ap-build list boards plane <version_id>

# List features with defaults for a board
ap-build list features plane <version_id> CubeOrange

# List recent builds (optionally filter by state)
ap-build list builds
ap-build list builds --state RUNNING
```

#### Submit a build

```bash
# Build with default features
ap-build submit plane <version_id> CubeOrange

# Build with all features enabled
ap-build submit plane <version_id> CubeOrange --all-features

# Build with no optional features
ap-build submit plane <version_id> CubeOrange --no-features

# Build with specific features
ap-build submit plane <version_id> CubeOrange --features HAL_ADSB_ENABLED,GPS
```

#### Monitor builds

```bash
# Check build status
ap-build status <build_id>

# View build logs
ap-build logs <build_id>

# Stream logs in real-time
ap-build logs <build_id> --follow

# View last 50 lines
ap-build logs <build_id> --tail 50
```

#### Download artifacts

```bash
# Download firmware archive
ap-build download <build_id>

# Download to a specific file
ap-build download <build_id> --output firmware.tar.gz
```

### Example workflow

```bash
# 1. Find the version ID for jack7169's latest Plane build
ap-build list versions plane | grep jack7169

# 2. Check available boards
ap-build list boards plane jack7169-refs-heads-feature-extpos-kalman-fusion-abc123

# 3. Submit a build
ap-build submit plane jack7169-refs-heads-feature-extpos-kalman-fusion-abc123 CubeOrangePlus

# 4. Follow the build progress
ap-build logs plane-CubeOrangePlus-... --follow

# 5. Download when complete
ap-build download plane-CubeOrangePlus-...
```

## REST API

The full API is available at `/api/v1/`. Key endpoints:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/vehicles` | List vehicle types |
| `GET` | `/api/v1/vehicles/{id}/versions` | List versions for a vehicle |
| `GET` | `/api/v1/vehicles/{id}/versions/{vid}/boards` | List boards |
| `GET` | `/api/v1/vehicles/{id}/versions/{vid}/boards/{bid}/features` | List features |
| `POST` | `/api/v1/builds` | Submit a new build |
| `GET` | `/api/v1/builds` | List builds |
| `GET` | `/api/v1/builds/{id}` | Get build status |
| `GET` | `/api/v1/builds/{id}/logs` | Get build logs |
| `GET` | `/api/v1/builds/{id}/artifact` | Download build artifact |

## Access Control

Public access is gated by Cloudflare Zero Trust:
- Authentication via OTP email
- Allowed domains: `@s2va.mil`, `@tyrlaboratories.com`
- LAN access (`http://carthagenas.local:8000`) bypasses Cloudflare

## Deployment

The stack runs on Unraid via Docker Compose at `/mnt/user/appdata/ardupilot/docker/`.

```bash
# SSH to the server
ssh root@carthagenas.local

# Navigate to the compose directory
cd /mnt/user/appdata/ardupilot/docker

# Rebuild and restart
docker compose up -d --build

# View logs
docker compose logs -f
```
