"""
Admin service for managing CustomBuild remotes.json and system status.
Reads/writes the shared remotes.json and triggers hot-reload on the CustomBuild app.
"""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="ArduPilot Remotes Admin", docs_url="/admin/api/docs", redoc_url=None)

BASE_DIR = os.environ.get("CBS_BASEDIR", "/base")
REMOTES_JSON_PATH = os.path.join(BASE_DIR, "configs", "remotes.json")
BUILDLOGS_DIR = os.environ.get("BUILDLOGS_DIR", "/buildlogs")
CBS_APP_URL = os.environ.get("CBS_APP_URL", "http://custombuild-app:8080")
CBS_RELOAD_TOKEN = os.environ.get("CBS_REMOTES_RELOAD_TOKEN", "")

templates = Jinja2Templates(directory="/app/templates")
app.mount("/admin/static", StaticFiles(directory="/app/static"), name="static")


# --- API Capabilities (machine-readable discovery for AI agents) ---

CAPABILITIES = {
    "version": "1.0",
    "openapi_docs": {
        "builds": "/api/docs",
        "tests": "/autotest/api/docs",
        "admin": "/admin/api/docs",
    },
    "categories": [
        {
            "id": "builds",
            "name": "Firmware Builds",
            "description": "Build custom ArduPilot firmware for specific vehicles, boards, and feature sets",
            "operations": [
                {
                    "id": "list_vehicles",
                    "name": "List Vehicles",
                    "description": "Get all available vehicle types (Plane, Copter, Rover, Sub, etc.)",
                    "method": "GET",
                    "path": "/api/v1/vehicles",
                    "parameters": [],
                },
                {
                    "id": "get_vehicle",
                    "name": "Get Vehicle",
                    "description": "Get details for a specific vehicle type",
                    "method": "GET",
                    "path": "/api/v1/vehicles/{vehicle_id}",
                    "parameters": [
                        {"name": "vehicle_id", "in": "path", "type": "string", "required": True, "description": "Vehicle identifier (e.g. 'plane', 'copter')"},
                    ],
                },
                {
                    "id": "list_versions",
                    "name": "List Versions",
                    "description": "List available firmware versions/branches for a vehicle",
                    "method": "GET",
                    "path": "/api/v1/vehicles/{vehicle_id}/versions",
                    "parameters": [
                        {"name": "vehicle_id", "in": "path", "type": "string", "required": True, "description": "Vehicle identifier"},
                        {"name": "type", "in": "query", "type": "string", "required": False, "description": "Filter by version type", "enum": ["beta", "stable", "latest", "tag"]},
                    ],
                },
                {
                    "id": "get_version",
                    "name": "Get Version",
                    "description": "Get details for a specific version",
                    "method": "GET",
                    "path": "/api/v1/vehicles/{vehicle_id}/versions/{version_id}",
                    "parameters": [
                        {"name": "vehicle_id", "in": "path", "type": "string", "required": True, "description": "Vehicle identifier"},
                        {"name": "version_id", "in": "path", "type": "string", "required": True, "description": "Version identifier"},
                    ],
                },
                {
                    "id": "list_boards",
                    "name": "List Boards",
                    "description": "List available hardware boards for a vehicle/version combination",
                    "method": "GET",
                    "path": "/api/v1/vehicles/{vehicle_id}/versions/{version_id}/boards",
                    "parameters": [
                        {"name": "vehicle_id", "in": "path", "type": "string", "required": True, "description": "Vehicle identifier"},
                        {"name": "version_id", "in": "path", "type": "string", "required": True, "description": "Version identifier"},
                    ],
                },
                {
                    "id": "get_board",
                    "name": "Get Board",
                    "description": "Get details for a specific board",
                    "method": "GET",
                    "path": "/api/v1/vehicles/{vehicle_id}/versions/{version_id}/boards/{board_id}",
                    "parameters": [
                        {"name": "vehicle_id", "in": "path", "type": "string", "required": True, "description": "Vehicle identifier"},
                        {"name": "version_id", "in": "path", "type": "string", "required": True, "description": "Version identifier"},
                        {"name": "board_id", "in": "path", "type": "string", "required": True, "description": "Board identifier (e.g. 'CubeOrangePlus')"},
                    ],
                },
                {
                    "id": "list_features",
                    "name": "List Features",
                    "description": "List available build features for a vehicle/version/board, including defaults",
                    "method": "GET",
                    "path": "/api/v1/vehicles/{vehicle_id}/versions/{version_id}/boards/{board_id}/features",
                    "parameters": [
                        {"name": "vehicle_id", "in": "path", "type": "string", "required": True, "description": "Vehicle identifier"},
                        {"name": "version_id", "in": "path", "type": "string", "required": True, "description": "Version identifier"},
                        {"name": "board_id", "in": "path", "type": "string", "required": True, "description": "Board identifier"},
                        {"name": "category_id", "in": "query", "type": "string", "required": False, "description": "Filter features by category"},
                    ],
                },
                {
                    "id": "get_feature",
                    "name": "Get Feature",
                    "description": "Get details for a specific build feature",
                    "method": "GET",
                    "path": "/api/v1/vehicles/{vehicle_id}/versions/{version_id}/boards/{board_id}/features/{feature_id}",
                    "parameters": [
                        {"name": "vehicle_id", "in": "path", "type": "string", "required": True, "description": "Vehicle identifier"},
                        {"name": "version_id", "in": "path", "type": "string", "required": True, "description": "Version identifier"},
                        {"name": "board_id", "in": "path", "type": "string", "required": True, "description": "Board identifier"},
                        {"name": "feature_id", "in": "path", "type": "string", "required": True, "description": "Feature identifier"},
                    ],
                },
                {
                    "id": "submit_build",
                    "name": "Submit Build",
                    "description": "Submit a new firmware build job. Rate limited to 10/hour.",
                    "method": "POST",
                    "path": "/api/v1/builds",
                    "parameters": [
                        {"name": "vehicle_id", "in": "body", "type": "string", "required": True, "description": "Vehicle ID to build for"},
                        {"name": "board_id", "in": "body", "type": "string", "required": True, "description": "Board ID to build for"},
                        {"name": "version_id", "in": "body", "type": "string", "required": True, "description": "Version ID for build source code"},
                        {"name": "selected_features", "in": "body", "type": "array[string]", "required": False, "description": "Feature IDs to enable (empty array for no optional features)", "default": []},
                    ],
                    "example_request": {
                        "vehicle_id": "plane",
                        "board_id": "CubeOrangePlus",
                        "version_id": "ardupilot-refs-heads-master-e2f0bfbd",
                        "selected_features": ["HAL_ADSB_ENABLED"],
                    },
                },
                {
                    "id": "list_builds",
                    "name": "List Builds",
                    "description": "List recent builds with optional filtering",
                    "method": "GET",
                    "path": "/api/v1/builds",
                    "parameters": [
                        {"name": "vehicle_id", "in": "query", "type": "string", "required": False, "description": "Filter by vehicle ID"},
                        {"name": "board_id", "in": "query", "type": "string", "required": False, "description": "Filter by board ID"},
                        {"name": "state", "in": "query", "type": "string", "required": False, "description": "Filter by build state", "enum": ["PENDING", "RUNNING", "SUCCESS", "FAILURE", "CANCELLED", "TIMED_OUT"]},
                        {"name": "limit", "in": "query", "type": "integer", "required": False, "description": "Max results (1-100)", "default": 20},
                        {"name": "offset", "in": "query", "type": "integer", "required": False, "description": "Pagination offset", "default": 0},
                    ],
                },
                {
                    "id": "get_build",
                    "name": "Get Build",
                    "description": "Get build status, progress, and details",
                    "method": "GET",
                    "path": "/api/v1/builds/{build_id}",
                    "parameters": [
                        {"name": "build_id", "in": "path", "type": "string", "required": True, "description": "Build identifier"},
                    ],
                },
                {
                    "id": "get_build_logs",
                    "name": "Get Build Logs",
                    "description": "Get build output logs as plain text",
                    "method": "GET",
                    "path": "/api/v1/builds/{build_id}/logs",
                    "parameters": [
                        {"name": "build_id", "in": "path", "type": "string", "required": True, "description": "Build identifier"},
                        {"name": "tail", "in": "query", "type": "integer", "required": False, "description": "Return only the last N lines"},
                    ],
                },
                {
                    "id": "download_artifact",
                    "name": "Download Artifact",
                    "description": "Download the built firmware archive (.tar.gz). Only available after build succeeds.",
                    "method": "GET",
                    "path": "/api/v1/builds/{build_id}/artifact",
                    "parameters": [
                        {"name": "build_id", "in": "path", "type": "string", "required": True, "description": "Build identifier"},
                    ],
                },
            ],
        },
        {
            "id": "tests",
            "name": "SITL Autotest",
            "description": "Run ArduPilot SITL (Software In The Loop) tests against any branch or commit",
            "operations": [
                {
                    "id": "autotest_status",
                    "name": "Autotest Status",
                    "description": "Get autotest service status (busy/idle, running count, repo state)",
                    "method": "GET",
                    "path": "/autotest/api/status",
                    "parameters": [],
                },
                {
                    "id": "list_test_vehicles",
                    "name": "List Test Vehicles",
                    "description": "List vehicles available for SITL testing",
                    "method": "GET",
                    "path": "/autotest/api/vehicles",
                    "parameters": [],
                },
                {
                    "id": "list_subtests",
                    "name": "List Subtests",
                    "description": "List available subtests for a vehicle",
                    "method": "GET",
                    "path": "/autotest/api/subtests",
                    "parameters": [
                        {"name": "vehicle", "in": "query", "type": "string", "required": False, "description": "Vehicle name", "default": "Plane"},
                    ],
                },
                {
                    "id": "list_test_suites",
                    "name": "List Test Suites",
                    "description": "List top-level test suite names (test.Plane, test.Copter, etc.)",
                    "method": "GET",
                    "path": "/autotest/api/test-suites",
                    "parameters": [],
                },
                {
                    "id": "submit_test",
                    "name": "Submit Test",
                    "description": "Submit a SITL test run for a vehicle and test target",
                    "method": "POST",
                    "path": "/autotest/api/tests",
                    "parameters": [
                        {"name": "vehicle", "in": "body", "type": "string", "required": False, "description": "Vehicle to test", "default": "Plane"},
                        {"name": "test", "in": "body", "type": "string", "required": False, "description": "Test target (e.g. 'test.PlaneTests1b' or 'test.PlaneTests1b.TestA')", "default": "test.PlaneTests1b"},
                        {"name": "remote", "in": "body", "type": "string", "required": False, "description": "Git remote name", "default": "origin"},
                        {"name": "ref", "in": "body", "type": "string", "required": False, "description": "Git ref to test (branch, tag, or SHA)", "default": "master"},
                        {"name": "waf_configure_args", "in": "body", "type": "array[string]", "required": False, "description": "Extra waf configure flags", "default": []},
                        {"name": "waf_build_args", "in": "body", "type": "array[string]", "required": False, "description": "Extra waf build flags", "default": []},
                    ],
                    "example_request": {
                        "vehicle": "Plane",
                        "test": "test.PlaneTests1b",
                        "remote": "jack7169",
                        "ref": "feature/extpos-kalman-fusion",
                        "waf_configure_args": [],
                        "waf_build_args": [],
                    },
                },
                {
                    "id": "list_tests",
                    "name": "List Tests",
                    "description": "List recent test runs",
                    "method": "GET",
                    "path": "/autotest/api/tests",
                    "parameters": [
                        {"name": "limit", "in": "query", "type": "integer", "required": False, "description": "Max results", "default": 100},
                    ],
                },
                {
                    "id": "get_test",
                    "name": "Get Test",
                    "description": "Get test status and details",
                    "method": "GET",
                    "path": "/autotest/api/tests/{test_id}",
                    "parameters": [
                        {"name": "test_id", "in": "path", "type": "string", "required": True, "description": "Test identifier"},
                    ],
                },
                {
                    "id": "get_test_logs",
                    "name": "Get Test Logs",
                    "description": "Get test output logs as plain text",
                    "method": "GET",
                    "path": "/autotest/api/tests/{test_id}/logs",
                    "parameters": [
                        {"name": "test_id", "in": "path", "type": "string", "required": True, "description": "Test identifier"},
                        {"name": "tail", "in": "query", "type": "integer", "required": False, "description": "Return only the last N lines"},
                    ],
                },
                {
                    "id": "cancel_test",
                    "name": "Cancel Test",
                    "description": "Cancel a running or queued test",
                    "method": "POST",
                    "path": "/autotest/api/tests/{test_id}/cancel",
                    "parameters": [
                        {"name": "test_id", "in": "path", "type": "string", "required": True, "description": "Test identifier"},
                    ],
                },
            ],
        },
        {
            "id": "git",
            "name": "Git Management",
            "description": "Manage git remotes, branches, and tags in the autotest ArduPilot repository",
            "operations": [
                {
                    "id": "git_update",
                    "name": "Git Update",
                    "description": "Fetch from a remote and checkout a ref. Optionally add/update the remote URL.",
                    "method": "POST",
                    "path": "/autotest/api/git/update",
                    "parameters": [
                        {"name": "remote_name", "in": "body", "type": "string", "required": False, "description": "Remote name", "default": "origin"},
                        {"name": "remote_url", "in": "body", "type": "string", "required": False, "description": "Remote URL (adds or updates the remote if provided)"},
                        {"name": "ref", "in": "body", "type": "string", "required": False, "description": "Branch, tag, or SHA to checkout", "default": "master"},
                    ],
                    "example_request": {
                        "remote_name": "jack7169",
                        "ref": "master",
                    },
                },
                {
                    "id": "list_git_remotes",
                    "name": "List Git Remotes",
                    "description": "List configured git remotes and their URLs",
                    "method": "GET",
                    "path": "/autotest/api/git/remotes",
                    "parameters": [],
                },
                {
                    "id": "list_git_branches",
                    "name": "List Git Branches",
                    "description": "List branches available on a remote",
                    "method": "GET",
                    "path": "/autotest/api/git/branches",
                    "parameters": [
                        {"name": "remote", "in": "query", "type": "string", "required": False, "description": "Remote name to list branches for", "default": "origin"},
                    ],
                },
                {
                    "id": "list_git_tags",
                    "name": "List Git Tags",
                    "description": "List git tags (sorted by date, most recent first, max 200)",
                    "method": "GET",
                    "path": "/autotest/api/git/tags",
                    "parameters": [
                        {"name": "remote", "in": "query", "type": "string", "required": False, "description": "Fetch tags from this remote before listing"},
                    ],
                },
                {
                    "id": "add_git_remote",
                    "name": "Add Git Remote",
                    "description": "Add a new git remote and fetch its refs",
                    "method": "POST",
                    "path": "/autotest/api/git/add-remote",
                    "parameters": [
                        {"name": "name", "in": "body", "type": "string", "required": True, "description": "Remote name"},
                        {"name": "url", "in": "body", "type": "string", "required": True, "description": "Remote URL"},
                    ],
                    "example_request": {
                        "name": "jack7169",
                        "url": "https://github.com/jack7169/ardupilot-jack.git",
                    },
                },
            ],
        },
        {
            "id": "admin",
            "name": "Admin & Remotes",
            "description": "Manage build remotes configuration and system status",
            "operations": [
                {
                    "id": "list_remotes",
                    "name": "List Remotes",
                    "description": "List all configured build remotes with their vehicles and releases",
                    "method": "GET",
                    "path": "/admin/api/remotes",
                    "parameters": [],
                },
                {
                    "id": "add_remote",
                    "name": "Add Remote",
                    "description": "Add a new build remote configuration",
                    "method": "POST",
                    "path": "/admin/api/remotes",
                    "parameters": [
                        {"name": "name", "in": "body", "type": "string", "required": True, "description": "Remote name"},
                        {"name": "url", "in": "body", "type": "string", "required": True, "description": "Git repository URL"},
                        {"name": "vehicles", "in": "body", "type": "array[object]", "required": True, "description": "Vehicle configurations with releases"},
                    ],
                    "example_request": {
                        "name": "jack7169",
                        "url": "https://github.com/jack7169/ardupilot-jack.git",
                        "vehicles": [{"name": "Plane", "releases": [{"release_type": "stable", "version_number": "4.5.0", "commit_reference": "v4.5.0"}]}],
                    },
                },
                {
                    "id": "update_remote",
                    "name": "Update Remote",
                    "description": "Update an existing build remote configuration",
                    "method": "PUT",
                    "path": "/admin/api/remotes/{name}",
                    "parameters": [
                        {"name": "name", "in": "path", "type": "string", "required": True, "description": "Remote name to update"},
                        {"name": "name", "in": "body", "type": "string", "required": True, "description": "New remote name"},
                        {"name": "url", "in": "body", "type": "string", "required": True, "description": "Git repository URL"},
                        {"name": "vehicles", "in": "body", "type": "array[object]", "required": True, "description": "Vehicle configurations"},
                    ],
                },
                {
                    "id": "delete_remote",
                    "name": "Delete Remote",
                    "description": "Delete a build remote configuration",
                    "method": "DELETE",
                    "path": "/admin/api/remotes/{name}",
                    "parameters": [
                        {"name": "name", "in": "path", "type": "string", "required": True, "description": "Remote name to delete"},
                    ],
                },
                {
                    "id": "add_vehicle",
                    "name": "Add Vehicle to Remote",
                    "description": "Add a vehicle configuration to a remote",
                    "method": "POST",
                    "path": "/admin/api/remotes/{name}/vehicles",
                    "parameters": [
                        {"name": "name", "in": "path", "type": "string", "required": True, "description": "Remote name"},
                        {"name": "name", "in": "body", "type": "string", "required": True, "description": "Vehicle name (e.g. 'Plane')"},
                        {"name": "releases", "in": "body", "type": "array[object]", "required": True, "description": "Release configurations"},
                    ],
                },
                {
                    "id": "delete_vehicle",
                    "name": "Delete Vehicle from Remote",
                    "description": "Remove a vehicle configuration from a remote",
                    "method": "DELETE",
                    "path": "/admin/api/remotes/{remote_name}/vehicles/{vehicle_name}",
                    "parameters": [
                        {"name": "remote_name", "in": "path", "type": "string", "required": True, "description": "Remote name"},
                        {"name": "vehicle_name", "in": "path", "type": "string", "required": True, "description": "Vehicle name to remove"},
                    ],
                },
                {
                    "id": "add_release",
                    "name": "Add Release to Vehicle",
                    "description": "Add a release configuration to a vehicle on a remote",
                    "method": "POST",
                    "path": "/admin/api/remotes/{remote_name}/vehicles/{vehicle_name}/releases",
                    "parameters": [
                        {"name": "remote_name", "in": "path", "type": "string", "required": True, "description": "Remote name"},
                        {"name": "vehicle_name", "in": "path", "type": "string", "required": True, "description": "Vehicle name"},
                        {"name": "release_type", "in": "body", "type": "string", "required": True, "description": "Release type (e.g. 'stable', 'beta')"},
                        {"name": "version_number", "in": "body", "type": "string", "required": True, "description": "Version number (e.g. '4.5.0')"},
                        {"name": "commit_reference", "in": "body", "type": "string", "required": True, "description": "Git commit reference (tag, branch, SHA)"},
                        {"name": "ap_build_artifacts_url", "in": "body", "type": "string", "required": False, "description": "URL for pre-built artifacts"},
                    ],
                },
                {
                    "id": "delete_release",
                    "name": "Delete Release",
                    "description": "Remove a release from a vehicle on a remote by index",
                    "method": "DELETE",
                    "path": "/admin/api/remotes/{remote_name}/vehicles/{vehicle_name}/releases/{release_idx}",
                    "parameters": [
                        {"name": "remote_name", "in": "path", "type": "string", "required": True, "description": "Remote name"},
                        {"name": "vehicle_name", "in": "path", "type": "string", "required": True, "description": "Vehicle name"},
                        {"name": "release_idx", "in": "path", "type": "integer", "required": True, "description": "Release index (0-based)"},
                    ],
                },
                {
                    "id": "force_refresh",
                    "name": "Force Refresh",
                    "description": "Trigger CustomBuild to reload remotes configuration",
                    "method": "POST",
                    "path": "/admin/api/refresh",
                    "parameters": [],
                },
                {
                    "id": "system_status",
                    "name": "System Status",
                    "description": "Get health status of all services (custombuild, builder, redis, autotest, caddy)",
                    "method": "GET",
                    "path": "/status/api",
                    "parameters": [],
                },
            ],
        },
    ],
}


