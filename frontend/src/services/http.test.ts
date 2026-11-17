/**
 * Tests for the shared HTTP client.
 *
 * The load balancer refuses every /api request without the client header, and signs
 * users in only on a top-level page load. So if this client stops sending the
 * header, every page breaks at once; if it stops reloading on an ended session, the
 * app sits on errors instead of the sign-in page; and if the reload is not
 * rate-limited, a sign-in that does not help loops forever. These use real axios with
 * a stub adapter so the interceptors actually run.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios';

import {
  api,
  claimReauth,
  clearLegacyToken,
  CLIENT_HEADER,
  LEGACY_TOKEN_KEY,
  REAUTH_INTERVAL_MS,
  REAUTH_KEY,
} from './http';

function respond(config: InternalAxiosRequestConfig, status: number): AxiosResponse {
  return { data: {}, status, statusText: String(status), headers: {}, config };
}

/** Adapter answering every request with `status`, recording what was sent. */
function adapterReturning(status: number, seen: InternalAxiosRequestConfig[] = []): AxiosAdapter {
  return async (config) => {
    seen.push(config);
    const res = respond(config, status);
    if (status >= 400) {
      throw Object.assign(new Error(String(status)), { isAxiosError: true, config, response: res });
    }
    return res;
  };
}

/** Adapter failing the way a CORS-blocked redirect or a dropped network does. */
function adapterNoResponse(code = 'ERR_NETWORK'): AxiosAdapter {
  return async (config) => {
    throw Object.assign(new Error('Network Error'), { isAxiosError: true, config, code });
  };
}

/** Minimal Storage: vitest runs in the node environment, which has none. */
function memoryStorage(): Storage {
  const m = new Map<string, string>();
  return {
    get length() {
      return m.size;
    },
    clear: () => m.clear(),
    getItem: (k) => m.get(k) ?? null,
    key: (i) => [...m.keys()][i] ?? null,
    removeItem: (k) => void m.delete(k),
    setItem: (k, v) => void m.set(k, v),
  };
}

describe('http client', () => {
  let reload: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    reload = vi.fn();
    vi.stubGlobal('sessionStorage', memoryStorage());
    vi.stubGlobal('location', { reload });
  });
  afterEach(() => vi.unstubAllGlobals());

  it('sends the client header on every request, and no credential', async () => {
    const seen: InternalAxiosRequestConfig[] = [];
    await api.get('/models', { adapter: adapterReturning(200, seen) });
    await api.post('/jobs', {}, { adapter: adapterReturning(200, seen) });
    expect(seen.map((c) => c.headers.get(CLIENT_HEADER))).toEqual(['spa', 'spa']);
    expect(seen.map((c) => c.headers.get('Authorization'))).toEqual([undefined, undefined]);
  });

  it('reloads once to sign in again on a 401, and still rejects', async () => {
    await expect(api.get('/models', { adapter: adapterReturning(401) })).rejects.toBeTruthy();
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('does not reload again straight after a sign-in that did not help', async () => {
    await expect(api.get('/models', { adapter: adapterReturning(401) })).rejects.toBeTruthy();
    await expect(api.get('/models', { adapter: adapterReturning(401) })).rejects.toBeTruthy();
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('does not reload on other errors', async () => {
    await expect(api.get('/models', { adapter: adapterReturning(403) })).rejects.toBeTruthy();
    await expect(api.get('/models', { adapter: adapterReturning(500) })).rejects.toBeTruthy();
    expect(reload).not.toHaveBeenCalled();
  });

  it('reloads when a response-less failure is an expired session', async () => {
    const probe = vi.fn(async () => ({ type: 'opaqueredirect' }) as Response);
    vi.stubGlobal('fetch', probe);
    await expect(api.get('/models', { adapter: adapterNoResponse() })).rejects.toBeTruthy();
    expect(probe).toHaveBeenCalledWith('/', { redirect: 'manual', cache: 'no-store' });
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('does not reload when the network itself is down', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new TypeError('Failed to fetch'))));
    await expect(api.get('/models', { adapter: adapterNoResponse() })).rejects.toBeTruthy();
    expect(reload).not.toHaveBeenCalled();
  });

  it('does not reload when the session is fine (the page loads normally)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ type: 'basic', status: 200 }) as Response));
    await expect(api.get('/models', { adapter: adapterNoResponse() })).rejects.toBeTruthy();
    expect(reload).not.toHaveBeenCalled();
  });

  it('ignores a cancelled request without probing', async () => {
    const probe = vi.fn();
    vi.stubGlobal('fetch', probe);
    await expect(
      api.get('/models', { adapter: adapterNoResponse('ERR_CANCELED') }),
    ).rejects.toBeTruthy();
    expect(probe).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
  });
});

describe('claimReauth', () => {
  it('allows one reload per interval', () => {
    const storage = memoryStorage();
    expect(claimReauth(storage, 1_000_000)).toBe(true);
    expect(storage.getItem(REAUTH_KEY)).toBe('1000000');
    expect(claimReauth(storage, 1_000_000 + REAUTH_INTERVAL_MS - 1)).toBe(false);
    expect(claimReauth(storage, 1_000_000 + REAUTH_INTERVAL_MS)).toBe(true);
  });
});

describe('clearLegacyToken', () => {
  it('removes the token an earlier version stored', () => {
    const storage = memoryStorage();
    storage.setItem(LEGACY_TOKEN_KEY, 'old-shared-token');
    storage.setItem('other', 'kept');
    clearLegacyToken(storage);
    expect(storage.getItem(LEGACY_TOKEN_KEY)).toBeNull();
    expect(storage.getItem('other')).toBe('kept');
  });

  it('is harmless where there is no storage', () => {
    expect(() => clearLegacyToken(undefined)).not.toThrow();
  });
});
