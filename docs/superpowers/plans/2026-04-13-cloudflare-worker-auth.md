# Cloudflare Worker Auth Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Cloudflare Worker that gates `https://jforbes.us` behind a shared password, with CLI login support and a killswitch.

**Architecture:** A Cloudflare Worker intercepts all requests on `jforbes.us/*`. It checks for a valid session cookie or password header, returning a Bootstrap dark-theme login page for unauthenticated browser requests and a JSON 401 for API requests. A KV-backed killswitch can block all public access. The `ap-build` CLI gets `login` and `killswitch` commands plus automatic auth header injection.

**Tech Stack:** Cloudflare Workers (ES modules), Workers KV, Web Crypto API (HMAC-SHA256), vitest, Bootstrap 5.3.8 CDN, bash

**Spec:** `docs/superpowers/specs/2026-04-13-cloudflare-worker-auth-design.md`

---

### File Structure

```
cloudflare/worker/
├── package.json           # wrangler + vitest deps
├── wrangler.toml          # Route, KV binding, compatibility config
├── vitest.config.js       # Vitest config
├── src/
│   ├── index.js           # Fetch handler, routing, HTML pages
│   └── auth.js            # Cookie sign/verify, cookie parsing (pure functions)
└── test/
    └── auth.test.js       # Unit tests for cookie logic

ap-build                   # Existing CLI — modified for auth
```

---

### Task 1: Scaffold Worker Project

**Files:**
- Create: `cloudflare/worker/package.json`
- Create: `cloudflare/worker/wrangler.toml`
- Create: `cloudflare/worker/vitest.config.js`
- Create: `cloudflare/worker/.gitignore`

- [ ] **Step 1: Create package.json**

```json
{
  "name": "ardupilot-auth",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "wrangler dev",
    "deploy": "wrangler deploy",
    "test": "vitest run"
  },
  "devDependencies": {
    "vitest": "^3.2.1",
    "wrangler": "^4.14.4"
  }
}
```

Write to `cloudflare/worker/package.json`.

- [ ] **Step 2: Create wrangler.toml**

```toml
name = "ardupilot-auth"
main = "src/index.js"
compatibility_date = "2024-09-23"

routes = [
  { pattern = "jforbes.us/*", zone_name = "jforbes.us" }
]

[[kv_namespaces]]
binding = "AUTH_KV"
id = "REPLACE_AFTER_CREATE"
```

Write to `cloudflare/worker/wrangler.toml`. The KV namespace ID is set during deployment (Task 8).

- [ ] **Step 3: Create vitest.config.js**

```javascript
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    globals: true,
  },
});
```

Write to `cloudflare/worker/vitest.config.js`.

- [ ] **Step 4: Create .gitignore**

```
node_modules/
.wrangler/
```

Write to `cloudflare/worker/.gitignore`.

- [ ] **Step 5: Install dependencies**

Run: `cd cloudflare/worker && npm install`
Expected: `node_modules/` created, no errors.

- [ ] **Step 6: Commit**

```bash
git add cloudflare/worker/package.json cloudflare/worker/wrangler.toml cloudflare/worker/vitest.config.js cloudflare/worker/.gitignore cloudflare/worker/package-lock.json
git commit -m "feat: scaffold Cloudflare Worker project for auth gate"
```

---

### Task 2: Auth Module + Tests

**Files:**
- Create: `cloudflare/worker/src/auth.js`
- Create: `cloudflare/worker/test/auth.test.js`

- [ ] **Step 1: Write failing tests**

