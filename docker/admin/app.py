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

app = FastAPI(title="ArduPilot Remotes Admin", docs_url=None, redoc_url=None)

BASE_DIR = os.environ.get("CBS_BASEDIR", "/base")
REMOTES_JSON_PATH = os.path.join(BASE_DIR, "configs", "remotes.json")
BUILDLOGS_DIR = os.environ.get("BUILDLOGS_DIR", "/buildlogs")
CBS_APP_URL = os.environ.get("CBS_APP_URL", "http://custombuild-app:8080")
CBS_RELOAD_TOKEN = os.environ.get("CBS_REMOTES_RELOAD_TOKEN", "")

templates = Jinja2Templates(directory="/app/templates")
app.mount("/admin/static", StaticFiles(directory="/app/static"), name="static")


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
