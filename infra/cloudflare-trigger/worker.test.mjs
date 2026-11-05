// Run with: node --test infra/cloudflare-trigger/worker.test.mjs
import assert from "node:assert/strict";
import test from "node:test";

import worker, { dispatch, dispatchRequest, slotFor } from "./worker.js";

const env = {
  GITHUB_OWNER: "owner",
  GITHUB_REPO: "repo",
  WORKFLOW_FILE: "paper.yml",
  GITHUB_REF: "main",
  GITHUB_TOKEN: "test-token",
};

test("the :17 slot of even UTC hours is the sportsbook slot", () => {
  assert.equal(slotFor(new Date("2026-11-05T04:17:00Z")), "sportsbook");
  assert.equal(slotFor(new Date("2026-11-05T04:19:30Z")), "sportsbook"); // late cron
  assert.equal(slotFor(new Date("2026-11-05T04:47:00Z")), "scanner");
  assert.equal(slotFor(new Date("2026-11-05T05:17:00Z")), "scanner");
  assert.equal(slotFor(new Date("2026-11-05T00:17:00Z")), "sportsbook");
});

test("dispatch posts workflow_dispatch with the slot input", async () => {
  const calls = [];
  const fake = async (url, init) => {
    calls.push({ url, init });
    return new Response(null, { status: 204 });
  };
  assert.equal(await dispatch(env, new Date("2026-11-05T06:17:00Z"), fake), "sportsbook");
  assert.equal(
    calls[0].url,
    "https://api.github.com/repos/owner/repo/actions/workflows/paper.yml/dispatches",
  );
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers.Authorization, "Bearer test-token");
  assert.deepEqual(JSON.parse(calls[0].init.body), { ref: "main", inputs: { slot: "sportsbook" } });
});

test("a non-204 answer throws so the Worker logs a failed invocation", async () => {
  const fake = async () => new Response("Bad credentials", { status: 401 });
  await assert.rejects(dispatch(env, new Date("2026-11-05T06:47:00Z"), fake), /401 Bad credentials/);
});

test("the request body never carries the token", () => {
  const { init } = dispatchRequest(env, new Date("2026-11-05T06:47:00Z"));
  assert.ok(!init.body.includes("test-token"));
});

test("plain HTTP requests are refused", async () => {
  assert.equal((await worker.fetch()).status, 404);
});
