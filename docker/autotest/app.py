"""
Autotest service — runs ArduPilot SITL tests and exposes results via API.
Supports concurrent tests using git worktrees for isolation.
"""
import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="ArduPilot Autotest", docs_url="/autotest/api/docs", redoc_url=None)

WORKDIR = Path(os.environ.get("AUTOTEST_WORKDIR", "/workdir"))
ARDUPILOT_DIR = WORKDIR / "ardupilot"
WORKTREES_DIR = WORKDIR / "worktrees"
RESULTS_DIR = Path(os.environ.get("AUTOTEST_RESULTS_DIR", "/results"))
BUILDLOGS_DIR = Path(os.environ.get("BUILDLOGS_DIR", "/buildlogs"))

# Serialize git operations — concurrent submodule updates deadlock on shared .git
git_lock = asyncio.Lock()

# SITL instance pool — each instance gets unique ports via -I N (port + N*10)
# This allows concurrent SITL execution without port conflicts
MAX_SITL_INSTANCES = 10
sitl_instance_pool = asyncio.Queue()
for _i in range(MAX_SITL_INSTANCES):
    sitl_instance_pool.put_nowait(_i)

tests: dict[str, dict] = {}

# Cache of ready-to-copy template worktrees keyed by commit SHA
# Each entry: {"path": Path, "last_used": float}
template_cache: dict[str, dict] = {}
TEMPLATES_DIR = WORKDIR / "templates"
MAX_CACHED_TEMPLATES = 10

# Cache of pre-built templates keyed by (commit, vehicle, waf_configure, waf_build)
# Each entry: {"path": Path, "last_used": float}
build_cache: dict[str, dict] = {}
BUILD_TEMPLATES_DIR = WORKDIR / "build_templates"
MAX_CACHED_BUILDS = 20
build_lock = asyncio.Lock()


def save_test_metadata(test_info: dict):
    """Persist test metadata to disk as JSON."""
    meta_path = RESULTS_DIR / test_info["test_id"] / "meta.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "test_id": test_info["test_id"],
        "batch_id": test_info.get("batch_id"),
        "vehicle": test_info["vehicle"],
        "test": test_info["test"],
        "remote": test_info["remote"],
        "ref": test_info["ref"],
        "state": test_info["state"],
        "waf_configure_args": test_info.get("waf_configure_args", []),
        "waf_build_args": test_info.get("waf_build_args", []),
        "created_at": test_info["created_at"],
        "finished_at": test_info.get("finished_at"),
    }
    meta_path.write_text(json.dumps(meta, indent=2))


def load_persisted_tests():
    """Reload test metadata from disk on startup."""
    if not RESULTS_DIR.exists():
        return
    count = 0
    for test_dir in RESULTS_DIR.iterdir():
        if not test_dir.is_dir():
            continue
        meta_path = test_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
            test_id = meta["test_id"]
            # Mark any previously-running tests as ERROR (interrupted by restart)
            if meta["state"] in ("PENDING", "UPDATING", "BUILDING", "TESTING"):
                meta["state"] = "ERROR"
                meta["finished_at"] = meta.get("finished_at") or time.time()
                meta_path.write_text(json.dumps(meta, indent=2))
            # Load log from disk
            log_path = test_dir / "test.log"
            log_text = log_path.read_text() if log_path.exists() else ""
            tests[test_id] = {
                **meta,
                "log": log_text,
                "task": None,
                "process": None,
                "worktree": None,
            }
            count += 1
        except Exception as e:
            logger.warning(f"Failed to load test metadata from {test_dir}: {e}")
    logger.info(f"Loaded {count} persisted test(s) from disk")


@app.on_event("startup")
async def startup():
    """Load persisted tests and clean up orphaned worktrees."""
    # 1. Reload test history from disk
    load_persisted_tests()

    # 2. Clean up orphaned test copies and buildlogs (plain dirs, just rm)
    if WORKTREES_DIR.exists():
        for item in WORKTREES_DIR.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
                logger.info(f"Cleaned up test copy: {item.name}")

    # 3. Reload build templates from disk into cache
    if BUILD_TEMPLATES_DIR.exists():
        for item in BUILD_TEMPLATES_DIR.iterdir():
            if item.is_dir() and item.name.startswith("bld-"):
                # Dir name: bld-<16char_key>-<vehicle>
                # Extract key: everything between first and last hyphen-segment
                parts = item.name.split("-")
                # parts = ["bld", "<key>", "<vehicle>"] but key may be 8 or 16 chars
                key = parts[1] if len(parts) >= 3 else item.name
                build_cache[key] = {"path": item, "last_used": time.time()}
                logger.info(f"Restored build template: {item.name} (key={key})")
        logger.info(f"Restored {len(build_cache)} build template(s) from disk")

    # 4. Reload source templates into cache
    if (ARDUPILOT_DIR / "waf").exists() and TEMPLATES_DIR.exists():
        for tpl in TEMPLATES_DIR.iterdir():
            if tpl.is_dir() and tpl.name.startswith("tpl-"):
                commit_prefix = tpl.name.removeprefix("tpl-")
                # Find full commit SHA
                rc, out = await run_cmd(
                    ["git", "rev-parse", commit_prefix],
                    cwd=ARDUPILOT_DIR, timeout=10,
                )
                commit = out.strip() if rc == 0 else commit_prefix
                template_cache[commit] = {"path": tpl, "last_used": time.time()}
                logger.info(f"Restored source template: {tpl.name} -> {commit[:12]}")
        logger.info(f"Restored {len(template_cache)} source template(s) from disk")
        await run_cmd(["git", "worktree", "prune"], cwd=ARDUPILOT_DIR)
    logger.info("Startup cleanup complete")


