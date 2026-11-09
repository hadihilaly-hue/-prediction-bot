// Run with: node --test infra/cloudflare-trigger/worker.test.mjs
import assert from "node:assert/strict";
import test from "node:test";

import worker, {
  dispatch,
  dispatchRequest,
  healthDue,
  healthRequest,
  slotFor,
  tick,
} from "./worker.js";

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

test("the 10-minute ticks around the sportsbook slot stay scanner ticks", () => {
  for (const minute of ["07", "27", "37", "47", "57"]) {
    assert.equal(slotFor(new Date(`2026-11-05T04:${minute}:00Z`)), "scanner", minute);
  }
  assert.equal(slotFor(new Date("2026-11-05T05:17:00Z")), "scanner");
});

test("wrangler.toml ticks every 10 minutes, never on :00 or :30", async () => {
  const { readFile } = await import("node:fs/promises");
  const toml = await readFile(new URL("./wrangler.toml", import.meta.url), "utf8");
  assert.match(toml, /crons = \["7-57\/10 \* \* \* \*"\]/);
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

test("health.yml is due once an hour, on the :37 tick", () => {
  const due = [];
  for (let minute = 7; minute < 60; minute += 10) {
    if (healthDue(new Date(`2026-11-05T04:${String(minute).padStart(2, "0")}:00Z`))) due.push(minute);
  }
  assert.deepEqual(due, [37]);
  assert.equal(healthDue(new Date("2026-11-05T04:39:40Z")), true); // late cron
  assert.equal(healthDue(new Date("2026-11-05T04:47:00Z")), false);
});

test("the health request dispatches health.yml with no inputs", () => {
  const { url, init } = healthRequest(env);
  assert.equal(url, "https://api.github.com/repos/owner/repo/actions/workflows/health.yml/dispatches");
  assert.deepEqual(JSON.parse(init.body), { ref: "main" });
  assert.ok(!init.body.includes("test-token"));
});

test("the :37 tick dispatches paper.yml and health.yml; other ticks only paper.yml", async () => {
  const urls = [];
  const fake = async (url) => {
    urls.push(url);
    return new Response(null, { status: 204 });
  };
  assert.deepEqual(await tick(env, new Date("2026-11-05T04:37:00Z"), fake), ["scanner", "health"]);
  assert.deepEqual(await tick(env, new Date("2026-11-05T04:47:00Z"), fake), ["scanner"]);
  assert.equal(urls.filter((u) => u.includes("/health.yml/")).length, 1);
  assert.equal(urls.filter((u) => u.includes("/paper.yml/")).length, 2);
});

test("a failed health dispatch still sends the paper dispatch, then rejects", async () => {
  const urls = [];
  const fake = async (url) => {
    urls.push(url);
    return url.includes("/health.yml/")
      ? new Response("Not Found", { status: 404 })
      : new Response(null, { status: 204 });
  };
  await assert.rejects(tick(env, new Date("2026-11-05T04:37:00Z"), fake), /404 Not Found/);
  assert.ok(urls.some((u) => u.includes("/paper.yml/")));
});

test("scheduled() runs every due dispatch", async () => {
  const pending = [];
  const urls = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    urls.push(url);
    return new Response(null, { status: 204 });
  };
  try {
    await worker.scheduled(
      { scheduledTime: Date.parse("2026-11-05T06:37:00Z") },
      env,
      { waitUntil: (p) => pending.push(p) },
    );
    await Promise.all(pending);
  } finally {
    globalThis.fetch = realFetch;
  }
  assert.equal(urls.length, 2);
});