```javascript
import { describe, it, expect } from 'vitest';
import { signCookie, verifyCookie, parseCookie } from '../src/auth.js';

describe('signCookie', () => {
  it('produces timestamp.hex format', async () => {
    const cookie = await signCookie(1700000000, 'test-secret');
    expect(cookie).toMatch(/^1700000000\.[0-9a-f]{64}$/);
  });

  it('produces consistent signatures', async () => {
    const a = await signCookie(1700000000, 'test-secret');
    const b = await signCookie(1700000000, 'test-secret');
    expect(a).toBe(b);
  });

  it('produces different signatures for different secrets', async () => {
    const a = await signCookie(1700000000, 'secret-a');
    const b = await signCookie(1700000000, 'secret-b');
    expect(a).not.toBe(b);
  });
});

describe('verifyCookie', () => {
  it('verifies a valid non-expired cookie', async () => {
    const future = Math.floor(Date.now() / 1000) + 3600;
    const cookie = await signCookie(future, 'test-secret');
    expect(await verifyCookie(cookie, 'test-secret')).toBe(true);
  });

  it('rejects an expired cookie', async () => {
    const past = Math.floor(Date.now() / 1000) - 3600;
    const cookie = await signCookie(past, 'test-secret');
    expect(await verifyCookie(cookie, 'test-secret')).toBe(false);
  });

  it('rejects a tampered signature', async () => {
    const future = Math.floor(Date.now() / 1000) + 3600;
    const cookie = await signCookie(future, 'test-secret');
    const tampered = cookie.slice(0, -1) + (cookie.slice(-1) === '0' ? '1' : '0');
    expect(await verifyCookie(tampered, 'test-secret')).toBe(false);
  });

  it('rejects wrong secret', async () => {
    const future = Math.floor(Date.now() / 1000) + 3600;
    const cookie = await signCookie(future, 'secret-a');
    expect(await verifyCookie(cookie, 'secret-b')).toBe(false);
  });

  it('rejects malformed input', async () => {
    expect(await verifyCookie('', 'secret')).toBe(false);
    expect(await verifyCookie('not-a-cookie', 'secret')).toBe(false);
    expect(await verifyCookie('abc.def.ghi', 'secret')).toBe(false);
  });
});

describe('parseCookie', () => {
  it('extracts named cookie from header', () => {
    expect(parseCookie('cf_auth=abc123; other=xyz', 'cf_auth')).toBe('abc123');
  });

  it('returns null for missing cookie', () => {
    expect(parseCookie('other=xyz', 'cf_auth')).toBe(null);
  });

  it('handles empty header', () => {
    expect(parseCookie('', 'cf_auth')).toBe(null);
  });

  it('handles cookie with dots in value', () => {
    expect(parseCookie('cf_auth=123.abcdef; x=1', 'cf_auth')).toBe('123.abcdef');
  });
});
```

Write to `cloudflare/worker/test/auth.test.js`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd cloudflare/worker && npx vitest run`
Expected: All tests fail — `auth.js` does not exist yet.

- [ ] **Step 3: Implement auth module**

```javascript
export async function signCookie(expiryTimestamp, secret) {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw',
    encoder.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const signature = await crypto.subtle.sign(
    'HMAC',
    key,
    encoder.encode(String(expiryTimestamp)),
  );
  const hex = [...new Uint8Array(signature)]
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
  return `${expiryTimestamp}.${hex}`;
}

export async function verifyCookie(cookieValue, secret) {
  const dotIndex = cookieValue.indexOf('.');
  if (dotIndex === -1) return false;
  const timestampStr = cookieValue.slice(0, dotIndex);
  const expiry = parseInt(timestampStr, 10);
  if (isNaN(expiry) || Date.now() / 1000 > expiry) return false;
  const expected = await signCookie(expiry, secret);
  if (cookieValue.length !== expected.length) return false;
  const a = new TextEncoder().encode(cookieValue);
  const b = new TextEncoder().encode(expected);
  let result = 0;
  for (let i = 0; i < a.length; i++) result |= a[i] ^ b[i];
  return result === 0;
}

export function parseCookie(cookieHeader, name) {
  const match = cookieHeader.match(new RegExp(`(?:^|;\\s*)${name}=([^;]*)`));
  return match ? match[1] : null;
}
```

Write to `cloudflare/worker/src/auth.js`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd cloudflare/worker && npx vitest run`
Expected: All 12 tests pass.

- [ ] **Step 5: Commit**

```bash
git add cloudflare/worker/src/auth.js cloudflare/worker/test/auth.test.js
git commit -m "feat: add cookie signing/verification module with tests"
```

---

### Task 3: Worker Fetch Handler

**Files:**
- Create: `cloudflare/worker/src/index.js`

- [ ] **Step 1: Write the Worker entry point**

This file contains the main fetch handler, routing, login page HTML, and offline page HTML.

