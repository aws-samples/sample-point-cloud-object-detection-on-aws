/**
 * The one HTTP client every page uses to reach the backend.
 *
 * It holds no credential. Sign-in happens before a request reaches the API: on an
 * internet deployment the load balancer signs the user in with Cognito and keeps the
 * session in its own cookie, which the browser sends with every same-origin call; in
 * port-forward mode the kubectl tunnel is the gate.
 *
 * Every call carries CLIENT_HEADER. The load balancer (and nginx in port-forward
 * mode) refuses /api requests without it. That is the CSRF control for the session
 * cookie: another site cannot add a custom header without a CORS preflight, and the
 * preflight is refused.
 *
 * When the session ends, /api answers 401 if the cookie is gone. If the cookie is
 * there but expired, the load balancer instead redirects the XHR to the Cognito
 * domain, which fails CORS and arrives here as a network error with no response. In
 * both cases the fix is a top-level page load, which the load balancer turns into
 * the sign-in page. The reload is rate-limited through sessionStorage, so a failure
 * that sign-in does not fix cannot loop.
 */
import axios, { type AxiosError } from 'axios';

/** Must match the chart's pcd.clientHeaderName / pcd.clientHeaderValue. */
export const CLIENT_HEADER = 'X-PCD-Client';
export const CLIENT_HEADER_VALUE = 'spa';

/** When this tab last reloaded to sign in again, in ms since the epoch. */
export const REAUTH_KEY = 'pcd.reauthAt';
/** At most one sign-in reload per tab in this window. */
export const REAUTH_INTERVAL_MS = 60_000;

/** Where an earlier version kept the shared API token. */
export const LEGACY_TOKEN_KEY = 'pcd.apiToken';

/** Forget a token left behind by the earlier bearer-token client. */
export function clearLegacyToken(storage: Storage | undefined = globalThis.sessionStorage): void {
  storage?.removeItem(LEGACY_TOKEN_KEY);
}

/**
 * Record a sign-in reload and say whether one may happen now. False if this tab
 * already reloaded within REAUTH_INTERVAL_MS: signing in did not help, so reloading
 * again would only loop.
 */
export function claimReauth(storage: Storage = sessionStorage, now: number = Date.now()): boolean {
  const last = Number(storage.getItem(REAUTH_KEY) ?? 0);
  if (now - last < REAUTH_INTERVAL_MS) return false;
  storage.setItem(REAUTH_KEY, String(now));
  return true;
}

/**
 * Tell an expired session from a real network failure. With an expired session the
 * load balancer answers the page itself with a redirect to sign-in; with the network
 * down, or in port-forward mode, it does not.
 */
export async function sessionExpired(): Promise<boolean> {
  try {
    const probe = await fetch('/', { redirect: 'manual', cache: 'no-store' });
    return probe.type === 'opaqueredirect';
  } catch {
    return false;
  }
}

/**
 * End the session in this browser: nginx expires the load balancer's cookie, then
 * Cognito's /logout ends its own. Neither revokes a token (see the runbook).
 */
export const SIGN_OUT_PATH = '/signout';

clearLegacyToken();

export const api = axios.create({
  baseURL: '/api',
  headers: { [CLIENT_HEADER]: CLIENT_HEADER_VALUE },
});

api.interceptors.response.use(undefined, async (error: AxiosError) => {
  const signedOut =
    error.response?.status === 401 ||
    (!error.response && error.code !== 'ERR_CANCELED' && (await sessionExpired()));
  if (signedOut && claimReauth()) {
    globalThis.location.reload();
  }
  throw error;
});