# --- Models ---

class TestRequest(BaseModel):
    vehicle: str = "Plane"
    test: str = "test.PlaneTests1b"
    remote: str = "origin"
    ref: str = "master"
    waf_configure_args: list[str] = []
    waf_build_args: list[str] = []
    batch_id: str | None = None


class GitUpdateRequest(BaseModel):
    remote_url: str | None = None
    remote_name: str = "origin"
    ref: str = "master"


class AddRemoteRequest(BaseModel):
    name: str
    url: str


# --- Helpers ---

async def run_cmd(cmd: list[str], cwd: str | Path | None = None,
                  timeout: int = 300) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode(errors="replace")
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "Command timed out"


async def ensure_repo():
    if not (ARDUPILOT_DIR / "waf").exists():
        logger.info("Cloning ardupilot repository...")
        rc, out = await run_cmd(
            ["git", "clone", "--recurse-submodules",
             "https://github.com/ArduPilot/ardupilot.git", str(ARDUPILOT_DIR)],
            timeout=600,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to clone: {out}")
        logger.info("Clone complete")
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    BUILD_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)


async def commit_is_local(commit_or_ref: str, remote_name: str) -> tuple[bool, str | None]:
    """Check if a commit is already available locally. Returns (found, sha)."""
    # Try as full SHA or tag
    rc, out = await run_cmd(
        ["git", "cat-file", "-t", commit_or_ref],
        cwd=ARDUPILOT_DIR,
    )
    if rc == 0:
        rc2, sha = await run_cmd(
            ["git", "rev-parse", "--verify", f"{commit_or_ref}^{{commit}}"],
            cwd=ARDUPILOT_DIR,
        )
        if rc2 == 0:
            return True, sha.strip()

    # Try as remote/branch
    if "/" not in commit_or_ref and len(commit_or_ref) < 40:
        rc, out = await run_cmd(
            ["git", "rev-parse", "--verify", f"{remote_name}/{commit_or_ref}"],
            cwd=ARDUPILOT_DIR,
        )
        if rc == 0:
            return True, out.strip()

    return False, None


async def fetch_remote(remote_name: str, remote_url: str | None = None, ref: str | None = None) -> str:
    """Fetch from a remote, optionally adding/updating the URL first."""
    await ensure_repo()
    output_lines = []

    if remote_url:
        rc, out = await run_cmd(
            ["git", "remote", "get-url", remote_name], cwd=ARDUPILOT_DIR
        )
        if rc != 0:
            rc, out = await run_cmd(
                ["git", "remote", "add", remote_name, remote_url],
                cwd=ARDUPILOT_DIR,
            )
            output_lines.append(f"Added remote {remote_name}: {remote_url}")
        else:
            current_url = out.strip()
            if current_url != remote_url:
                await run_cmd(
                    ["git", "remote", "set-url", remote_name, remote_url],
                    cwd=ARDUPILOT_DIR,
                )
                output_lines.append(
                    f"Updated remote {remote_name}: {current_url} -> {remote_url}"
                )

    fetch_cmd = ["git", "fetch", remote_name, "--prune", "--tags"]
    if ref and len(ref) < 40:
        # Explicitly fetch the target branch to ensure we get latest commits
        fetch_cmd.append(f"+refs/heads/{ref}:refs/remotes/{remote_name}/{ref}")
    rc, out = await run_cmd(fetch_cmd, cwd=ARDUPILOT_DIR, timeout=300)
    output_lines.append(f"Fetch {remote_name}: {'OK' if rc == 0 else 'FAILED'}")
    if rc != 0:
        output_lines.append(out)

    return "\n".join(output_lines)


async def resolve_ref(remote_name: str, ref: str) -> str:
    """Resolve a ref to a commit SHA in the main repo."""
    if len(ref) < 40:
        rc, out = await run_cmd(
            ["git", "rev-parse", "--verify", f"{remote_name}/{ref}"],
            cwd=ARDUPILOT_DIR,
        )
        if rc == 0:
            return out.strip()

    rc, out = await run_cmd(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=ARDUPILOT_DIR,
    )
    if rc == 0:
        return out.strip()

    return ref


