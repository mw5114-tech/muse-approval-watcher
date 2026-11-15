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

## GitHub Actions: all-card Muse approval watcher

The public-repository GitHub Actions workflow [muse-approval-watch.yml](.github/workflows/muse-approval-watch.yml) is scheduled every five minutes (`3/5 * * * *`, UTC), the minimum GitHub Actions cron interval. Individual jobs start late or may be skipped; this is **not** a continuously running service. Each scheduled run maintains an authenticated Chromium browser for up to 260 seconds, rescanning the page at roughly one-second intervals. Only one watcher job can run at a time.

The user explicitly requested blanket approval of newly appearing cards, including cards from other chats and scheduled tasks. **This is a high-risk setting.** Automatic approval can permit the Muse agent to share information externally, upload files, send messages, change connected apps, make purchases or take other consequential actions. This mode does not decide whether the requested action is wise, legitimate or safe. Never enable it on an account that has privileged or payment-linked connectors without understanding that risk.

**Config:** [approval_policy.json](approval_policy.json) has `approve_all` (master opt-in), `enabled` (kill switch), `prefer_always` (persistent allow if the UI offers it), `max_approvals_per_run` (bounded queue), and `scan_seconds`. The workflow uses [scripts/approve_all.py](scripts/approve_all.py). The engine recognizes only native Muse approval surfaces with visible Allow and Deny controls; ambiguous or unknown UI is not clicked. It attempts to open the approval panel and process cards sequentially, prioritizing `Always allow` menu options and falling back to `Allow once`. A clicked button is logged as `CLICKED` but not counted as backend acceptance unless separately confirmed in Muse.

**Secrets:** Store the session from the local exporter under `MUSE_AUTH_B64` (or split `MUSE_AUTH_B64_01` etc.) in GitHub repository Actions Secrets. Session values may contain sensitive account access and must never be committed, logged, attached as artifacts, or pasted into chat.

**Verification:** The automated [Muse watcher regression tests](.github/workflows/muse-regression-tests.yml) run on code changes without loading the Muse session secret. [tests/test_approve_all.py](tests/test_approve_all.py) uses synthetic DOM examples including sharing, uploads, multiple approvals, missing controls and a persistent-allow menu. These are not proof that the real Muse permission was accepted. Compare the run's `MUSE_APPROVAL=CLICKED`, `ui=DISMISSED/UNCONFIRMED`, and `MUSE_WATCHER=COMPLETE` lines with the Muse UI. The previous conservative engine is retained in [scripts/actions_approval_watcher.py](scripts/actions_approval_watcher.py) for rollback.

**Emergency stop:** change `enabled` or `approve_all` to `false` in `approval_policy.json`, or disable the workflow in GitHub Actions. In-flight jobs may need cancellation, and existing site-level persistent grants in Muse must be revoked separately if desired. Avoid untrusted pull requests or changes to the workflow in a public repository because maintainers with write access can modify jobs that consume secrets.

### Reliability and health checks (2026-11)

- The official GitHub schedule minimum is five minutes. The workflow stays inside the authenticated browser for **260 seconds** after startup and checks for approvals every ~1 second. Initial setup and GitHub schedule delays can still leave gaps.
- The dedicated [15-minute watchdog](.github/workflows/muse-watchdog.yml) checks GitHub's own scheduled-run history. It fails if the latest schedule is older than 30 minutes, stuck, or concluded unsuccessfully. The watchdog does not receive the Muse authentication secret. Like any GitHub schedule, it can itself be delayed or eventually disabled on inactive public repositories.
- The approval watcher now retries transient page startup failures, checks that authentication survives the run, opens the central review panel even if the current chat has pending cards, and emits a heartbeat about every 45 seconds. Only GitHub status and counts go into logs.
- GitHub may drop a scheduled run, and a button click does not prove Muse accepted the action. Check the Muse right-hand review queue when a click is marked `UNCONFIRMED`, and do not treat tests or a successful job alone as final approval.
- GitHub Actions standard runners on this public repository are free, but this is not a guaranteed always-on service. For a true sub-minute uninterrupted watcher, a locally running browser extension or dedicated server is more appropriate.
