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
