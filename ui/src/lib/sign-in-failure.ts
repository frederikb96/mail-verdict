/**
 * Whether a response says the sign-in is no longer good. A 401 is the auth
 * proxy asking for a login; a 403 that is not JSON is the proxy refusing the
 * token (the application's own 403 -- not yours, not an admin -- is JSON
 * and stays an ordinary error).
 */
export function isSignInFailure(res: { status: number; headers: Headers }): boolean {
  if (res.status === 401) return true;
  if (res.status !== 403) return false;
  return !(res.headers.get("content-type") ?? "").includes("json");
}