async def get_or_create_template(commit: str) -> Path:
    """
    Get a cached template worktree for a commit, or create one.
    Templates are git worktrees with submodules initialized — ready to copy.
    Must be called under git_lock.
    """
    if commit in template_cache:
        entry = template_cache[commit]
        if entry["path"].exists():
            entry["last_used"] = time.time()
            logger.info(f"Template cache HIT for {commit[:12]}")
            return entry["path"]
        else:
            del template_cache[commit]

    logger.info(f"Template cache MISS for {commit[:12]}, creating...")

    # Evict oldest if at capacity
    if len(template_cache) >= MAX_CACHED_TEMPLATES:
        oldest_key = min(template_cache, key=lambda k: template_cache[k]["last_used"])
        oldest = template_cache.pop(oldest_key)
        logger.info(f"Evicting template {oldest_key[:12]}")
        await run_cmd(
            ["git", "worktree", "unlock", str(oldest["path"])],
            cwd=ARDUPILOT_DIR, timeout=10,
        )
        rc, _ = await run_cmd(
            ["git", "worktree", "remove", "--force", str(oldest["path"])],
            cwd=ARDUPILOT_DIR, timeout=60,
        )
        if rc != 0:
            shutil.rmtree(oldest["path"], ignore_errors=True)
        await run_cmd(["git", "worktree", "prune"], cwd=ARDUPILOT_DIR)

    # Create template worktree
    tpl_path = TEMPLATES_DIR / f"tpl-{commit[:12]}"
    if tpl_path.exists():
        shutil.rmtree(tpl_path, ignore_errors=True)

    rc, out = await run_cmd(
        ["git", "worktree", "add", "--detach", str(tpl_path), commit],
        cwd=ARDUPILOT_DIR, timeout=120,
    )
    if rc != 0:
        raise RuntimeError(f"Failed to create template worktree: {out}")

    rc, out = await run_cmd(
        ["git", "submodule", "update", "--init", "--recursive"],
        cwd=tpl_path, timeout=300,
    )
    if rc != 0:
        logger.warning(f"Submodule update issue for template {commit[:12]}: {out}")

    # Lock the template so it's not accidentally pruned
    await run_cmd(
        ["git", "worktree", "lock", str(tpl_path)],
        cwd=ARDUPILOT_DIR, timeout=10,
    )

    template_cache[commit] = {"path": tpl_path, "last_used": time.time()}
    logger.info(f"Template created for {commit[:12]} at {tpl_path}")
    return tpl_path


async def fast_copy(src: Path, dest: Path) -> None:
    """Fast copy using cp -a --reflink=auto (instant on btrfs)."""
    rc, out = await run_cmd(
        ["cp", "-a", "--reflink=auto", str(src), str(dest)],
        timeout=300,
    )
    if rc != 0:
        raise RuntimeError(f"Failed to copy {src} -> {dest}: {out}")


async def create_test_copy(test_id: str, template_path: Path) -> Path:
    """Fast copy of a build template for a test run. No git or build ops."""
    dest = WORKTREES_DIR / test_id
    await fast_copy(template_path, dest)
    return dest


def cleanup_test_copy(test_id: str):
    """Remove a test copy. Just rm -rf, no git worktree ops needed."""
    copy_path = WORKTREES_DIR / test_id
    if copy_path.exists():
        shutil.rmtree(copy_path, ignore_errors=True)


# --- Build cache ---

