# Cloudflare Worker Auth Gate — Design Spec

**Date:** 2026-04-13
**Status:** Approved
**Purpose:** Add shared-password authentication to the public `https://jforbes.us` endpoint so ArduPilot devs can access the build/test server without exposing it to the open internet. Dev-only feature — minimal changes to the main codebase.

## Requirements

1. Nice-looking login page consistent with existing Bootstrap 5 dark theme
2. Shared password — one password for all devs
3. Browser sessions last 24 hours via signed cookie
4. CLI (`ap-build`) and API support over the public URL
5. Keep Cloudflare Tunnel + bot/DDoS protections active
6. Killswitch: CLI command to instantly block all public access (LAN/Tailscale unaffected)

## Architecture

A Cloudflare Worker on `jforbes.us/*` intercepts every request before it reaches the Tunnel origin. The Worker either:

- Returns the login page (unauthenticated browser request)
- Returns a 401 JSON error (unauthenticated API request)
- Returns a 503 "Site Offline" page (killswitch active)
- Calls `fetch(request)` to pass through to the Tunnel origin (authenticated)

LAN and Tailscale access (`100.99.196.120:8000`) bypasses the Worker entirely — no auth required, killswitch has no effect.

```
Internet → Cloudflare Edge
              ├── Bot/DDoS protection (unchanged)
              ├── Worker (auth gate) ← NEW
              │     ├── /__auth       → login form / API login
              │     ├── /__admin/*    → killswitch toggle
              │     └── /*            → check auth → fetch(origin)
              └── Tunnel → Caddy:8000 → FastAPI services
```

### What changes

| Component | Changes |
|-----------|---------|
| Cloudflare Worker | New — `cloudflare/worker/` directory |
| `ap-build` CLI | Add `login`, `killswitch` commands; modify `api_get`/`api_post` to send cookie/header |
| Docker container | None |
| FastAPI apps | None |
| Caddy config | None |
| Cloudflare Tunnel | None |

## Auth Flow

### Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/__auth` | `GET` | Returns login page HTML (browser) |
| `/__auth` | `POST` | Browser form submission (password in form data) → set cookie, redirect to `/` |
| `/__auth/api` | `POST` | API login (JSON `{"password": "..."}`) → returns JSON with `Set-Cookie` header |
| `/__admin/killswitch` | `POST` | Toggle killswitch (JSON `{"enabled": bool}`) |

### Browser

1. User visits `https://jforbes.us/<any-path>`
2. Worker checks for `cf_auth` cookie
3. No valid cookie → return login page HTML (no origin hit)
4. User submits password via `POST /__auth` (form data)
5. Worker verifies password against `PASSWORD` secret
6. Correct → set `cf_auth` cookie, redirect to original path
7. Wrong → re-render login page with error message

### API / CLI

1. Request includes `X-Auth-Password: <password>` header → verify, pass through
2. Request includes valid `cf_auth` cookie → pass through
3. Neither → return `401 {"error": "authentication required"}`

Detection: Worker checks the `Accept` header. If it contains `application/json`, return JSON 401. Otherwise return HTML login page.

### Cookie Format

```
cf_auth = <expiry_unix_timestamp>.<hmac_sha256_hex(expiry_unix_timestamp, COOKIE_SECRET)>
```

- `COOKIE_SECRET` is a Worker secret (separate from `PASSWORD`) so password rotation doesn't require redeployment
- Verification: parse timestamp, check `now < expiry`, verify HMAC
- Stateless — no KV needed for sessions
- Cookie attributes: `HttpOnly`, `Secure`, `SameSite=Strict`, `Max-Age=86400`, `Path=/`

## Login Page

Inline HTML returned by the Worker. Styled with Bootstrap 5.3.8 via CDN, `data-bs-theme="dark"` to match the existing app.

Layout:
- Centered card on dark background
- Title: "ArduPilot Build Server"
- Single password input field (no username)
- Submit button
- Error message area (conditional, red text)
- No nav, no footer — just the auth gate

## Killswitch

### Storage

Workers KV namespace bound to the Worker.

- Key: `killswitch`
- Value: `"on"` when active, absent/deleted when inactive

