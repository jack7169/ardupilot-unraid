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