def build_cache_key(commit: str, vehicle: str,
                    waf_configure_args: list[str],
                    waf_build_args: list[str]) -> str:
    """Deterministic cache key for a build configuration."""
    import hashlib
    raw = f"{commit}:{vehicle}:{sorted(waf_configure_args)}:{sorted(waf_build_args)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def get_or_create_build_template(
    commit: str, vehicle: str,
    waf_configure_args: list[str], waf_build_args: list[str],
    source_template: Path, log_cb=None,
) -> Path:
    """
    Get a cached pre-built template, or build one from the source template.
    Must be called under build_lock.
    """
    key = build_cache_key(commit, vehicle, waf_configure_args, waf_build_args)

    if key in build_cache and build_cache[key]["path"].exists():
        build_cache[key]["last_used"] = time.time()
        if log_cb:
            log_cb(f"Build cache HIT ({key[:8]}): {vehicle} already built for {commit[:12]}\n")
        logger.info(f"Build cache HIT: {key[:8]}")
        return build_cache[key]["path"]

    if log_cb:
        log_cb(f"Build cache MISS ({key[:8]}): building {vehicle} for {commit[:12]}...\n")
    logger.info(f"Build cache MISS: {key[:8]}, building...")

    # Evict oldest if at capacity
    if len(build_cache) >= MAX_CACHED_BUILDS:
        oldest_key = min(build_cache, key=lambda k: build_cache[k]["last_used"])
        oldest = build_cache.pop(oldest_key)
        if oldest["path"].exists():
            shutil.rmtree(oldest["path"], ignore_errors=True)
        logger.info(f"Evicted build template {oldest_key[:8]}")

    # Copy source template to build template dir
    bld_path = BUILD_TEMPLATES_DIR / f"bld-{key}-{vehicle.lower()}"
    if bld_path.exists():
        shutil.rmtree(bld_path, ignore_errors=True)

    await fast_copy(source_template, bld_path)

    # Configure
    configure_cmd = ["python3", "./waf", "configure", "--board", "sitl"]
    configure_cmd.extend(waf_configure_args)
    if log_cb:
        log_cb(f"=== Configure: {' '.join(configure_cmd)} ===\n")

    rc, out = await run_cmd(configure_cmd, cwd=bld_path, timeout=120)
    if log_cb:
        log_cb(out + "\n")
    if rc != 0:
        shutil.rmtree(bld_path, ignore_errors=True)
        raise RuntimeError(f"Build configure failed: {out[-200:]}")

    # Build
    cpu_count = os.cpu_count() or 4
    build_cmd = ["python3", "./waf", vehicle.lower(), f"-j{cpu_count}"]
    build_cmd.extend(waf_build_args)
    if log_cb:
        log_cb(f"=== Build: {' '.join(build_cmd)} ===\n")

    rc, out = await run_cmd(build_cmd, cwd=bld_path, timeout=600)
    if log_cb:
        log_cb(out + "\n")
    if rc != 0:
        shutil.rmtree(bld_path, ignore_errors=True)
        raise RuntimeError(f"Build failed: {out[-200:]}")

    build_cache[key] = {"path": bld_path, "last_used": time.time()}
    if log_cb:
        log_cb(f"Build template cached: {bld_path}\n\n")
    logger.info(f"Build template created: {key[:8]} at {bld_path}")
    return bld_path


# --- Test runner ---

def flush_log(test_info: dict):
    """Flush in-memory log and metadata to disk immediately."""
    log_path = RESULTS_DIR / test_info["test_id"] / "test.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(test_info["log"])
    save_test_metadata(test_info)


def collect_artifacts(test_id: str, wt_path: Path, test_buildlogs: Path):
    """Copy all build artifacts to the persistent results directory."""
    dest = RESULTS_DIR / test_id
    dest.mkdir(parents=True, exist_ok=True)

    # 1. Autotest buildlogs (tlogs, output files, badge, index.html)
    if test_buildlogs.exists():
        for item in test_buildlogs.iterdir():
            try:
                target = dest / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, target)
            except Exception as e:
                logger.warning(f"Failed to copy {item}: {e}")

    # 2. SITL dataflash logs (BIN files)
    sitl_logs = wt_path / "logs"
    if sitl_logs.exists():
        logs_dest = dest / "dataflash"
        logs_dest.mkdir(exist_ok=True)
        for item in sitl_logs.iterdir():
            try:
                shutil.copy2(item, logs_dest / item.name)
            except Exception as e:
                logger.warning(f"Failed to copy dataflash {item}: {e}")

    # 3. Also copy to web-visible buildlogs directory for /results/ page
    if BUILDLOGS_DIR.exists():
        web_dest = BUILDLOGS_DIR / f"autotest_{test_id}"
        try:
            if web_dest.exists():
                shutil.rmtree(web_dest)
            shutil.copytree(dest, web_dest, dirs_exist_ok=True)
        except Exception as e:
            logger.warning(f"Failed to copy to buildlogs: {e}")

    logger.info(f"Artifacts collected for {test_id}: {[f.name for f in dest.iterdir()]}")


