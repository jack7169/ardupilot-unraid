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
