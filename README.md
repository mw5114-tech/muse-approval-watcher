# Muse approval watcher

Playwright Chromium observer for pending Muse approval cards. It uses DOM changes and a 15-second fallback scan, logs request fingerprints, and classifies exact allowlisted public-news reads for manual review. All other scopes are held. The observer has no approval click path.

On 2026-11-15, a separately signed-in browser showed the same nine pending scheduled-task approvals reported by the operator. Eight were labeled external information sharing. The web-access prompt disclosed an HTTP GET for an advertising image and a `tool.exec` policy label. This verifies visibility in a separate browser session. An Oracle-hosted browser, the production selectors, and approval effects still require verification.

Run the local tests with `python -m pip install -r requirements.txt`, `python -m playwright install chromium` and `python -m unittest -v`. The Chromium tests supply a synthetic page and intercept its network requests. They do not contact Muse.

## Deployment

Use a dedicated ARM64 Ubuntu instance named `muse-approval-watcher`. Verify Muse permits the deployment, inspect a genuine pending read-only card, and verify delivery in an independent session before enabling the service.

Run `sudo bash deploy/install.sh` on that instance. Populate `/etc/muse-watcher/config.json` with verified selectors for the visible card, stable request ID, exact pending marker and the four displayed request fields. Rules require exact site, chat, action and scope. The example is deliberately inactive. Review the production configuration before running `sudo systemctl enable --now muse-watcher`.

On a trusted graphical session on the dedicated host, invoke the installed Python as the `muse-watcher` service account with `PLAYWRIGHT_BROWSERS_PATH=/opt/muse-watcher/browsers`, `MUSE_PROFILE=/var/lib/muse-watcher/profile` and the `--login` flag. Stop the observer first so Chromium has exclusive use of its profile. Complete authentication interactively. An operator must arrange the graphical session before running this step.

Use `bash deploy/status.sh` to inspect service state, restart count, memory, CPU and recent logs. systemd supervises Chromium and caps memory at 1.5 GiB. Journald handles log rotation according to the host policy. The observer exits without restart on known authentication and configuration errors.

## Security and recovery

Keep the profile private to the service account. It contains live authentication state. Keep it off GitHub and out of ordinary backups. Keep inbound management access private. Stop the service and revoke the Muse session if profile exposure is suspected.

To roll back on the dedicated VM, run `sudo systemctl disable --now muse-watcher`. A failed service can be inspected with the status script. After correcting the cause, run `sudo systemctl reset-failed muse-watcher` and `sudo systemctl start muse-watcher`. For expired authentication, repeat interactive sign-in using the dedicated profile.

Blind button labels do not establish the type or scope of a Muse request. For native persistent permission, review the actual prompt and apply the product's option manually only to the intended scope. See the [Muse Terms](https://muse.ai/terms) and [Muse connector permissions](https://platform.muse.ai/docs).

## GitHub Actions: five-minute Muse approval watcher

The dedicated [Muse approval watcher](.github/workflows/muse-approval-watch.yml) runs on the standard free public-repository GitHub Actions runner at `3/5 * * * *` (UTC) and can also run manually. This is GitHub's minimum scheduled interval, **not** a latency or continuous-availability guarantee. A scheduled run currently scans for up to 180 seconds, polling roughly once a second after Chromium starts. Schedules may run late or be skipped. The workflow deliberately uses a concurrency group to avoid overlapping approvals.

**Secret:** `MUSE_AUTH_B64` (or the supported split `MUSE_AUTH_B64_01` etc.). This is a credential, never a code file. The login smoke test is separate. If it expires, create a new secret on a trusted local device and re-run the login check; do not store cookies in a public commit, workflow artifact, screenshot or log.

**Current automated approval scope:** [`approval_policy.json`](approval_policy.json) initially allows a recognizable read-only website visit to `yna.co.kr` only. The intended automatic action is an exact allowlisted host and clear "website visit/read" prompt, with both Allow and Deny buttons. An unambiguous `Always allow for this site` option is preferred; otherwise it clicks `Allow once`. Any request that involves external information sharing, connector writes, uploads, posts, purchases, account changes, other domains or unclear wording is **held**. General chat trust or "approve everything" is not implemented.

The scheduled workflow applies this narrow policy; push-triggered setup checks only observe. To inspect without clicking, choose `Run workflow` and `observe`. Inspect job logs for `MUSE_CARD=` and `MUSE_WATCHER=` status; no cookies or raw approval payloads are logged. The existing server watcher (`watcher.py`) is not started by this workflow.

**Verification:** synthetic browser tests cover the allowlist, external sharing denial, button selection, duplicate suppression and observe-only mode. These tests are not evidence that Muse presents identical DOM or that a live approval was processed. A separate authenticated session has been confirmed with GitHub Actions; a live card approval remains unverified until a genuine allowlisted request appears and an operator reviews the resulting action in Muse. Review changes to the policy and Muse UI after upstream updates.