async def run_test_async(test_id: str, vehicle: str, test_target: str,
                         remote: str, ref: str,
                         waf_configure_args: list[str],
                         waf_build_args: list[str]):
    test_info = tests[test_id]
    test_info["state"] = "UPDATING"

    wt_path = None
    # Each test gets its own buildlogs dir so concurrent tests don't clobber each other
    test_buildlogs = WORKTREES_DIR / f"buildlogs_{test_id}"
    test_buildlogs.mkdir(parents=True, exist_ok=True)
    test_env = {**os.environ, "BUILDLOGS": str(test_buildlogs)}

    try:
        test_info["log"] = f"=== Preparing source for {remote}/{ref} ===\n"
        flush_log(test_info)

        await ensure_repo()

        # All git + template ops under one lock acquisition to avoid redundant fetches
        async with git_lock:
            # Check if ref resolves locally first
            found, commit = await commit_is_local(ref, remote)

            if found and commit and commit in template_cache:
                # Fast path: commit local + template cached = zero git API calls
                test_info["log"] += f"Cache HIT: {commit[:12]} (no fetch needed)\n"
                template_path = template_cache[commit]["path"]
                template_cache[commit]["last_used"] = time.time()
            else:
                # Need to fetch from remote
                if not found or not commit:
                    test_info["log"] += f"Ref not local, fetching {remote}...\n"
                    flush_log(test_info)
                    fetch_out = await fetch_remote(remote, ref=ref)
                    test_info["log"] += fetch_out + "\n"
                else:
                    test_info["log"] += f"Ref local ({commit[:12]}) but no template, skipping fetch\n"

                commit = await resolve_ref(remote, ref)
                test_info["log"] += f"Resolved to: {commit}\n"

                # Double-check cache (another test may have created it while we waited)
                if commit in template_cache and template_cache[commit]["path"].exists():
                    test_info["log"] += f"Template appeared while waiting (cache HIT)\n"
                    template_path = template_cache[commit]["path"]
                    template_cache[commit]["last_used"] = time.time()
                else:
                    template_path = await get_or_create_template(commit)

            test_info["log"] += f"Template: {template_path}\n"
            flush_log(test_info)

        # Get or create pre-built template (serialized to avoid redundant builds)
        test_info["state"] = "BUILDING"
        def build_log(msg):
            test_info["log"] += msg
            flush_log(test_info)

        async with build_lock:
            try:
                build_tpl = await get_or_create_build_template(
                    commit, vehicle, waf_configure_args, waf_build_args,
                    template_path, log_cb=build_log,
                )
            except RuntimeError as e:
                test_info["state"] = "FAILURE"
                test_info["log"] += f"\n{e}\n"
                return

        # Fast copy from pre-built template — no git or build ops
        test_info["log"] += f"=== Copying pre-built source for {test_id} ===\n"
        flush_log(test_info)
        wt_path = await create_test_copy(test_id, build_tpl)
        test_info["worktree"] = str(wt_path)
        test_info["log"] += f"Copy ready: {wt_path}\n\n"
        flush_log(test_info)

        # Grab a SITL instance slot (each uses unique ports: base + instance*10)
        test_info["state"] = "QUEUED"
        test_info["log"] += f"\n=== Waiting for SITL instance ({sitl_instance_pool.qsize()}/{MAX_SITL_INSTANCES} free) ===\n"
        flush_log(test_info)

        instance_num = await sitl_instance_pool.get()
        try:
            test_info["state"] = "TESTING"
            test_info["log"] += f"=== SITL instance {instance_num} (ports {5760 + instance_num*10}+) ===\n"
            test_info["log"] += f"=== Running: {test_target} ===\n"
            test_info["log"] += f"=== BUILDLOGS: {test_buildlogs} ===\n"
            test_info["log"] += "=" * 60 + "\n"
            flush_log(test_info)

            # Port isolation for concurrent SITL instances.
            # SITL -I N offsets ALL ports by N*10: base_port, serial ports,
            # RC input, etc. We must also patch the autotest framework so
            # it connects to the same offset ports.
            port_offset = instance_num * 10

            # 1. Shim SITL binaries to inject -I <instance>
            bin_dir = wt_path / "build" / "sitl" / "bin"
            if bin_dir.exists():
                for binary in bin_dir.iterdir():
                    if binary.is_file() and binary.stat().st_mode & 0o111:
                        real = binary.with_suffix(".real")
                        if not real.exists():
                            binary.rename(real)
                            binary.write_text(
                                f"#!/bin/bash\nexec {real} -I {instance_num} \"$@\"\n"
                            )
                            binary.chmod(0o755)

            # 2. Patch vehicle_test_suite.py — all port methods
            vts_path = wt_path / "Tools" / "autotest" / "vehicle_test_suite.py"
            if vts_path.exists():
                vts = vts_path.read_text()
                # adjust_ardupilot_port: used for MAVLink ports (5760, 5762, 5763)
                vts = vts.replace(
                    "def adjust_ardupilot_port(self, port):\n"
                    "        '''adjust port in case we do not wish to use the default range (5760 and 5501 etc)'''\n"
                    "        return port",
                    "def adjust_ardupilot_port(self, port):\n"
                    "        '''adjust port in case we do not wish to use the default range (5760 and 5501 etc)'''\n"
                    f"        return port + {port_offset}",
                )
                # sitl_rcin_port: RC input port (5501)
                vts = vts.replace(
                    "def sitl_rcin_port(self, offset=0):\n"
                    "        if offset > 2:\n"
                    "            raise ValueError(\"offset too large\")\n"
                    "        return 5501 + offset",
                    "def sitl_rcin_port(self, offset=0):\n"
                    "        if offset > 2:\n"
                    "            raise ValueError(\"offset too large\")\n"
                    f"        return {5501 + port_offset} + offset",
                )
                # spare_network_port: auxiliary ports (8000+)
                vts = vts.replace(
                    "def spare_network_port(self, offset=0):\n"
                    "        '''returns a network port which should be able to be bound'''\n"
                    "        if offset > 2:\n"
                    "            raise ValueError(\"offset too large\")\n"
                    "        return 8000 + offset",
                    "def spare_network_port(self, offset=0):\n"
                    "        '''returns a network port which should be able to be bound'''\n"
                    "        if offset > 2:\n"
                    "            raise ValueError(\"offset too large\")\n"
                    f"        return {8000 + port_offset} + offset",
                )
                vts_path.write_text(vts)

            # 3. Patch util.py — mavproxy default rcin port
            util_path = wt_path / "Tools" / "autotest" / "pysim" / "util.py"
            if util_path.exists():
                util_src = util_path.read_text()
                util_src = util_src.replace(
                    "sitl_rcin_port=5501,",
                    f"sitl_rcin_port={5501 + port_offset},",
                )
                util_path.write_text(util_src)

            run_env = test_env

            proc = await asyncio.create_subprocess_exec(
                "python3", "Tools/autotest/autotest.py", test_target,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=wt_path,
                env=run_env,
            )
            test_info["process"] = proc

            line_count = 0
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                test_info["log"] += line.decode(errors="replace")
                line_count += 1
                if line_count % 100 == 0:
                    flush_log(test_info)

            await proc.wait()
            test_info["process"] = None
        finally:
            # Return instance slot to pool
            sitl_instance_pool.put_nowait(instance_num)

        if proc.returncode == 0:
            test_info["state"] = "SUCCESS"
            test_info["log"] += "\nAll tests passed!\n"
        else:
            test_info["state"] = "FAILURE"
            test_info["log"] += f"\nTests failed (exit code {proc.returncode})\n"

    except asyncio.CancelledError:
        test_info["state"] = "CANCELLED"
        test_info["log"] += "\nTest cancelled by user\n"
    except Exception as e:
        test_info["state"] = "ERROR"
        test_info["log"] += f"\nUnexpected error: {e}\n"
        logger.exception("Test runner error")
    finally:
        test_info["process"] = None
        test_info["finished_at"] = time.time()

        # Final log flush
        flush_log(test_info)

        # Collect all artifacts then remove the copy (no git ops needed)
        if wt_path:
            collect_artifacts(test_id, wt_path, test_buildlogs)
            cleanup_test_copy(test_id)

        # Clean up isolated buildlogs dir
        if test_buildlogs.exists():
            shutil.rmtree(test_buildlogs, ignore_errors=True)


