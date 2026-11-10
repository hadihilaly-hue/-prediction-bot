# Cloudflare cron trigger for the paper bot

GitHub's own `schedule:` trigger dropped 40–70% of the paper bot's 30-minute slots in late October 2026. This Worker runs on Cloudflare's cron, on the free plan, and asks GitHub to start `paper.yml` every 10 minutes (:07, :17, … :57 UTC; one cron expression, never :00 or :30) through the `workflow_dispatch` API, passing `slot=sportsbook` (the :17 tick of even UTC hours) or `slot=scanner`.

Since v5 Phase 3-E most ticks are idle on purpose. A scanner tick within 25 minutes of the last scanner run stops in `pbot slot-guard --plan-due` without writing to the ledger or committing, unless a pull-plan target (an NFL status-report pull, a lineup pull, a T−10m close) is due and funded. So the scanner still runs about every 30 minutes, and odds pulls land within 10 minutes of their target instead of up to 30. The scanner decides what is due, so the Worker needs no daylight-saving logic.

Once an hour, the :37 tick also dispatches `health.yml` (the dead-man check), because GitHub ran only about one in six of that workflow's hourly scheduled slots. The same token covers it.

`paper.yml` keeps its GitHub schedule as a fallback. When both triggers fire for the same slot, the second run finds the first in the ledger (`pbot slot-guard`) and stops before doing anything, so no odds credits are spent twice.

Nothing here can place orders. The token can only start workflow runs in this one repository.

## One-time setup (Hadi)

These steps depend on Cloudflare's and GitHub's current UI. Menu names may have moved since this was written.

1. **Create a fine-grained GitHub token.**
   - GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token.
   - Resource owner: `hadihilaly-hue`. Repository access: **Only select repositories** → `-prediction-bot`.
   - Permissions → Repository permissions → **Actions: Read and write**. GitHub adds "Metadata: Read-only" by itself; grant nothing else.
   - Pick an expiry, note the date (when it expires, the Worker's runs fail and the GitHub fallback carries on), and copy the token.
2. **Create a free Cloudflare account** at <https://dash.cloudflare.com/sign-up>.
3. **Install Wrangler and log in** (needs Node 18+):
   ```bash
   npm install -g wrangler
   wrangler login
   ```
4. **Deploy and store the token.** From this folder:
   ```bash
   cd infra/cloudflare-trigger
   wrangler deploy
   wrangler secret put GITHUB_TOKEN   # paste the token from step 1
   ```
   `wrangler.toml` already sets the cron (`7-57/10 * * * *`), the repo, the workflow file and `main`. Never put the token in that file. If you deployed the earlier `17,47 * * * *` version, `wrangler deploy` again replaces its cron.
5. **Check it.**
   - In GitHub's Actions tab, within 10 minutes, a "Paper trading" run should appear with the event `workflow_dispatch` and a `slot` input. Most runs end after the "Slot guard" step (idle ticks); that is expected.
   - In Cloudflare, Workers & Pages → `paper-bot-trigger` lists the cron trigger and its recent invocations. A failed dispatch shows there as an error with GitHub's reply (for example `401 Bad credentials` if the token is wrong or expired). `wrangler tail` streams the same logs.

6. **Tighten the dead-man check** once runs arrive every 10 minutes: set the repo Variable `PBOT_HEALTH_MAX_AGE_MINUTES` to `40` (Settings → Secrets and variables → Actions → Variables). It defaults to 90 for the GitHub-cron-only schedule.

## Redeploying after a change

`wrangler.toml` and `worker.js` are read at deploy time, so a change in this folder does nothing until you redeploy. The stored `GITHUB_TOKEN` secret is kept:

```bash
cd infra/cloudflare-trigger && wrangler deploy
```

## Testing

```bash
node --test infra/cloudflare-trigger/worker.test.mjs
```

The tests cover the slot rule (including the 10-minute ticks around the sportsbook slot), the hourly `health.yml` dispatch, the cron expression, the requests GitHub receives, and failure on a non-204 answer. `pytest` runs them too when Node is installed.

## Turning it off

`wrangler delete` (from this folder), or remove the `crons` line and run `wrangler deploy`. The GitHub schedule in `paper.yml` keeps running either way.
