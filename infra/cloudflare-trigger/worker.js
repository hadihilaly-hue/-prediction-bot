// Cloudflare Workers cron trigger for the paper bot (v5 Phase 6.1).
//
// GitHub drops many scheduled workflow slots, so this Worker fires at :17 and :47 UTC
// and calls the `workflow_dispatch` API for paper.yml with inputs.slot. The :17 slot of
// even UTC hours is the sportsbook slot, matching paper.yml's "17 */2 * * *" cron.
// paper.yml keeps its own schedule as a fallback; `pbot slot-guard` stops the second
// run when both fire.
//
// Secret: GITHUB_TOKEN, a fine-grained PAT for this repo only with "Actions: read and
// write". Vars (wrangler.toml): GITHUB_OWNER, GITHUB_REPO, WORKFLOW_FILE, GITHUB_REF.

export function slotFor(when) {
  return when.getUTCHours() % 2 === 0 && when.getUTCMinutes() < 30 ? "sportsbook" : "scanner";
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