def test_summary(t: dict) -> dict:
    return {
        "test_id": t["test_id"],
        "batch_id": t.get("batch_id"),
        "vehicle": t["vehicle"],
        "test": t["test"],
        "remote": t["remote"],
        "ref": t["ref"],
        "state": t["state"],
        "waf_configure_args": t.get("waf_configure_args", []),
        "waf_build_args": t.get("waf_build_args", []),
        "created_at": t["created_at"],
        "finished_at": t.get("finished_at"),
    }


# --- Discovery API ---

@app.get("/autotest/api/vehicles")
async def list_test_vehicles():
    """List vehicles available for testing."""
    if not (ARDUPILOT_DIR / "waf").exists():
        return {"vehicles": []}
    rc, out = await run_cmd(
        ["python3", "Tools/autotest/autotest.py", "--list-vehicles-test"],
        cwd=ARDUPILOT_DIR, timeout=30,
    )
    if rc != 0:
        return {"vehicles": []}
    # Output is space-separated on one line
    vehicles = [v.strip() for v in out.strip().split() if v.strip()]
    return {"vehicles": sorted(vehicles)}


@app.get("/autotest/api/subtests")
async def list_subtests(vehicle: str = "Plane"):
    """List available subtests for a vehicle."""
    if not (ARDUPILOT_DIR / "waf").exists():
        return {"subtests": []}
    rc, out = await run_cmd(
        ["python3", "Tools/autotest/autotest.py",
         f"--list-subtests-for-vehicle={vehicle}"],
        cwd=ARDUPILOT_DIR, timeout=30,
    )
    if rc != 0:
        return {"subtests": []}
    # Output is space-separated on one line
    names = [n.strip() for n in out.strip().split() if n.strip()]
    return {"subtests": [{"name": n} for n in sorted(names)]}


@app.get("/autotest/api/test-suites")
async def list_test_suites():
    """List top-level test suites (test.Plane, test.Copter, etc.)."""
    if not (ARDUPILOT_DIR / "waf").exists():
        return {"suites": []}
    rc, out = await run_cmd(
        ["python3", "Tools/autotest/autotest.py", "--list"],
        cwd=ARDUPILOT_DIR, timeout=30,
    )
    if rc != 0:
        return {"suites": []}
    suites = [s.strip() for s in out.strip().splitlines() if s.strip().startswith("test.")]
    return {"suites": sorted(suites)}