```javascript
import { signCookie, verifyCookie, parseCookie } from './auth.js';

const SESSION_MAX_AGE = 86400; // 24 hours

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Killswitch toggle is always reachable (auth-gated inside handler)
    if (url.pathname === '/__admin/killswitch' && request.method === 'POST') {
      return handleKillswitch(request, env);
    }

    // Check killswitch before anything else
    const killswitch = await env.AUTH_KV.get('killswitch');
    if (killswitch === 'on') {
      return offlinePage();
    }

    // Login page (GET)
    if (url.pathname === '/__auth' && request.method === 'GET') {
      return loginPage('/');
    }

    // Browser form login (POST)
    if (url.pathname === '/__auth' && request.method === 'POST') {
      return handleFormLogin(request, env);
    }

    // API login (POST JSON)
    if (url.pathname === '/__auth/api' && request.method === 'POST') {
      return handleApiLogin(request, env);
    }

    // All other requests: check auth
    if (!(await isAuthenticated(request, env))) {
      const accept = request.headers.get('Accept') || '';
      if (accept.includes('application/json')) {
        return new Response(JSON.stringify({ error: 'authentication required' }), {
          status: 401,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return loginPage(url.pathname + url.search);
    }

    // Authenticated — pass through to origin
    return fetch(request);
  },
};

async function isAuthenticated(request, env) {
  const password = request.headers.get('X-Auth-Password');
  if (password && password === env.PASSWORD) return true;

  const cookieValue = parseCookie(request.headers.get('Cookie') || '', 'cf_auth');
  if (cookieValue && (await verifyCookie(cookieValue, env.COOKIE_SECRET))) return true;

  return false;
}

async function handleFormLogin(request, env) {
  const form = await request.formData();
  const password = form.get('password');
  let redirect = form.get('redirect') || '/';
  if (!redirect.startsWith('/')) redirect = '/';

  if (password !== env.PASSWORD) {
    return loginPage(redirect, 'Incorrect password');
  }

  const expiry = Math.floor(Date.now() / 1000) + SESSION_MAX_AGE;
  const cookie = await signCookie(expiry, env.COOKIE_SECRET);

  return new Response(null, {
    status: 302,
    headers: {
      Location: redirect,
      'Set-Cookie': `cf_auth=${cookie}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${SESSION_MAX_AGE}`,
    },
  });
}

async function handleApiLogin(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: 'invalid JSON' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  if (body.password !== env.PASSWORD) {
    return new Response(JSON.stringify({ error: 'invalid password' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const expiry = Math.floor(Date.now() / 1000) + SESSION_MAX_AGE;
  const cookie = await signCookie(expiry, env.COOKIE_SECRET);

  return new Response(JSON.stringify({ status: 'ok', expires_in: SESSION_MAX_AGE }), {
    status: 200,
    headers: {
      'Content-Type': 'application/json',
      'Set-Cookie': `cf_auth=${cookie}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${SESSION_MAX_AGE}`,
    },
  });
}

async function handleKillswitch(request, env) {
  if (!(await isAuthenticated(request, env))) {
    return new Response(JSON.stringify({ error: 'authentication required' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: 'invalid JSON' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  if (body.enabled) {
    await env.AUTH_KV.put('killswitch', 'on');
  } else {
    await env.AUTH_KV.delete('killswitch');
  }

  return new Response(
    JSON.stringify({ killswitch: body.enabled ? 'on' : 'off' }),
    { status: 200, headers: { 'Content-Type': 'application/json' } },
  );
}

function escapeHtml(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function loginPage(redirect = '/', error = '') {
  const safeRedirect = escapeHtml(redirect);
  const html = `<!DOCTYPE html>
<html lang="en" data-bs-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ArduPilot Build Server</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body {
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background-color: var(--bs-body-bg);
    }
  </style>
</head>
<body>
  <div class="card shadow" style="width: 100%; max-width: 400px;">
    <div class="card-body p-4">
      <h4 class="card-title text-center mb-4">ArduPilot Build Server</h4>
      ${error ? `<div class="alert alert-danger py-2">${escapeHtml(error)}</div>` : ''}
      <form method="POST" action="/__auth">
        <input type="hidden" name="redirect" value="${safeRedirect}">
        <div class="mb-3">
          <input type="password" class="form-control" name="password"
                 placeholder="Password" autofocus required>
        </div>
        <button type="submit" class="btn btn-primary w-100">Sign In</button>
      </form>
    </div>
  </div>
</body>
</html>`;
  return new Response(html, {
    status: 401,
    headers: { 'Content-Type': 'text/html;charset=UTF-8' },
  });
}

function offlinePage() {
  const html = `<!DOCTYPE html>
<html lang="en" data-bs-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Site Offline</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body {
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background-color: var(--bs-body-bg);
    }
  </style>
