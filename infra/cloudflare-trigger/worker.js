// Cloudflare Workers cron trigger for the paper bot (v5 Phase 6.1).
//
// GitHub drops many scheduled workflow slots, so this Worker calls the
// `workflow_dispatch` API for paper.yml with inputs.slot. Since v5 Phase 3-E it ticks
// every 10 minutes at :07, :17, ... :57 UTC (one cron expression, never :00/:30). The
// :17 tick of even UTC hours is the sportsbook slot, matching paper.yml's
// "17 */2 * * *" cron; every other tick is a scanner slot. The scanner decides what is
// due (`pbot slot-guard --plan-due`): a tick within 25 minutes of the last scanner run
// is idle unless a pull-plan target is due, so the Worker needs no DST logic.
// paper.yml keeps its own schedule as a fallback; `pbot slot-guard` stops the second
// run when both fire.
//
// Secret: GITHUB_TOKEN, a fine-grained PAT for this repo only with "Actions: read and
// write". Vars (wrangler.toml): GITHUB_OWNER, GITHUB_REPO, WORKFLOW_FILE, GITHUB_REF.

export function slotFor(when) {
  const minute = when.getUTCMinutes();
  // [17, 27) so a late-firing :17 tick still counts, and :07/:27 stay scanner ticks.
  return when.getUTCHours() % 2 === 0 && minute >= 17 && minute < 27 ? "sportsbook" : "scanner";
}

export function dispatchRequest(env, when) {
  const url =
    `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}` +
    `/actions/workflows/${env.WORKFLOW_FILE || "paper.yml"}/dispatches`;
  return {
    url,
    init: {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "paper-bot-cloudflare-trigger",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: env.GITHUB_REF || "main", inputs: { slot: slotFor(when) } }),
    },
  };
}

export async function dispatch(env, when, fetchImpl = fetch) {
  const { url, init } = dispatchRequest(env, when);
  const response = await fetchImpl(url, init);
  // GitHub answers 204 No Content on success.
  if (response.status !== 204) {
    throw new Error(`workflow_dispatch failed: ${response.status} ${await response.text()}`);
  }
  return slotFor(when);
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env, new Date(event.scheduledTime)));
  },
  async fetch() {
    return new Response("paper-bot trigger: cron only\n", { status: 404 });
  },
};