# --- Test API ---

@app.get("/autotest/api/status")
async def api_status():
    running_states = ("UPDATING", "BUILDING", "TESTING", "QUEUED")
    running = [t for t in tests.values() if t["state"] in running_states]
    return {
        "status": "busy" if running else "idle",
        "running_count": len(running),
        "total_tests": len(tests),
        "repo_exists": (ARDUPILOT_DIR / "waf").exists(),
    }


@app.get("/autotest/api/tests")
async def list_tests(limit: int = 100, batch_id: str | None = None):
    filtered = tests.values()
    if batch_id:
        filtered = [t for t in filtered if t.get("batch_id") == batch_id]
    sorted_tests = sorted(filtered, key=lambda t: t["created_at"], reverse=True)
    return [test_summary(t) for t in sorted_tests[:limit]]


@app.post("/autotest/api/tests")
async def submit_test(req: TestRequest):
    test_id = f"{req.vehicle.lower()}-{uuid.uuid4().hex[:8]}"
    batch_id = req.batch_id or None
    test_info = {
        "test_id": test_id,
        "batch_id": batch_id,
        "vehicle": req.vehicle,
        "test": req.test,
        "remote": req.remote,
        "ref": req.ref,
        "waf_configure_args": req.waf_configure_args,
        "waf_build_args": req.waf_build_args,
        "state": "PENDING",
        "created_at": time.time(),
        "finished_at": None,
        "log": "",
        "task": None,
        "process": None,
        "worktree": None,
    }
    tests[test_id] = test_info

    task = asyncio.create_task(
        run_test_async(
            test_id, req.vehicle, req.test, req.remote, req.ref,
            req.waf_configure_args, req.waf_build_args,
        )
    )
    test_info["task"] = task

    return {"test_id": test_id, "batch_id": batch_id, "status": "submitted"}


@app.get("/autotest/api/tests/{test_id}")
async def get_test(test_id: str):
    if test_id not in tests:
        raise HTTPException(404, f"Test '{test_id}' not found")
    return test_summary(tests[test_id])


@app.get("/autotest/api/tests/{test_id}/logs")
async def get_test_logs(test_id: str, tail: int | None = None):
    if test_id not in tests:
        raise HTTPException(404, f"Test '{test_id}' not found")
    log = tests[test_id]["log"]
    if tail:
        lines = log.splitlines()
        log = "\n".join(lines[-tail:])
    return PlainTextResponse(log)


@app.post("/autotest/api/tests/{test_id}/cancel")
async def cancel_test(test_id: str):
    if test_id not in tests:
        raise HTTPException(404, f"Test '{test_id}' not found")
    t = tests[test_id]
    if t["state"] not in ("UPDATING", "BUILDING", "TESTING", "QUEUED", "PENDING"):
        raise HTTPException(400, f"Test is not running (state: {t['state']})")

    proc = t.get("process")
    if proc:
        proc.kill()
    task = t.get("task")
    if task and not task.done():
        task.cancel()
    t["state"] = "CANCELLED"
    t["finished_at"] = time.time()
    return {"status": "cancelled"}


# --- Batch API ---

@app.get("/autotest/api/batches")
async def list_batches():
    """List all batch IDs with summary counts."""
    batches: dict[str, dict] = {}
    for t in tests.values():
        bid = t.get("batch_id")
        if not bid:
            continue
        if bid not in batches:
            batches[bid] = {
                "batch_id": bid,
                "total": 0, "passed": 0, "failed": 0, "running": 0,
                "vehicle": t["vehicle"], "remote": t["remote"], "ref": t["ref"],
                "created_at": t["created_at"],
            }
        b = batches[bid]
        b["total"] += 1
        if t["state"] == "SUCCESS":
            b["passed"] += 1
        elif t["state"] in ("FAILURE", "ERROR"):
            b["failed"] += 1
        elif t["state"] in ("PENDING", "UPDATING", "BUILDING", "QUEUED", "TESTING"):
            b["running"] += 1
        b["created_at"] = min(b["created_at"], t["created_at"])
    return sorted(batches.values(), key=lambda b: b["created_at"], reverse=True)


@app.get("/autotest/api/batches/{batch_id}")
async def get_batch(batch_id: str):
    """Get all tests in a batch with their summaries."""
    batch_tests = [
        test_summary(t) for t in tests.values()
        if t.get("batch_id") == batch_id
    ]
    if not batch_tests:
        raise HTTPException(404, f"Batch '{batch_id}' not found")
    batch_tests.sort(key=lambda t: t["test"])
    passed = sum(1 for t in batch_tests if t["state"] == "SUCCESS")
    failed = sum(1 for t in batch_tests if t["state"] in ("FAILURE", "ERROR"))
    running = sum(1 for t in batch_tests if t["state"] in ("PENDING", "UPDATING", "BUILDING", "QUEUED", "TESTING"))
    return {
        "batch_id": batch_id,
        "total": len(batch_tests),
        "passed": passed,
        "failed": failed,
        "running": running,
        "tests": batch_tests,
    }


