"""
Autotest service — runs ArduPilot SITL tests and exposes results via API.
Supports concurrent tests using git worktrees for isolation.
"""
import asyncio
import fcntl
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import psutil

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="ArduPilot Autotest", docs_url="/autotest/api/docs", redoc_url=None)

WORKDIR = Path(os.environ.get("AUTOTEST_WORKDIR", "/workdir"))
# Use shared golden repo if available; fall back to per-service clone
_shared = os.environ.get("SHARED_ARDUPILOT_DIR")
ARDUPILOT_DIR = Path(_shared) if _shared else WORKDIR / "ardupilot"
WORKTREES_DIR = WORKDIR / "worktrees"
RESULTS_DIR = Path(os.environ.get("AUTOTEST_RESULTS_DIR", "/results"))
BUILDLOGS_DIR = Path(os.environ.get("BUILDLOGS_DIR", "/buildlogs"))

# --- Locks ---
# Serialize git fetch operations (short-held, ~30s max)
_fetch_lock = asyncio.Lock()
# Per-commit locks for template creation (independent commits build in parallel)
_template_cache_guard = asyncio.Lock()  # protects template_cache dict only
_template_locks: dict[str, asyncio.Lock] = {}
# Per-key locks for builds (different vehicles/configs build in parallel)
_build_cache_guard = asyncio.Lock()  # protects build_cache dict only
_build_key_locks: dict[str, asyncio.Lock] = {}

# SITL instance pool — each instance gets unique ports via -I N (port + N*10)
# This allows concurrent SITL execution without port conflicts
MAX_SITL_INSTANCES = 50
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
MAX_CACHED_BUILDS = 10


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
    # 0. Prime psutil CPU counter (first call with interval=0 always returns 0)
    psutil.cpu_percent(interval=None)

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

    # Evict excess build templates down to MAX_CACHED_BUILDS
    while len(build_cache) > MAX_CACHED_BUILDS:
        oldest_key = min(build_cache, key=lambda k: build_cache[k]["last_used"])
        oldest = build_cache.pop(oldest_key)
        if oldest["path"].exists():
            shutil.rmtree(oldest["path"], ignore_errors=True)
        logger.info(f"Startup eviction: build template {oldest_key[:8]}")

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

        # Evict excess source templates down to MAX_CACHED_TEMPLATES
        while len(template_cache) > MAX_CACHED_TEMPLATES:
            oldest_key = min(template_cache, key=lambda k: template_cache[k]["last_used"])
            oldest = template_cache.pop(oldest_key)
            try:
                await run_cmd(["git", "worktree", "unlock", str(oldest["path"])], cwd=ARDUPILOT_DIR, timeout=10)
            except Exception:
                pass
            try:
                await run_cmd(["git", "worktree", "remove", "--force", str(oldest["path"])], cwd=ARDUPILOT_DIR, timeout=30)
            except Exception:
                shutil.rmtree(oldest["path"], ignore_errors=True)
            logger.info(f"Startup eviction: source template {oldest_key[:12]}")

        await run_cmd(["git", "worktree", "prune"], cwd=ARDUPILOT_DIR)
    logger.info("Startup cleanup complete")


# --- Models ---

class TestRequest(BaseModel):
    vehicle: str = "Plane"
    test: str = "test.PlaneTests1b"
    remote: str = "origin"
    ref: str = "master"
    commit: str | None = None  # Pin exact SHA — fetches ref but checks out this commit
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
                  timeout: int = 300, log_cb=None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )
    try:
        if log_cb:
            # Stream output line-by-line to the callback
            chunks = []
            async def read_stream():
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    text = line.decode(errors="replace")
                    chunks.append(text)
                    log_cb(text)
            await asyncio.wait_for(read_stream(), timeout=timeout)
            await proc.wait()
            return proc.returncode, "".join(chunks)
        else:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode, stdout.decode(errors="replace")
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "Command timed out"