@app.get("/api/capabilities")
async def capabilities():
    """Machine-readable API discovery for AI agents. Returns all available operations."""
    return CAPABILITIES


# --- Models ---

class ReleaseIn(BaseModel):
    release_type: str
    version_number: str
    commit_reference: str
    ap_build_artifacts_url: str | None = None


class VehicleIn(BaseModel):
    name: str
    releases: list[ReleaseIn]


class RemoteIn(BaseModel):
    name: str
    url: str
    vehicles: list[VehicleIn]


# --- Helpers ---

def read_remotes() -> list[dict]:
    try:
        with open(REMOTES_JSON_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def write_remotes(remotes: list[dict]):
    os.makedirs(os.path.dirname(REMOTES_JSON_PATH), exist_ok=True)
    with open(REMOTES_JSON_PATH, "w") as f:
        json.dump(remotes, f, indent=2)


async def trigger_refresh():
    """Call CustomBuild's refresh_remotes endpoint if token is configured."""
    if not CBS_RELOAD_TOKEN:
        logger.info("No reload token configured, skipping refresh trigger")
        return
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{CBS_APP_URL}/api/v1/admin/refresh_remotes",
                headers={"Authorization": f"Bearer {CBS_RELOAD_TOKEN}"},
                timeout=30,
            )
            logger.info(f"Refresh response: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.warning(f"Failed to trigger refresh: {e}")


# --- UI ---

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    remotes = read_remotes()
    return templates.TemplateResponse("admin.html", {"request": request, "remotes": remotes})


# --- API ---

@app.get("/admin/api/remotes")
async def list_remotes():
    return read_remotes()


@app.post("/admin/api/remotes")
async def add_remote(remote: RemoteIn):
    remotes = read_remotes()
    # Check for duplicate name
    if any(r["name"] == remote.name for r in remotes):
        raise HTTPException(400, f"Remote '{remote.name}' already exists")
    remotes.append(remote.model_dump(exclude_none=True))
    write_remotes(remotes)
    await trigger_refresh()
    return {"status": "ok", "remotes": remotes}


@app.put("/admin/api/remotes/{name}")
async def update_remote(name: str, remote: RemoteIn):
    remotes = read_remotes()
    idx = next((i for i, r in enumerate(remotes) if r["name"] == name), None)
    if idx is None:
        raise HTTPException(404, f"Remote '{name}' not found")
    remotes[idx] = remote.model_dump(exclude_none=True)
    write_remotes(remotes)
    await trigger_refresh()
    return {"status": "ok", "remotes": remotes}


@app.delete("/admin/api/remotes/{name}")
async def delete_remote(name: str):
    remotes = read_remotes()
    remotes = [r for r in remotes if r["name"] != name]
    write_remotes(remotes)
    await trigger_refresh()
    return {"status": "ok", "remotes": remotes}


@app.post("/admin/api/remotes/{name}/vehicles")
async def add_vehicle(name: str, vehicle: VehicleIn):
    remotes = read_remotes()
    remote = next((r for r in remotes if r["name"] == name), None)
    if not remote:
        raise HTTPException(404, f"Remote '{name}' not found")
    if any(v["name"] == vehicle.name for v in remote.get("vehicles", [])):
        raise HTTPException(400, f"Vehicle '{vehicle.name}' already exists in remote '{name}'")
    remote.setdefault("vehicles", []).append(vehicle.model_dump(exclude_none=True))
    write_remotes(remotes)
    await trigger_refresh()
    return {"status": "ok", "remotes": remotes}


@app.delete("/admin/api/remotes/{remote_name}/vehicles/{vehicle_name}")
async def delete_vehicle(remote_name: str, vehicle_name: str):
    remotes = read_remotes()
    remote = next((r for r in remotes if r["name"] == remote_name), None)
    if not remote:
        raise HTTPException(404, f"Remote '{remote_name}' not found")
    remote["vehicles"] = [v for v in remote.get("vehicles", []) if v["name"] != vehicle_name]
    write_remotes(remotes)
    await trigger_refresh()
    return {"status": "ok", "remotes": remotes}


@app.post("/admin/api/remotes/{remote_name}/vehicles/{vehicle_name}/releases")
async def add_release(remote_name: str, vehicle_name: str, release: ReleaseIn):
    remotes = read_remotes()
    remote = next((r for r in remotes if r["name"] == remote_name), None)
    if not remote:
        raise HTTPException(404, f"Remote '{remote_name}' not found")
    vehicle = next((v for v in remote.get("vehicles", []) if v["name"] == vehicle_name), None)
    if not vehicle:
        raise HTTPException(404, f"Vehicle '{vehicle_name}' not found")
    vehicle.setdefault("releases", []).append(release.model_dump(exclude_none=True))
    write_remotes(remotes)
    await trigger_refresh()
    return {"status": "ok", "remotes": remotes}


@app.put("/admin/api/remotes/{remote_name}/vehicles/{vehicle_name}/releases/{release_idx}")
async def update_release(remote_name: str, vehicle_name: str, release_idx: int, release: ReleaseIn):
    remotes = read_remotes()
    remote = next((r for r in remotes if r["name"] == remote_name), None)
    if not remote:
        raise HTTPException(404, f"Remote '{remote_name}' not found")
    vehicle = next((v for v in remote.get("vehicles", []) if v["name"] == vehicle_name), None)
    if not vehicle:
        raise HTTPException(404, f"Vehicle '{vehicle_name}' not found")
    releases = vehicle.get("releases", [])
    if release_idx < 0 or release_idx >= len(releases):
        raise HTTPException(400, "Invalid release index")
    releases[release_idx] = release.model_dump(exclude_none=True)
    write_remotes(remotes)
    await trigger_refresh()
    return {"status": "ok", "remotes": remotes}


@app.delete("/admin/api/remotes/{remote_name}/vehicles/{vehicle_name}/releases/{release_idx}")
async def delete_release(remote_name: str, vehicle_name: str, release_idx: int):
    remotes = read_remotes()
    remote = next((r for r in remotes if r["name"] == remote_name), None)
    if not remote:
        raise HTTPException(404, f"Remote '{remote_name}' not found")
    vehicle = next((v for v in remote.get("vehicles", []) if v["name"] == vehicle_name), None)
    if not vehicle:
        raise HTTPException(404, f"Vehicle '{vehicle_name}' not found")
    releases = vehicle.get("releases", [])
    if release_idx < 0 or release_idx >= len(releases):
        raise HTTPException(400, "Invalid release index")
    releases.pop(release_idx)
    write_remotes(remotes)
    await trigger_refresh()
    return {"status": "ok", "remotes": remotes}


@app.post("/admin/api/refresh")
async def force_refresh():
    await trigger_refresh()
    return {"status": "ok"}


@app.get("/admin/api/validate")
async def validate_remotes():
    """Check each remote/release ref is resolvable via git ls-remote."""
    import asyncio
    remotes = read_remotes()
    results = {}

    async def check_remote(remote):
        name = remote["name"]
        url = remote["url"]
        remote_result = {"url_ok": False, "vehicles": {}}

        # Check remote URL is reachable
        proc = await asyncio.create_subprocess_exec(
            "git", "ls-remote", "--heads", "--tags", url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            remote_result["error"] = "Timeout reaching remote"
            results[name] = remote_result
            return

        if proc.returncode != 0:
            remote_result["error"] = stderr.decode(errors="replace").strip()
            results[name] = remote_result
            return

        remote_result["url_ok"] = True
        available_refs = stdout.decode(errors="replace")

        for vehicle in remote.get("vehicles", []):
            vehicle_releases = []
            for idx, rel in enumerate(vehicle.get("releases", [])):
                ref = rel.get("commit_reference", "")
                # Check if ref appears in ls-remote output or looks like a SHA
                found = ref in available_refs or (len(ref) >= 40 and not ref.startswith("refs/"))
                vehicle_releases.append({
                    "index": idx,
                    "commit_reference": ref,
                    "valid": found,
                })
            remote_result["vehicles"][vehicle["name"]] = vehicle_releases

        results[name] = remote_result

    await asyncio.gather(*[check_remote(r) for r in remotes])
    return results


# --- Results Page ---

def extract_body_content(html: str) -> str:
    """Extract content between <body> tags and clean up external links."""
    match = re.search(r'<body[^>]*>(.*)</body>', html, re.DOTALL)
    if match:
        content = match.group(1)
    else:
        content = html
    # Remove links to external ardupilot sites
    content = re.sub(
        r'<a[^>]*href="https?://[^"]*ardupilot\.org[^"]*"[^>]*>.*?</a>',
        '', content, flags=re.DOTALL
    )
    return content


@app.get("/results", response_class=HTMLResponse)
@app.get("/results/", response_class=HTMLResponse)
@app.get("/results/{path:path}", response_class=HTMLResponse)
async def results_page(request: Request, path: str = ""):
    # Serve the index.html with our navbar wrapper
    if path == "" or path.endswith("/"):
        # Look for index.html in the requested directory
        subdir = Path(BUILDLOGS_DIR) / path
        index_file = subdir / "index.html"
        if index_file.is_file():
            raw_html = index_file.read_text(errors="replace")
            body_content = extract_body_content(raw_html)
            # Extract any inline CSS from the original <head>
            style_matches = re.findall(
                r'<link[^>]*href="([^"]*\.css)"[^>]*/?>',
                raw_html
            )
            css_links = [
                f"/results/_static/{path}{s}" if not s.startswith("http") else s
                for s in style_matches
            ]
            return templates.TemplateResponse("results.html", {
                "request": request,
                "body_content": body_content,
                "css_links": css_links,
                "subpath": path,
            })
        # No index.html — show directory listing
        if subdir.is_dir():
            entries = sorted(subdir.iterdir(), key=lambda p: (not p.is_dir(), p.name))
            items = []
            for entry in entries:
                name = entry.name + ("/" if entry.is_dir() else "")
                items.append(name)
            return templates.TemplateResponse("results.html", {
                "request": request,
                "body_content": None,
                "css_links": [],
                "subpath": path,
                "dir_listing": items,
            })
    raise HTTPException(404, "Not found")


# --- Status Page ---

SERVICES = [
    {"name": "Custom Firmware Builder", "description": "Web UI and API for building custom firmware", "check": "http", "url": "http://custombuild-app:8080/api/v1/vehicles"},
    {"name": "Build Worker", "description": "Processes firmware build jobs from the queue", "check": "dns", "host": "custombuild-builder"},
    {"name": "Redis", "description": "Message queue and job broker", "check": "tcp", "host": "redis", "port": 6379},
    {"name": "Admin Service", "description": "Remotes management and status dashboard", "check": "self"},
    {"name": "Autotest Runner", "description": "SITL test execution and git management", "check": "http", "url": "http://autotest:8091/autotest/api/status"},
    {"name": "Reverse Proxy", "description": "Caddy - routes traffic to all services", "check": "http", "url": "http://caddy:8000/"},
]


async def check_service(client: httpx.AsyncClient, svc: dict) -> dict:
    result = {"name": svc["name"], "description": svc["description"], "status": "operational", "response_ms": None}
    check = svc.get("check", "http")

    if check == "self":
        result["response_ms"] = 0
        return result

    if check == "dns":
        # Worker has no listening port — just verify DNS resolves (container exists)
        import socket
        start = time.monotonic()
        try:
            socket.getaddrinfo(svc["host"], None)
            result["response_ms"] = round((time.monotonic() - start) * 1000)
            result["status"] = "operational"
        except socket.gaierror:
            result["status"] = "major_outage"
        return result

    if check == "tcp":
        start = time.monotonic()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(svc["host"], svc["port"]), timeout=3
            )
            result["response_ms"] = round((time.monotonic() - start) * 1000)
            writer.close()
            await writer.wait_closed()
            result["status"] = "operational"
        except Exception:
            result["status"] = "major_outage"
        return result

    # HTTP check
    start = time.monotonic()
    try:
        resp = await client.get(svc["url"], timeout=5)
        result["response_ms"] = round((time.monotonic() - start) * 1000)
        result["status"] = "operational" if resp.status_code < 500 else "degraded"
    except httpx.TimeoutException:
        result["status"] = "degraded"
        result["response_ms"] = 5000
    except Exception:
        result["status"] = "major_outage"

    return result


@app.get("/autotest", response_class=HTMLResponse)
async def autotest_page(request: Request):
    return templates.TemplateResponse("autotest.html", {"request": request})


@app.get("/docs", response_class=HTMLResponse)
async def docs_page(request: Request):
    return templates.TemplateResponse("docs.html", {"request": request})


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    return templates.TemplateResponse("status.html", {"request": request})


@app.get("/status/api")
async def status_api():
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[check_service(client, svc) for svc in SERVICES])

    all_operational = all(r["status"] == "operational" for r in results)
    any_major = any(r["status"] == "major_outage" for r in results)

    if all_operational:
        overall = "operational"
    elif any_major:
        overall = "major_outage"
    else:
        overall = "degraded"

    return {
        "overall": overall,
        "services": results,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