@app.get("/autotest/api/batches/{batch_id}/logs")
async def get_batch_logs(batch_id: str):
    """Get structured logs for all tests in a batch."""
    batch_tests = [
        t for t in tests.values()
        if t.get("batch_id") == batch_id
    ]
    if not batch_tests:
        raise HTTPException(404, f"Batch '{batch_id}' not found")
    batch_tests.sort(key=lambda t: t["test"])
    result = []
    for t in batch_tests:
        result.append({
            "test_id": t["test_id"],
            "test": t["test"],
            "state": t["state"],
            "log": t["log"],
        })
    return {"batch_id": batch_id, "tests": result}


# --- Git API ---

@app.post("/autotest/api/git/update")
async def api_git_update(req: GitUpdateRequest):
    await ensure_repo()
    fetch_out = await fetch_remote(req.remote_name, req.remote_url, ref=req.ref)

    # Checkout in main repo (for browsing, not for tests — tests use worktrees)
    checkout_ref = req.ref
    if len(req.ref) < 40:
        rc, _ = await run_cmd(
            ["git", "rev-parse", "--verify", f"{req.remote_name}/{req.ref}"],
            cwd=ARDUPILOT_DIR,
        )
        if rc == 0:
            checkout_ref = f"{req.remote_name}/{req.ref}"

    rc, out = await run_cmd(
        ["git", "checkout", "-f", checkout_ref], cwd=ARDUPILOT_DIR
    )
    checkout_out = f"Checkout {checkout_ref}: {'OK' if rc == 0 else 'FAILED'}"
    if rc != 0:
        checkout_out += f"\n{out}"

    rc, out = await run_cmd(
        ["git", "submodule", "update", "--init", "--recursive"],
        cwd=ARDUPILOT_DIR, timeout=300,
    )
    sub_out = f"Submodules: {'OK' if rc == 0 else 'FAILED'}"

    rc, out = await run_cmd(["git", "log", "--oneline", "-1"], cwd=ARDUPILOT_DIR)
    head_out = f"HEAD: {out.strip()}"

    return {
        "status": "ok",
        "output": f"{fetch_out}\n{checkout_out}\n{sub_out}\n{head_out}",
    }


@app.get("/autotest/api/git/remotes")
async def list_git_remotes():
    if not (ARDUPILOT_DIR / "waf").exists():
        return {"remotes": {}}
    rc, out = await run_cmd(["git", "remote", "-v"], cwd=ARDUPILOT_DIR)
    remotes = {}
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2 and "(fetch)" in line:
            remotes[parts[0]] = parts[1]
    return {"remotes": remotes}


@app.get("/autotest/api/git/branches")
async def list_git_branches(remote: str = "origin"):
    if not (ARDUPILOT_DIR / "waf").exists():
        return {"branches": []}
    rc, out = await run_cmd(
        ["git", "branch", "-r", "--list", f"{remote}/*"], cwd=ARDUPILOT_DIR
    )
    branches = [
        b.strip().removeprefix(f"{remote}/")
        for b in out.strip().splitlines()
        if "->" not in b
    ]
    return {"branches": sorted(branches)}


@app.get("/autotest/api/git/tags")
async def list_git_tags(remote: str | None = None):
    if not (ARDUPILOT_DIR / "waf").exists():
        return {"tags": []}
    if remote:
        await run_cmd(
            ["git", "fetch", remote, "--tags"], cwd=ARDUPILOT_DIR, timeout=120
        )
    rc, out = await run_cmd(["git", "tag", "--sort=-creatordate"], cwd=ARDUPILOT_DIR)
    tags = [t.strip() for t in out.strip().splitlines() if t.strip()]
    return {"tags": tags[:200]}


@app.post("/autotest/api/git/add-remote")
async def add_git_remote(req: AddRemoteRequest):
    await ensure_repo()
    rc, _ = await run_cmd(
        ["git", "remote", "get-url", req.name], cwd=ARDUPILOT_DIR
    )
    if rc == 0:
        raise HTTPException(400, f"Remote '{req.name}' already exists")

    rc, out = await run_cmd(
        ["git", "remote", "add", req.name, req.url], cwd=ARDUPILOT_DIR
    )
    if rc != 0:
        raise HTTPException(400, f"Failed to add remote: {out}")

    rc, out = await run_cmd(
        ["git", "fetch", req.name, "--tags"],
        cwd=ARDUPILOT_DIR, timeout=300,
    )
    return {
        "status": "ok",
        "output": f"Added remote {req.name} ({req.url})\n"
                  f"Fetch: {'OK' if rc == 0 else out}",
    }
