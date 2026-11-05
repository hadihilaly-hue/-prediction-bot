# Cloudflare cron trigger for the paper bot

GitHub's own `schedule:` trigger dropped 40–70% of the paper bot's 30-minute slots in late October 2026. This Worker runs on Cloudflare's cron, on the free plan, and asks GitHub to start `paper.yml` at :17 and :47 every hour through the `workflow_dispatch` API, passing `slot=sportsbook` (the :17 slot of even UTC hours) or `slot=scanner`.

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
   `wrangler.toml` already sets the cron (`17,47 * * * *`), the repo, the workflow file and `main`. Never put the token in that file.
5. **Check it.**
   - In GitHub's Actions tab, within half an hour, a "Paper trading" run should appear with the event `workflow_dispatch` and a `slot` input.
   - In Cloudflare, Workers & Pages → `paper-bot-trigger` lists the cron trigger and its recent invocations. A failed dispatch shows there as an error with GitHub's reply (for example `401 Bad credentials` if the token is wrong or expired). `wrangler tail` streams the same logs.

## Testing

```bash
node --test infra/cloudflare-trigger/worker.test.mjs
```

The tests cover the slot rule, the request GitHub receives, and failure on a non-204 answer. `pytest` runs them too when Node is installed.

## Turning it off

`wrangler delete` (from this folder), or remove the `crons` line and run `wrangler deploy`. The GitHub schedule in `paper.yml` keeps running either way.