</head>
<body>
  <div class="text-center">
    <h2 class="mb-3">Site Offline</h2>
    <p class="text-muted">This site is temporarily unavailable. Please try again later.</p>
  </div>
</body>
</html>`;
  return new Response(html, {
    status: 503,
    headers: { 'Content-Type': 'text/html;charset=UTF-8' },
  });
}
```

Write to `cloudflare/worker/src/index.js`.

- [ ] **Step 2: Verify tests still pass**

Run: `cd cloudflare/worker && npx vitest run`
Expected: All 12 tests still pass (index.js doesn't break auth.js imports).

- [ ] **Step 3: Commit**

```bash
git add cloudflare/worker/src/index.js
git commit -m "feat: add Worker fetch handler with login page, auth check, and killswitch"
```

---

### Task 4: `ap-build` Auth Support

**Files:**
- Modify: `ap-build`

This task adds auth header injection to all existing curl calls. No new commands yet — just the plumbing.

- [ ] **Step 1: Add auth config and AUTH_ARGS array**

After line 9 (`AUTOTEST_API="${BASE_URL}/autotest/api"`), add:

```bash
# Auth
AUTH_COOKIE_FILE="${HOME}/.config/ap-build/cookie"
AUTH_ARGS=()
if [[ "$BASE_URL" == https://* ]]; then
    if [[ -n "${AP_AUTH_PASSWORD:-}" ]]; then
        AUTH_ARGS=(-H "X-Auth-Password: ${AP_AUTH_PASSWORD}")
    elif [[ -f "$AUTH_COOKIE_FILE" ]]; then
        AUTH_ARGS=(-H "Cookie: cf_auth=$(cat "$AUTH_COOKIE_FILE")")
    fi
fi
```

- [ ] **Step 2: Update api_get to include auth**

Change `api_get()` (currently line 27-34) from:

```bash
api_get() {
    local response
    response=$(curl -s -w '\n%{http_code}' "${1}" 2>/dev/null) || err "Cannot reach: GET $1"
```

to:

```bash
api_get() {
    local response
    response=$(curl -s -w '\n%{http_code}' ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} "${1}" 2>/dev/null) || err "Cannot reach: GET $1"
```

- [ ] **Step 3: Update api_post to include auth**

Change `api_post()` (currently line 35-42) from:

```bash
api_post() {
    local response
    response=$(curl -s -w '\n%{http_code}' -X POST "${1}" -H 'Content-Type: application/json' -d "$2" 2>/dev/null) || err "Cannot reach: POST $1"
```

to:

```bash
api_post() {
    local response
    response=$(curl -s -w '\n%{http_code}' ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} -X POST "${1}" -H 'Content-Type: application/json' -d "$2" 2>/dev/null) || err "Cannot reach: POST $1"
```

- [ ] **Step 4: Update all direct curl calls**

There are 10 direct `curl` calls in the script that bypass `api_get`/`api_post`. Add `${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"}` to each one. The affected functions and their curl calls:

**`show_build_logs` follow mode (2 calls):**
```bash
# Line ~264: change
logs=$(curl -sf "${API}/builds/${build_id}/logs" 2>/dev/null || echo "")
# to
logs=$(curl -sf ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} "${API}/builds/${build_id}/logs" 2>/dev/null || echo "")

# Line ~274: change
state=$(curl -sf "${API}/builds/${build_id}" 2>/dev/null | jq -r '.progress.state' 2>/dev/null || echo "UNKNOWN")
# to
state=$(curl -sf ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} "${API}/builds/${build_id}" 2>/dev/null | jq -r '.progress.state' 2>/dev/null || echo "UNKNOWN")
```

**`download_artifact` (1 call):**
```bash
# Line ~300: change
curl -s -f "${API}/builds/${build_id}/artifact" -o "$output" || err "Artifact not available for build ${build_id}"
# to
curl -s -f ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} "${API}/builds/${build_id}/artifact" -o "$output" || err "Artifact not available for build ${build_id}"
```

**`test_logs` follow mode (2 calls):**
```bash
# Line ~460: change
logs=$(curl -sf "${AUTOTEST_API}/tests/${test_id}/logs" 2>/dev/null || echo "")
# to
logs=$(curl -sf ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} "${AUTOTEST_API}/tests/${test_id}/logs" 2>/dev/null || echo "")

# Line ~470: change
state=$(curl -sf "${AUTOTEST_API}/tests/${test_id}" 2>/dev/null | jq -r '.state' 2>/dev/null || echo "UNKNOWN")
# to
state=$(curl -sf ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} "${AUTOTEST_API}/tests/${test_id}" 2>/dev/null | jq -r '.state' 2>/dev/null || echo "UNKNOWN")
```

**`test_watch` (2 calls):**
```bash
# Line ~489: change
data=$(curl -sf "${AUTOTEST_API}/tests/${test_id}" 2>/dev/null) || { sleep 2; continue; }
# to
data=$(curl -sf ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} "${AUTOTEST_API}/tests/${test_id}" 2>/dev/null) || { sleep 2; continue; }

# Line ~496: change
logs=$(curl -sf "${AUTOTEST_API}/tests/${test_id}/logs" 2>/dev/null || echo "")
# to
logs=$(curl -sf ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} "${AUTOTEST_API}/tests/${test_id}/logs" 2>/dev/null || echo "")
```

**`batch_watch` (1 call):**
```bash
# Line ~670: change
data=$(curl -s --max-time 10 "${AUTOTEST_API}/batches/${batch_id}" 2>/dev/null) || { sleep 2; continue; }
# to
data=$(curl -s --max-time 10 ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} "${AUTOTEST_API}/batches/${batch_id}" 2>/dev/null) || { sleep 2; continue; }
```

**`batch_wait` (1 call):**
```bash
# Line ~756: change
response=$(curl -s -w '\n%{http_code}' "${AUTOTEST_API}/batches/${batch_id}/wait?timeout=${timeout}" 2>/dev/null) || err "Cannot reach server"
# to
response=$(curl -s -w '\n%{http_code}' ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} "${AUTOTEST_API}/batches/${batch_id}/wait?timeout=${timeout}" 2>/dev/null) || err "Cannot reach server"
```

**`batch_summary` (1 call):**
```bash
# Line ~651: change
curl -s "${AUTOTEST_API}/batches/${batch_id}/summary" 2>/dev/null || err "Failed to fetch batch summary"
# to
curl -s ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} "${AUTOTEST_API}/batches/${batch_id}/summary" 2>/dev/null || err "Failed to fetch batch summary"
```

- [ ] **Step 5: Verify script parses correctly**

Run: `bash -n ap-build`
Expected: No syntax errors.

- [ ] **Step 6: Verify existing commands still work over HTTP (no auth injected)**

Run: `AP_BUILD_URL=http://100.99.196.120:8000 ./ap-build list vehicles`
Expected: Same output as before — AUTH_ARGS is empty for `http://` URLs.

- [ ] **Step 7: Commit**

```bash
git add ap-build
git commit -m "feat: add auth header injection to ap-build for HTTPS endpoints"
```

---

### Task 5: `ap-build` Login Command

**Files:**
- Modify: `ap-build`

- [ ] **Step 1: Add the `do_login` function**

Add this function in a new `# AUTH COMMANDS` section before the `# MAIN` section:

```bash
# ============================================================
# AUTH COMMANDS
# ============================================================

do_login() {
    local password="${AP_AUTH_PASSWORD:-}"
    if [[ -z "$password" ]]; then
        echo -n "Password: "
        read -rs password
        echo
    fi
    [[ -z "$password" ]] && err "Password required"

    local full_response
    full_response=$(curl -si -X POST "${BASE_URL}/__auth/api" \
        -H 'Content-Type: application/json' \
        -d "$(jq -n --arg p "$password" '{password: $p}')" 2>/dev/null) || err "Cannot reach server"

    local http_code
    http_code=$(echo "$full_response" | head -1 | grep -o '[0-9][0-9][0-9]')

    if [[ "$http_code" -ge 200 && "$http_code" -lt 300 ]]; then
        local cookie_value
        cookie_value=$(echo "$full_response" | grep -i 'set-cookie:' | grep -o 'cf_auth=[^;]*' | cut -d= -f2)

        if [[ -z "$cookie_value" ]]; then
            err "Login succeeded but no session cookie received"
        fi

        mkdir -p "$(dirname "$AUTH_COOKIE_FILE")"
        echo "$cookie_value" > "$AUTH_COOKIE_FILE"
        chmod 600 "$AUTH_COOKIE_FILE"
        success "Logged in — session stored (expires in 24h)"
    else
        err "Login failed — check your password"
    fi
}
```

- [ ] **Step 2: Add `login` to the main case statement**

In the main `case "$cmd" in` block (around line 794), add before the `help` case:

```bash
    login)    do_login ;;
```

- [ ] **Step 3: Add login to usage text**

In the `usage()` function, add a new section after the `GIT` section and before `ENVIRONMENT`:

```
AUTH:
  ap-build login                                      Log in to remote server
  ap-build killswitch <on|off>                        Toggle public access killswitch

ENVIRONMENT:
  AP_BUILD_URL       Base URL (default: http://100.99.196.120:8000)
  AP_AUTH_PASSWORD    Password for auto-login (skips interactive prompt)
```

- [ ] **Step 4: Verify script parses correctly**

Run: `bash -n ap-build`
Expected: No syntax errors.

- [ ] **Step 5: Commit**

```bash
git add ap-build
git commit -m "feat: add 'ap-build login' command for auth to public endpoint"
```

---

### Task 6: `ap-build` Killswitch Command

**Files:**
- Modify: `ap-build`

- [ ] **Step 1: Add the `do_killswitch` function**

Add after `do_login()` in the AUTH COMMANDS section:

```bash
do_killswitch() {
    local action="${1:?Usage: ap-build killswitch <on|off>}"
    local enabled
    case "$action" in
        on)  enabled=true ;;
        off) enabled=false ;;
        *)   err "Usage: ap-build killswitch <on|off>" ;;
    esac

    # Build auth for this specific call (killswitch may be called before AUTH_ARGS are set)
    local -a auth=()
    if [[ -n "${AP_AUTH_PASSWORD:-}" ]]; then
        auth=(-H "X-Auth-Password: ${AP_AUTH_PASSWORD}")
    elif [[ -f "$AUTH_COOKIE_FILE" ]]; then
        auth=(-H "Cookie: cf_auth=$(cat "$AUTH_COOKIE_FILE")")
    else
        err "Not logged in. Run 'ap-build login' first or set AP_AUTH_PASSWORD"
    fi

    local response
    response=$(curl -s -w '\n%{http_code}' ${auth[@]+"${auth[@]}"} \
        -X POST "${BASE_URL}/__admin/killswitch" \
        -H 'Content-Type: application/json' \
        -d "{\"enabled\": ${enabled}}" 2>/dev/null) || err "Cannot reach server"

    local code="${response##*$'\n'}"
    local body="${response%$'\n'*}"

    [[ "$code" -ge 200 && "$code" -lt 300 ]] || err "$(echo "$body" | jq -r '.error // .' 2>/dev/null) (HTTP $code)"

    local state
    state=$(echo "$body" | jq -r '.killswitch')
    if [[ "$state" == "on" ]]; then
        echo -e "${RED}Killswitch ON — public access blocked${NC}"
    else
        success "Killswitch OFF — public access open"
    fi
}
```

- [ ] **Step 2: Add `killswitch` to the main case statement**

In the main `case "$cmd" in` block, add after the `login` case:

```bash
    killswitch) do_killswitch "$@" ;;
```

- [ ] **Step 3: Verify script parses correctly**

Run: `bash -n ap-build`
Expected: No syntax errors.

- [ ] **Step 4: Commit**

```bash
git add ap-build
git commit -m "feat: add 'ap-build killswitch' command to toggle public access"
```

---

### Task 7: Local Testing with `wrangler dev`

**Files:** None modified — manual verification only.

- [ ] **Step 1: Start Worker locally**

Run: `cd cloudflare/worker && npx wrangler dev --local`

This starts a local dev server (usually `http://localhost:8787`) with a local KV emulation. Secrets need to be provided — create a `.dev.vars` file (gitignored by `.wrangler/`):

```
PASSWORD=testpass123
COOKIE_SECRET=devsecret123456
```

Write to `cloudflare/worker/.dev.vars` (this file is auto-gitignored by wrangler).

- [ ] **Step 2: Test unauthenticated browser request**

Run: `curl -s http://localhost:8787/`
Expected: HTML response containing "ArduPilot Build Server" and a password form. HTTP 401.

- [ ] **Step 3: Test unauthenticated API request**

Run: `curl -s -H 'Accept: application/json' http://localhost:8787/api/v1/vehicles`
Expected: `{"error":"authentication required"}` with HTTP 401.

- [ ] **Step 4: Test browser form login (wrong password)**

Run: `curl -s -X POST http://localhost:8787/__auth -d 'password=wrong&redirect=/'`
Expected: HTML with "Incorrect password" error message.

- [ ] **Step 5: Test browser form login (correct password)**

Run: `curl -si -X POST http://localhost:8787/__auth -d 'password=testpass123&redirect=/'`
Expected: HTTP 302 with `Location: /` and `Set-Cookie: cf_auth=...` header.

- [ ] **Step 6: Test API login**

Run: `curl -si -X POST http://localhost:8787/__auth/api -H 'Content-Type: application/json' -d '{"password":"testpass123"}'`
Expected: HTTP 200 with `{"status":"ok","expires_in":86400}` and `Set-Cookie: cf_auth=...` header.

- [ ] **Step 7: Test authenticated request with cookie**

Using the cookie value from step 6:

Run: `curl -s -H 'Cookie: cf_auth=<value_from_step_6>' http://localhost:8787/`
Expected: Request passes through to origin (or connection refused if no origin running — the point is it's not a 401).

- [ ] **Step 8: Test authenticated request with password header**

Run: `curl -s -H 'X-Auth-Password: testpass123' -H 'Accept: application/json' http://localhost:8787/api/v1/vehicles`
Expected: Request passes through (not 401).

- [ ] **Step 9: Test killswitch on**

Run: `curl -s -X POST http://localhost:8787/__admin/killswitch -H 'X-Auth-Password: testpass123' -H 'Content-Type: application/json' -d '{"enabled":true}'`
Expected: `{"killswitch":"on"}`

Then: `curl -s http://localhost:8787/`
Expected: "Site Offline" page with HTTP 503.

- [ ] **Step 10: Test killswitch off**

Run: `curl -s -X POST http://localhost:8787/__admin/killswitch -H 'X-Auth-Password: testpass123' -H 'Content-Type: application/json' -d '{"enabled":false}'`
Expected: `{"killswitch":"off"}`

Then: `curl -s http://localhost:8787/`
Expected: Back to login page (not offline page).

- [ ] **Step 11: Run unit tests one final time**

Run: `cd cloudflare/worker && npx vitest run`
Expected: All tests pass.

---

### Task 8: Deployment

**Files:**
- Modify: `cloudflare/worker/wrangler.toml` (update KV namespace ID)

This task is run once to deploy the Worker to Cloudflare. It requires `wrangler` to be authenticated (`wrangler login`).

- [ ] **Step 1: Authenticate wrangler**

Run: `cd cloudflare/worker && npx wrangler login`
Expected: Browser opens for Cloudflare OAuth. After auth, wrangler stores credentials locally.

- [ ] **Step 2: Create KV namespace**

Run: `cd cloudflare/worker && npx wrangler kv namespace create AUTH_KV`
Expected: Output includes the namespace ID, e.g.: `id = "abc123..."`

- [ ] **Step 3: Update wrangler.toml with KV namespace ID**

Replace the `REPLACE_AFTER_CREATE` placeholder in `wrangler.toml` with the actual ID from step 2.

- [ ] **Step 4: Set Worker secrets**

Run:
```bash
cd cloudflare/worker
echo '<your-chosen-password>' | npx wrangler secret put PASSWORD
openssl rand -hex 32 | npx wrangler secret put COOKIE_SECRET
```

- [ ] **Step 5: Deploy**

Run: `cd cloudflare/worker && npx wrangler deploy`
Expected: Worker deployed to `jforbes.us/*` route.

- [ ] **Step 6: Remove or bypass Cloudflare Access policy**

In the Cloudflare Zero Trust dashboard:
1. Go to Access > Applications
2. Find the `jforbes.us` application
3. Either delete it or change the policy to Bypass

This is a manual dashboard step — the Worker now handles auth.

- [ ] **Step 7: Smoke test**

Open `https://jforbes.us` in a browser. Expected: login page appears. Enter the password. Expected: redirected to the app.

Run: `AP_BUILD_URL=https://jforbes.us ./ap-build login`
Expected: prompts for password, stores cookie, prints success.

Run: `AP_BUILD_URL=https://jforbes.us ./ap-build list vehicles`
Expected: vehicle list returned (same as over Tailscale).

- [ ] **Step 8: Commit wrangler.toml with real KV ID**

```bash
git add cloudflare/worker/wrangler.toml
git commit -m "chore: set KV namespace ID after deployment"
```