async def ensure_repo(log_cb=None):
    if not (ARDUPILOT_DIR / "waf").exists():
        # Clean up incomplete clone if present
        if ARDUPILOT_DIR.exists():
            logger.info("Removing incomplete clone...")
            shutil.rmtree(ARDUPILOT_DIR, ignore_errors=True)
        logger.info("Cloning ardupilot repository...")
        if log_cb:
            log_cb("Cloning ardupilot repository (first run)...\n")
        rc, out = await run_cmd(
            ["git", "clone", "--progress", "--recurse-submodules",
             "https://github.com/ArduPilot/ardupilot.git", str(ARDUPILOT_DIR)],
            timeout=600, log_cb=log_cb,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to clone: {out}")
        logger.info("Clone complete")
        if log_cb:
            log_cb("Clone complete\n")

    # Ensure submodules are initialized in the main repo (golden copy for templates)
    if not (ARDUPILOT_DIR / "modules" / "mavlink" / ".git").exists():
        logger.info("Initializing submodules in base repo...")
        if log_cb:
            log_cb("Initializing submodules in base repo...\n")
        cpu_count = os.cpu_count() or 4
        rc, out = await run_cmd(
            ["git", "submodule", "update", "--init", "--recursive",
             "--depth", "1", f"--jobs={cpu_count}"],
            cwd=ARDUPILOT_DIR, timeout=600, log_cb=log_cb,
        )
        if rc != 0:
            logger.warning(f"Submodule init issue: {out}")

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

    fetch_cmd = ["git", "fetch", remote_name, "--prune", "--tags",
                 "--no-recurse-submodules"]
    if ref and len(ref) < 40:
        # Explicitly fetch the target branch to ensure we get latest commits
        fetch_cmd.append(f"+refs/heads/{ref}:refs/remotes/{remote_name}/{ref}")

    # Cross-process file lock to coordinate with custombuild-builder
    lock_path = ARDUPILOT_DIR / ".fetch.lock"
    loop = asyncio.get_event_loop()

    def _locked_fetch():
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                result = subprocess.run(
                    fetch_cmd, cwd=ARDUPILOT_DIR,
                    capture_output=True, timeout=300,
                )
                return result.returncode, (result.stdout + result.stderr).decode(errors="replace")
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    rc, out = await loop.run_in_executor(None, _locked_fetch)
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


async def get_or_create_template(commit: str, log_cb=None) -> Path:
    """
    Get a cached template worktree for a commit, or create one.
    Templates are git worktrees with submodules initialized — ready to copy.
    Must be called under per-commit lock from _template_locks.
    """
    # Double-check cache (another task with same commit may have finished first)
    async with _template_cache_guard:
        if commit in template_cache:
            entry = template_cache[commit]
            if entry["path"].exists():
                entry["last_used"] = time.time()
                logger.info(f"Template cache HIT for {commit[:12]}")
                return entry["path"]
            else:
                del template_cache[commit]

    logger.info(f"Template cache MISS for {commit[:12]}, creating...")
    if log_cb:
        log_cb(f"Creating source template for {commit[:12]}...\n")

    # Evict oldest if at capacity
    evict_entry = None
    async with _template_cache_guard:
        if len(template_cache) >= MAX_CACHED_TEMPLATES:
            oldest_key = min(template_cache, key=lambda k: template_cache[k]["last_used"])
            evict_entry = template_cache.pop(oldest_key)
            logger.info(f"Evicting template {oldest_key[:12]}")
    if evict_entry:
        await run_cmd(
            ["git", "worktree", "unlock", str(evict_entry["path"])],
            cwd=ARDUPILOT_DIR, timeout=10,
        )
        rc, _ = await run_cmd(
            ["git", "worktree", "remove", "--force", str(evict_entry["path"])],
            cwd=ARDUPILOT_DIR, timeout=60,
        )
        if rc != 0:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, shutil.rmtree, evict_entry["path"], True)
        await run_cmd(["git", "worktree", "prune"], cwd=ARDUPILOT_DIR)

    # Create template worktree
    tpl_path = TEMPLATES_DIR / f"tpl-{commit[:12]}"
    if tpl_path.exists():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, shutil.rmtree, tpl_path, True)

    if log_cb:
        log_cb(f"  Creating worktree at {tpl_path.name}...\n")
    rc, out = await run_cmd(
        ["git", "worktree", "add", "--detach", str(tpl_path), commit],
        cwd=ARDUPILOT_DIR, timeout=120, log_cb=log_cb,
    )
    if rc != 0:
        raise RuntimeError(f"Failed to create template worktree: {out}")

    # Fast submodule init: copy git module stores from main repo, then checkout.
    # This avoids all network fetches — purely local filesystem operations.
    # On btrfs (Docker vDisk), --reflink=auto makes the copy near-instant (COW).
    main_modules = ARDUPILOT_DIR / ".git" / "modules"
    if main_modules.exists():
        if log_cb:
            log_cb(f"  Copying submodule objects from base repo (local)...\n")
        # Worktree .git is a file pointing to the main repo's worktrees dir;
        # we need the actual git dir for this worktree
        tpl_gitdir = tpl_path / ".git"
        if tpl_gitdir.is_file():
            # Read the gitdir path from the worktree .git file
            gitdir_content = tpl_gitdir.read_text().strip()
            if gitdir_content.startswith("gitdir: "):
                actual_gitdir = Path(gitdir_content[8:])
                if not actual_gitdir.is_absolute():
                    actual_gitdir = (tpl_path / actual_gitdir).resolve()
            else:
                actual_gitdir = tpl_gitdir
        else:
            actual_gitdir = tpl_gitdir

        modules_dest = actual_gitdir / "modules"
        rc, out = await run_cmd(
            ["cp", "-a", "--reflink=auto", str(main_modules), str(modules_dest)],
            timeout=120, log_cb=log_cb,
        )
        if rc != 0:
            logger.warning(f"Failed to copy modules, falling back to remote fetch: {out}")

        if log_cb:
            log_cb(f"  Initializing submodule working trees...\n")
        rc, out = await run_cmd(
            ["git", "submodule", "update", "--init", "--recursive"],
            cwd=tpl_path, timeout=300, log_cb=log_cb,
        )
    else:
        # Fallback: no cached modules, fetch from remote
        cpu_count = os.cpu_count() or 4
        if log_cb:
            log_cb(f"  Submodule update from remote ({cpu_count} jobs)...\n")
        rc, out = await run_cmd(
            ["git", "submodule", "update", "--init", "--recursive",
             "--depth", "1", f"--jobs={cpu_count}"],
            cwd=tpl_path, timeout=300, log_cb=log_cb,
        )
    if rc != 0:
        logger.warning(f"Submodule update issue for template {commit[:12]}: {out}")

    # Lock the template so it's not accidentally pruned
    await run_cmd(
        ["git", "worktree", "lock", str(tpl_path)],
        cwd=ARDUPILOT_DIR, timeout=10,
    )

    async with _template_cache_guard:
        template_cache[commit] = {"path": tpl_path, "last_used": time.time()}
    logger.info(f"Template created for {commit[:12]} at {tpl_path}")
    return tpl_path


