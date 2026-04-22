# CLAUDE.md

## Project Overview

Self-hosted ArduPilot custom firmware builder and SITL autotest runner. All services run inside a Docker container (`ardupilot-bundled`) on an Unraid server. Public access via Cloudflare Tunnel at `https://jforbes.us`.

## Critical: `ap-build` is a REST API Client

**`ap-build` is a CLI that sends HTTP requests to the remote server.** It is NOT a local tool. Every command hits `$AP_BUILD_URL` (default: `http://100.99.196.120:8000`). The server has its own ArduPilot git repo inside the Docker container with its own remotes — completely separate from any local git state.

- `ap-build git remotes` queries the **server's** git repo, not local
- `ap-build git add-remote` adds a remote to the **server's** git repo
- `ap-build test submit` sends a POST to the **server's** autotest API
- The local repo checkout, branches, and remotes are irrelevant to the server

## Test Submission

### Test Name Format

Tests passed to `autotest.py` must use the format expected by ArduPilot's autotest framework:

| Format | When to Use | Example |
|--------|-------------|---------|
| `test.<Vehicle>.<TestName>` | Running a specific subtest | `test.Plane.ThrottleFailsafe` |
| `test.<Vehicle>` | Running ALL tests for a vehicle | `test.QuadPlane` |
| `<TestName>` (bare name) | Also works — matched via fnmatch against step list | `ThrottleFailsafe` |

The `test.<Vehicle>.<TestName>` format is most reliable. When using `ap-build test submit`, provide names as positional args:

```bash
ap-build test submit QuadPlane \
    test.QuadPlane.GPSDeniedQLoiterExtPos \
    test.QuadPlane.GPSDeniedVTOLTransitionExtPos \
    --remote jack7169 --ref feature/my-branch
```

### Vehicle Parameter

The `vehicle` argument (first positional arg to `ap-build test submit`, or `vehicle` field in the batch API JSON) controls which SITL binary gets compiled via `waf <vehicle>`. It must match the binary needed by the test:

| Vehicle | Builds | Tests that need it |
|---------|--------|--------------------|
| `Plane` | `arduplane` | `test.Plane.*` |
| `Copter` | `arducopter` | `test.Copter.*` |
| `QuadPlane` | `arduplane` | `test.QuadPlane.*` |
| `Rover` | `ardurover` | `test.Rover.*` |
| `Sub` | `ardusub` | `test.Sub.*` |

Do NOT mix vehicles in a single batch — a batch with `vehicle: "Plane"` will only build `arduplane`, and any `test.Copter.*` tests in that batch will fail with "Binary does not exist".

### Remote and Ref

The `--remote` and `--ref` flags tell the server which git remote and branch/tag to fetch and build from **in the server's git repo**:

- `--remote origin` = upstream ArduPilot (`https://github.com/ardupilot/ardupilot.git`)
- `--remote jack7169` = Jack's fork (`https://github.com/jack7169/ardupilot-jack.git`)
- `--ref master` = the master branch on the specified remote
- `--ref feature/vtol-yaw-alignment` = a feature branch (must exist on the specified remote)

If a branch only exists on `jack7169`, you MUST use `--remote jack7169`. Using `--remote origin` will fail with "couldn't find remote ref".

## Git Remote Management (Server)

The server starts with only `origin` (upstream ArduPilot). To test branches from forks, you must add the fork as a remote:

```bash
# Check current server remotes
ap-build git remotes

# Add Jack's fork
ap-build git add-remote jack7169 https://github.com/jack7169/ardupilot-jack.git

# Verify branches are available
ap-build git branches --remote jack7169
```

### After Server Restore / Rebuild

When Docker volumes are wiped (server restore, rebuild, etc.), the server starts fresh:
- The ArduPilot repo auto-clones on startup (takes ~10-15 min with submodules)
- **All git remotes except `origin` are gone** — you must re-add them via `ap-build git add-remote`
- Build caches are empty — first build per vehicle will be slow (~30s compile)
- Test results from before the wipe are gone (except buildlogs on bind mount)

Standard post-restore checklist:
1. Wait for container startup: `curl http://100.99.196.120:8000/autotest/api/status` until `repo_exists: true`
2. Re-add fork remotes: `ap-build git add-remote jack7169 https://github.com/jack7169/ardupilot-jack.git`
3. Submit a smoke test: `ap-build test submit Plane test.Plane.ThrottleFailsafe`

## Server Access

| Method | URL |
|--------|-----|
| LAN | `http://192.168.50.45:8000` |
| Tailscale | `http://100.99.196.120:8000` |
| Public | `https://jforbes.us` (Cloudflare Zero Trust) |

SSH: `ssh root@100.99.196.120` (via Tailscale) or `ssh root@192.168.50.45` (LAN)

## Server Paths (on Unraid host)

| Path | Purpose |
|------|---------|
| `/mnt/user/appdata/ardupilot/servers-repo/` | This repo |
| `/mnt/user/appdata/ardupilot/servers-repo/docker/bundled/` | Compose file + Dockerfile |
| `/mnt/user/appdata/ardupilot/servers-repo/docker/bundled/.env` | Secrets (tunnel token, etc.) |

## Batch API (Direct curl)

When using the API directly (without `ap-build`), the batch endpoint is:

```bash
# Submit tests
curl -X POST http://100.99.196.120:8000/autotest/api/tests/batch \
  -H "Content-Type: application/json" \
  -d '{
    "vehicle": "QuadPlane",
    "tests": ["test.QuadPlane.ThrottleFailsafe", "test.QuadPlane.AHRSTrim"],
    "remote": "jack7169",
    "ref": "feature/my-branch"
  }'

# Check status
curl http://100.99.196.120:8000/autotest/api/tests/<test_id>

# Check batch
curl http://100.99.196.120:8000/autotest/api/batches/<batch_id>
```

## Build and Deploy

```bash
ssh root@100.99.196.120
cd /mnt/user/appdata/ardupilot/servers-repo
git pull
git submodule update --init
docker build -f docker/bundled/Dockerfile -t bundled-ardupilot .
cd docker/bundled && docker-compose up -d
```
