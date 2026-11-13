import { test } from "node:test";
import assert from "node:assert/strict";
import { isSignInFailure } from "./sign-in-failure.ts";

const res = (status: number, contentType?: string) => ({
  status,
  headers: new Headers(contentType ? { "content-type": contentType } : {}),
});

test("a 401 is a sign-in failure", () => {
  assert.equal(isSignInFailure(res(401, "application/json")), true);
});

test("a 403 that is not JSON is the proxy refusing the token", () => {
  assert.equal(isSignInFailure(res(403, "text/plain")), true);
  assert.equal(isSignInFailure(res(403)), true);
});

test("the application's own JSON 403 is not a sign-in failure", () => {
  assert.equal(isSignInFailure(res(403, "application/json")), false);
});

test("other statuses are not sign-in failures", () => {
  assert.equal(isSignInFailure(res(404, "text/plain")), false);
  assert.equal(isSignInFailure(res(500)), false);
});