### Behavior

On every request, Worker reads the `killswitch` KV key first:

- If `"on"` → return 503 "Site Offline" page (same Bootstrap dark theme, centered message)
- Exception: `POST /__admin/killswitch` is always reachable (requires password auth) so the killswitch can be turned off

### Toggle Endpoint

```
POST /__admin/killswitch
Auth: X-Auth-Password header OR valid cf_auth cookie
Body: {"enabled": true}  or  {"enabled": false}
```

Returns `200 {"killswitch": "on"|"off"}`.

Accepts the same auth methods as any other request (password header or session cookie). This means `ap-build killswitch` works with a stored session — no need to re-enter the password.

## `ap-build` CLI Changes

### New command: `ap-build login`

1. Prompts for password interactively (or reads `AP_AUTH_PASSWORD` env var)
2. POSTs to `$AP_BUILD_URL/__auth/api` with JSON `{"password": "..."}`
3. Stores returned `cf_auth` cookie value in `~/.config/ap-build/cookie`
4. Prints success/failure message

### New command: `ap-build killswitch on|off`

1. Uses stored session cookie from `~/.config/ap-build/cookie` (same auth as other API calls)
2. POSTs to `$AP_BUILD_URL/__admin/killswitch` with cookie header
3. Falls back to `AP_AUTH_PASSWORD` env var or interactive prompt if no stored session
4. Prints new killswitch state

### Modified: `api_get()` / `api_post()`

If `$AP_BUILD_URL` starts with `https://` (i.e., going through Cloudflare):
- If `AP_AUTH_PASSWORD` env var is set → add `-H "X-Auth-Password: $AP_AUTH_PASSWORD"` to curl
- Else if `~/.config/ap-build/cookie` exists → add `-H "Cookie: cf_auth=$(cat cookiefile)"` to curl
- Else → no auth headers (request will fail with 401 if Worker is active)

If `$AP_BUILD_URL` starts with `http://` (LAN/Tailscale) → no auth headers, unchanged behavior.

## Worker Deployment

### File structure

```
cloudflare/worker/
├── wrangler.toml      # Route, KV binding, compatibility config
├── src/
│   └── index.js       # Worker entry point (all logic in one file)
└── package.json       # Minimal — just wrangler dev dependency
```

### Secrets (set via `wrangler secret put`)

| Secret | Purpose |
|--------|---------|
| `PASSWORD` | Shared password devs enter |
| `COOKIE_SECRET` | HMAC key for signing session cookies |

### KV Namespace

| Binding | Purpose |
|---------|---------|
| `AUTH_KV` | Stores killswitch state |

### wrangler.toml

```toml
name = "ardupilot-auth"
main = "src/index.js"
compatibility_date = "2024-01-01"

routes = [
  { pattern = "jforbes.us/*", zone_name = "jforbes.us" }
]

[[kv_namespaces]]
binding = "AUTH_KV"
id = "<created-at-deploy-time>"
```

### Deploy commands

```bash
cd cloudflare/worker
npm install
wrangler kv:namespace create AUTH_KV     # creates namespace, outputs ID
# Update wrangler.toml with the ID
wrangler secret put PASSWORD             # enter shared password
wrangler secret put COOKIE_SECRET        # enter random signing key
wrangler deploy
```

## Cloudflare Access

The existing Cloudflare Access application/policy for `jforbes.us` should be removed or set to Bypass. The Worker now handles authentication. Cloudflare's standard bot protection, DDoS mitigation, and WAF rules remain active regardless of Access policy — those are separate Cloudflare features.

## Security Notes

- The password is stored as a Cloudflare Worker secret (encrypted at rest, not visible in dashboard after setting)
- Cookie is HMAC-signed — cannot be forged without `COOKIE_SECRET`
- Cookie is `HttpOnly` + `Secure` + `SameSite=Strict` — no XSS or CSRF risk
- This is a dev-sharing gate, not production security. A shared password with 24h sessions is appropriate for the threat model (preventing casual/bot access, not targeted attacks)
- Rate limiting on login attempts is not included in v1 but could be added via KV counters if brute-force becomes a concern