async def overlay_mount(lower: Path, dest: Path) -> Path:
    """
    Create an overlayfs mount over a read-only template.
    Instant regardless of template size — only modified files are written.
    Returns the mount point (dest).
    """
    upper = dest.parent / f".upper_{dest.name}"
    work = dest.parent / f".work_{dest.name}"
    upper.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    dest.mkdir(parents=True, exist_ok=True)

    rc, out = await run_cmd(
        ["sudo", "mount", "-t", "overlay", "overlay",
         "-o", f"lowerdir={lower},upperdir={upper},workdir={work}",
         str(dest)],
        timeout=10,
    )
    if rc != 0:
        # Fallback to cp if overlay not available
        logger.warning(f"Overlay mount failed ({out.strip()}), falling back to cp")
        shutil.rmtree(upper, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(dest, ignore_errors=True)
        rc, out = await run_cmd(
            ["cp", "-a", "--reflink=auto", str(lower), str(dest)],
            timeout=300,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to copy {lower} -> {dest}: {out}")
    return dest


async def create_test_copy(test_id: str, template_path: Path) -> Path:
    """Overlay mount from build template — instant, writes only diffs."""
    dest = WORKTREES_DIR / test_id
    return await overlay_mount(template_path, dest)


def cleanup_test_copy(test_id: str):
    """Unmount overlay (if mounted) and remove the test copy."""
    copy_path = WORKTREES_DIR / test_id
    upper = WORKTREES_DIR / f".upper_{test_id}"
    work = WORKTREES_DIR / f".work_{test_id}"

    # Unmount overlay first (ignore error if it was a plain copy)
    subprocess.run(["sudo", "umount", str(copy_path)],
                   capture_output=True, timeout=10)

    for p in (copy_path, upper, work):
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)


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
    Must be called under per-key lock from _build_key_locks.
    """
    key = build_cache_key(commit, vehicle, waf_configure_args, waf_build_args)

    # Double-check cache (another task with same key may have finished first)
    async with _build_cache_guard:
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
    evict_path = None
    async with _build_cache_guard:
        if len(build_cache) >= MAX_CACHED_BUILDS:
            oldest_key = min(build_cache, key=lambda k: build_cache[k]["last_used"])
            oldest = build_cache.pop(oldest_key)
            evict_path = oldest["path"]
            logger.info(f"Evicted build template {oldest_key[:8]}")
    if evict_path and evict_path.exists():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, shutil.rmtree, evict_path, True)

    # Copy source template to build template dir
    bld_path = BUILD_TEMPLATES_DIR / f"bld-{key}-{vehicle.lower()}"
    if bld_path.exists():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, shutil.rmtree, bld_path, True)

    if log_cb:
        log_cb(f"Copying source template to build dir...\n")
    rc, out = await run_cmd(
        ["cp", "-a", "--reflink=auto", str(source_template), str(bld_path)],
        timeout=300,
    )
    if rc != 0:
        raise RuntimeError(f"Failed to copy source template: {out}")

    # Configure
    configure_cmd = ["python3", "./waf", "configure", "--board", "sitl"]
    configure_cmd.extend(waf_configure_args)
    if log_cb:
        log_cb(f"=== Configure: {' '.join(configure_cmd)} ===\n")

    rc, out = await run_cmd(configure_cmd, cwd=bld_path, timeout=120,
                            log_cb=log_cb)
    if rc != 0:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, shutil.rmtree, bld_path, True)
        raise RuntimeError(f"Build configure failed: {out[-200:]}")

    # Build
    cpu_count = os.cpu_count() or 4
    build_cmd = ["python3", "./waf", vehicle.lower(), f"-j{cpu_count}"]
    build_cmd.extend(waf_build_args)
    if log_cb:
        log_cb(f"=== Build: {' '.join(build_cmd)} ===\n")

    rc, out = await run_cmd(build_cmd, cwd=bld_path, timeout=600,
                            log_cb=log_cb)
    if rc != 0:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, shutil.rmtree, bld_path, True)
        raise RuntimeError(f"Build failed: {out[-200:]}")

    async with _build_cache_guard:
        build_cache[key] = {"path": bld_path, "last_used": time.time()}
    if log_cb:
        log_cb(f"Build template cached: {bld_path}\n\n")
    logger.info(f"Build template created: {key[:8]} at {bld_path}")
    return bld_path


# --- Test runner ---

def _append_file(path: Path, content: str):
    with open(path, "a") as f:
        f.write(content)


async def flush_log(test_info: dict):
    """Append only new log content to disk (O(n) total instead of O(n^2))."""
    log_path = RESULTS_DIR / test_info["test_id"] / "test.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    current_log = test_info["log"]
    flushed = test_info.get("_log_flushed_len", 0)

    if len(current_log) > flushed:
        new_content = current_log[flushed:]
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _append_file, log_path, new_content)
        test_info["_log_flushed_len"] = len(current_log)

    # Metadata is small — full rewrite is fine
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, save_test_metadata, test_info)


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

    # 3. Symlink to web-visible buildlogs directory for /results/ page
    if BUILDLOGS_DIR.exists():
        web_dest = BUILDLOGS_DIR / f"autotest_{test_id}"
        try:
            if web_dest.is_symlink() or web_dest.exists():
                if web_dest.is_symlink():
                    web_dest.unlink()
                else:
                    shutil.rmtree(web_dest)
            web_dest.symlink_to(dest)
        except Exception as e:
            logger.warning(f"Failed to copy to buildlogs: {e}")

    logger.info(f"Artifacts collected for {test_id}: {[f.name for f in dest.iterdir()]}")


async def run_test_async(test_id: str, vehicle: str, test_target: str,
                         remote: str, ref: str,
                         waf_configure_args: list[str],
                         waf_build_args: list[str],
                         commit: str | None = None):
    test_info = tests[test_id]
    test_info["state"] = "UPDATING"

    wt_path = None
    # Each test gets its own buildlogs dir so concurrent tests don't clobber each other
    test_buildlogs = WORKTREES_DIR / f"buildlogs_{test_id}"
    test_buildlogs.mkdir(parents=True, exist_ok=True)
    test_env = {**os.environ, "BUILDLOGS": str(test_buildlogs)}

    pinned_commit = commit  # --commit flag from client (None if not pinned)

    try:
        test_info["log"] = f"=== Preparing source for {remote}/{ref}"
        if pinned_commit:
            test_info["log"] += f" (pinned: {pinned_commit[:12]})"
        test_info["log"] += " ===\n"
        await flush_log(test_info)

        def source_log(msg):
            test_info["log"] += msg

        await ensure_repo(log_cb=source_log)

        # Phase 1: Resolve commit — local checks are lock-free, only fetch serializes
        found, resolved = await commit_is_local(ref, remote)
        if not found or not resolved:
            async with _fetch_lock:
                # Re-check after acquiring lock (another test may have fetched)
                found, resolved = await commit_is_local(ref, remote)
                if not found or not resolved:
                    test_info["log"] += f"Ref not local, fetching {remote}...\n"
                    await flush_log(test_info)
                    fetch_out = await fetch_remote(remote, ref=ref)
                    test_info["log"] += fetch_out + "\n"
                    found, resolved = True, None  # fetched, now resolve below
                else:
                    test_info["log"] += f"Ref local ({resolved[:12]})\n"
        else:
            test_info["log"] += f"Ref local ({resolved[:12]})\n"

        if pinned_commit:
            commit = pinned_commit
            test_info["log"] += f"Using pinned commit: {commit}\n"
        elif resolved:
            commit = resolved
            test_info["log"] += f"Resolved to: {commit}\n"
        else:
            commit = await resolve_ref(remote, ref)
            test_info["log"] += f"Resolved to: {commit}\n"

        # Phase 2: Get or create template (per-commit lock)
        template_path = None
        async with _template_cache_guard:
            if commit in template_cache and template_cache[commit]["path"].exists():
                template_cache[commit]["last_used"] = time.time()
                template_path = template_cache[commit]["path"]
                test_info["log"] += f"Template cache HIT: {commit[:12]}\n"

        if template_path is None:
            # Get per-commit lock (independent commits build templates in parallel)
            async with _template_cache_guard:
                if commit not in _template_locks:
                    _template_locks[commit] = asyncio.Lock()
                commit_lock = _template_locks[commit]

            async with commit_lock:
                # Double-check after acquiring per-commit lock
                async with _template_cache_guard:
                    if commit in template_cache and template_cache[commit]["path"].exists():
                        template_cache[commit]["last_used"] = time.time()
                        template_path = template_cache[commit]["path"]
                        test_info["log"] += f"Template appeared while waiting (cache HIT)\n"
                if template_path is None:
                    template_path = await get_or_create_template(commit, log_cb=source_log)

        test_info["log"] += f"Template: {template_path}\n"
        await flush_log(test_info)

        # Get or create pre-built template (per-key lock: different builds run in parallel)
        test_info["state"] = "BUILDING"
        def build_log(msg):
            test_info["log"] += msg

        bld_key = build_cache_key(commit, vehicle, waf_configure_args, waf_build_args)
        build_tpl = None

        # Fast path: check cache under short guard
        async with _build_cache_guard:
            if bld_key in build_cache and build_cache[bld_key]["path"].exists():
                build_cache[bld_key]["last_used"] = time.time()
                build_tpl = build_cache[bld_key]["path"]
                build_log(f"Build cache HIT ({bld_key[:8]}): {vehicle} already built for {commit[:12]}\n")

        if build_tpl is None:
            # Get or create per-key lock (only same build config serializes)
            async with _build_cache_guard:
                if bld_key not in _build_key_locks:
                    _build_key_locks[bld_key] = asyncio.Lock()
                key_lock = _build_key_locks[bld_key]

            async with key_lock:
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
        await flush_log(test_info)
        wt_path = await create_test_copy(test_id, build_tpl)
        test_info["worktree"] = str(wt_path)
        test_info["log"] += f"Copy ready: {wt_path}\n\n"
        await flush_log(test_info)

        # Grab a SITL instance slot (each uses unique ports: base + instance*10)
        test_info["state"] = "QUEUED"
        test_info["log"] += f"\n=== Waiting for SITL instance ({sitl_instance_pool.qsize()}/{MAX_SITL_INSTANCES} free) ===\n"
        await flush_log(test_info)

        instance_num = await sitl_instance_pool.get()
        try:
            test_info["state"] = "TESTING"
            test_info["log"] += f"=== SITL instance {instance_num} (ports {5760 + instance_num*10}+) ===\n"
            test_info["log"] += f"=== Running: {test_target} ===\n"
            test_info["log"] += f"=== BUILDLOGS: {test_buildlogs} ===\n"
            test_info["log"] += "=" * 60 + "\n"
            await flush_log(test_info)

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
                    await flush_log(test_info)

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
        await flush_log(test_info)

        # Collect all artifacts then remove the copy — run in executor to avoid
        # blocking event loop on potentially large file I/O
        loop = asyncio.get_event_loop()
        if wt_path:
            await loop.run_in_executor(None, collect_artifacts, test_id, wt_path, test_buildlogs)
            await loop.run_in_executor(None, cleanup_test_copy, test_id)

        # Clean up isolated buildlogs dir
        if test_buildlogs.exists():
            await loop.run_in_executor(None, shutil.rmtree, test_buildlogs, True)


def test_summary(t: dict) -> dict:
    return {
        "test_id": t["test_id"],
        "batch_id": t.get("batch_id"),
        "vehicle": t["vehicle"],
        "test": t["test"],
        "remote": t["remote"],
        "ref": t["ref"],
        "commit": t.get("commit"),
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


@app.get("/autotest/api/metrics")
async def api_metrics():
    running_states = ("UPDATING", "BUILDING", "TESTING", "QUEUED")
    running = [t for t in tests.values() if t["state"] in running_states]
    pending = [t for t in tests.values() if t["state"] == "QUEUED"]
    mem = psutil.virtual_memory()
    load_1m = os.getloadavg()[0]
    return {
        "cpu_percent": psutil.cpu_percent(interval=0),
        "memory_percent": mem.percent,
        "memory_used_gb": round(mem.used / (1024**3), 1),
        "memory_total_gb": round(mem.total / (1024**3), 1),
        "running_tests": len(running),
        "pending_tests": len(pending),
        "load_avg_1m": round(load_1m, 1),
    }


@app.get("/autotest/api/tests")
async def list_tests(limit: int = 100, offset: int = 0, batch_id: str | None = None):
    from fastapi.responses import JSONResponse
    filtered = list(tests.values())
    if batch_id:
        filtered = [t for t in filtered if t.get("batch_id") == batch_id]
    sorted_tests = sorted(filtered, key=lambda t: t["created_at"], reverse=True)
    total = len(sorted_tests)
    page = sorted_tests[offset:offset + limit] if limit > 0 else sorted_tests
    data = [test_summary(t) for t in page]
    resp = JSONResponse(content=data)
    resp.headers["X-Total-Count"] = str(total)
    resp.headers["Access-Control-Expose-Headers"] = "X-Total-Count"
    return resp


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
        "commit": req.commit,
        "waf_configure_args": req.waf_configure_args,
        "waf_build_args": req.waf_build_args,
        "state": "PENDING",
        "created_at": time.time(),
        "finished_at": None,
        "log": "",
        "_log_flushed_len": 0,
        "task": None,
        "process": None,
        "worktree": None,
    }
    tests[test_id] = test_info

    task = asyncio.create_task(
        run_test_async(
            test_id, req.vehicle, req.test, req.remote, req.ref,
            req.waf_configure_args, req.waf_build_args,
            commit=req.commit,
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
    cancelled = sum(1 for t in batch_tests if t["state"] == "CANCELLED")
    complete = running == 0
    return {
        "batch_id": batch_id,
        "complete": complete,
        "total": len(batch_tests),
        "passed": passed,
        "failed": failed,
        "running": running,
        "cancelled": cancelled,
        "tests": batch_tests,
    }


@app.get("/autotest/api/batches/{batch_id}/wait")
async def wait_for_batch(batch_id: str, timeout: int = Query(default=600, ge=1, le=3600)):
    """
    Long-poll endpoint: blocks until all tests in the batch are done or timeout.
    Returns the full batch summary when complete. Ideal for AI agents and CI scripts.
    Poll interval: 2s. Returns HTTP 408 on timeout.
    """
    running_states = ("PENDING", "UPDATING", "BUILDING", "QUEUED", "TESTING")
    deadline = time.time() + timeout

    while time.time() < deadline:
        batch_tests = [
            t for t in tests.values()
            if t.get("batch_id") == batch_id
        ]
        if not batch_tests:
            raise HTTPException(404, f"Batch '{batch_id}' not found")

        still_running = sum(1 for t in batch_tests if t["state"] in running_states)
        if still_running == 0:
            # All done — return full summary
            summaries = sorted(
                [test_summary(t) for t in batch_tests], key=lambda t: t["test"]
            )
            passed = sum(1 for t in summaries if t["state"] == "SUCCESS")
            failed = sum(1 for t in summaries if t["state"] in ("FAILURE", "ERROR"))
            cancelled = sum(1 for t in summaries if t["state"] == "CANCELLED")
            return {
                "batch_id": batch_id,
                "complete": True,
                "total": len(summaries),
                "passed": passed,
                "failed": failed,
                "cancelled": cancelled,
                "tests": summaries,
            }

        await asyncio.sleep(2)

    raise HTTPException(408, f"Timeout waiting for batch '{batch_id}' after {timeout}s")


@app.get("/autotest/api/batches/{batch_id}/summary")
async def batch_summary(batch_id: str):
    """
    Concise batch summary for AI agents: one-line-per-test with pass/fail and failure reason.
    Designed to fit in a single LLM context window.
    """
    batch_tests = [
        t for t in tests.values()
        if t.get("batch_id") == batch_id
    ]
    if not batch_tests:
        raise HTTPException(404, f"Batch '{batch_id}' not found")

    batch_tests.sort(key=lambda t: t["test"])
    running_states = ("PENDING", "UPDATING", "BUILDING", "QUEUED", "TESTING")
    still_running = sum(1 for t in batch_tests if t["state"] in running_states)

    lines = []
    failures = []
    for t in batch_tests:
        test_name = t["test"].split(".")[-1] if "." in t["test"] else t["test"]
        state = t["state"]
        if state == "SUCCESS":
            lines.append(f"  PASS  {test_name}")
        elif state in ("FAILURE", "ERROR"):
            # Extract failure reason from last few log lines
            reason = ""
            log_lines = t.get("log", "").strip().splitlines()
            for line in reversed(log_lines[-20:]):
                if "NotAchievedException" in line or "Exception" in line:
                    reason = line.strip()
                    break
                if "FAILED" in line and "tests:" in line:
                    reason = line.strip()
                    break
            lines.append(f"  FAIL  {test_name}")
            if reason:
                failures.append({"test": test_name, "test_id": t["test_id"], "reason": reason})
        elif state == "CANCELLED":
            lines.append(f"  SKIP  {test_name}")
        else:
            lines.append(f"  .... {test_name} ({state})")

    passed = sum(1 for t in batch_tests if t["state"] == "SUCCESS")
    failed = sum(1 for t in batch_tests if t["state"] in ("FAILURE", "ERROR"))

    return PlainTextResponse(
        f"Batch {batch_id} — {passed} passed, {failed} failed, "
        f"{still_running} running, {len(batch_tests)} total\n"
        f"Remote: {batch_tests[0]['remote']}/{batch_tests[0]['ref']}\n\n"
        + "\n".join(lines)
        + ("\n\nFailure details:\n" + "\n".join(
            f"  {f['test']} ({f['test_id']}): {f['reason']}" for f in failures
        ) if failures else "")
        + "\n"
    )


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
