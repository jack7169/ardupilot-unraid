# ArduPilot Build & Test Server — Developer Access

A self-hosted server for building custom ArduPilot firmware and running SITL autotests. Submit builds, run tests in parallel, and download firmware artifacts — all from a browser or CLI.

**Server:** https://jforbes.us
**Password:** _(provided separately)_

## Browser Access

1. Go to https://jforbes.us
2. Enter the shared password
3. Session lasts 24 hours

**Key pages:**

| Page | URL | Description |
|------|-----|-------------|
| Build Dashboard | `/` | View/submit firmware builds, download artifacts |
| Add Build | `/add_build` | Select vehicle, version, board, features |
| Tests | `/autotest` | Submit SITL tests, view live results |
| Results | `/results/` | Browse test logs, dataflash, tlogs |
| Status | `/status` | Server health and service status |
| API Docs | `/autotest/api/docs` | Interactive Swagger UI |

## CLI Access (`ap-build`)

A command-line tool that talks to the server's REST API. Every command submits HTTP requests — it does not interact with your local git repo.

### Setup

```bash
# Download the CLI and make it executable (requires curl and jq)
curl -o ap-build https://jforbes.us/cli/ap-build && chmod +x ap-build
sudo mv ap-build /usr/local/bin/

# Point to the public server and authenticate
export AP_BUILD_URL=https://jforbes.us
ap-build login    # Enter password when prompted — session lasts 24h
```

To skip the interactive prompt, set `AP_AUTH_PASSWORD`:
```bash
export AP_AUTH_PASSWORD=<password>
```

### Build Firmware

```bash
# Browse available options
ap-build list vehicles
ap-build list versions plane
ap-build list boards plane <version_id>
ap-build list features plane <version_id> CubeOrangePlus

# Submit a build
ap-build submit plane <version_id> CubeOrangePlus --all-features

# Monitor and download
ap-build logs <build_id> --follow
ap-build download <build_id> --output firmware.tar.gz
```

### Run SITL Tests

Tests use ArduPilot's `autotest.py` naming: `test.<Vehicle>.<TestName>`

| Vehicle | Builds | Example test |
|---------|--------|-------------|
| `Plane` | `arduplane` | `test.Plane.ThrottleFailsafe` |
| `Copter` | `arducopter` | `test.Copter.MotorFail` |
| `QuadPlane` | `arduplane` | `test.QuadPlane.AHRSTrim` |
| `Rover` | `ardurover` | `test.Rover.DriveRTL` |
| `Sub` | `ardusub` | `test.Sub.DiveManual` |

Do not mix vehicles in a single batch — each batch compiles one binary.

```bash
# Run a single test against upstream master
ap-build test submit Plane test.Plane.ThrottleFailsafe

# Run against a fork branch
ap-build test submit Plane test.Plane.ThrottleFailsafe \
    --remote jack7169 --ref feature/my-branch

# Run multiple tests in parallel (auto-generates a batch)
ap-build test submit QuadPlane \
    test.QuadPlane.GPSDeniedQLoiterExtPos \
    test.QuadPlane.GPSDeniedVTOLTransitionExtPos \
    test.QuadPlane.AHRSTrim \
    --remote jack7169 --ref feature/my-branch

# Watch live progress
ap-build batch watch <batch_id>

# Compact pass/fail summary
ap-build batch summary <batch_id>
```

### Git Remotes

The server has its own ArduPilot git repo. `origin` is upstream ArduPilot. To test branches from a fork, add the fork as a remote first:

```bash
# Check what remotes exist
ap-build git remotes

# Add your fork
ap-build git add-remote <your-github-username> https://github.com/<user>/ardupilot.git

# Verify your branch is accessible
ap-build git branches --remote <your-github-username>

# Now submit tests against your branch
ap-build test submit Plane test.Plane.ThrottleFailsafe \
    --remote <your-github-username> --ref <your-branch>
```

### Quick Reference

```
ap-build login                              Authenticate (24h session)
ap-build list vehicles|versions|boards|...  Browse available options
ap-build submit <vehicle> <ver> <board>     Submit firmware build
ap-build status <build_id>                  Check build status
ap-build logs <build_id> [--follow]         View build logs
ap-build download <build_id>                Download firmware

ap-build test submit <vehicle> <tests...>   Submit SITL tests
ap-build test list                          List recent tests
ap-build test watch <test_id>               Watch live test output
ap-build batch watch <batch_id>             Watch batch progress
ap-build batch summary <batch_id>           Pass/fail summary

ap-build git remotes                        List server git remotes
ap-build git add-remote <name> <url>        Add a fork
ap-build git branches --remote <name>       List remote branches
```

## API Docs

Interactive Swagger UI: https://jforbes.us/autotest/api/docs

## Notes

- Tests run with `--speedup -1` (unlimited simulation speed) for fast, deterministic results
- Up to 50 tests run in parallel — a batch compiles the binary once and shares it
- Build caches are shared: same commit + vehicle = instant binary reuse
- Test results include dataflash logs, tlogs, and autotest output at `/results/<test_id>/`
