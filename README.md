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

The public-repository GitHub Actions workflow [muse-approval-watch.yml](.github/workflows/muse-approval-watch.yml) is scheduled every fifteen minutes (`3/15 * * * *`, UTC). GitHub Actions can delay or drop scheduled runs. Individual jobs start late or may be skipped; this is **not** a continuously running service. Each scheduled run maintains an authenticated Chromium browser for up to 230 seconds, rescanning the page at roughly one-second intervals. Only one watcher job can run at a time.

The user explicitly requested blanket approval of newly appearing cards, including cards from other chats and scheduled tasks. **This is a high-risk setting.** Automatic approval can permit the Muse agent to share information externally, upload files, send messages, change connected apps, make purchases or take other consequential actions. This mode does not decide whether the requested action is wise, legitimate or safe. Never enable it on an account that has privileged or payment-linked connectors without understanding that risk.

**Config:** [approval_policy.json](approval_policy.json) has `approve_all` (master opt-in), `enabled` (kill switch), `prefer_always` (persistent allow if the UI offers it), `max_approvals_per_run` (bounded queue), and `scan_seconds`. The workflow uses [scripts/approve_all.py](scripts/approve_all.py). The engine recognizes only native Muse approval surfaces with visible Allow and Deny controls; ambiguous or unknown UI is not clicked. It attempts to open the approval panel and process cards sequentially, prioritizing `Always allow` menu options and falling back to `Allow once`. A clicked button is logged as `CLICKED` but not counted as backend acceptance unless separately confirmed in Muse.

**Secrets:** Store the session from the local exporter under `MUSE_AUTH_B64` (or split `MUSE_AUTH_B64_01` etc.) in GitHub repository Actions Secrets. Session values may contain sensitive account access and must never be committed, logged, attached as artifacts, or pasted into chat.

### November 16 reliability changes

- Official GitHub cron remains **five minutes**; it is not a reliable real-time timer. GitHub documentation confirms schedule delays and dropped events during heavy traffic. Recent scheduled runs were all successful when they ran, but their start times varied by **31–49 minutes**. There is no evidence that five-minute cron alone caused a Muse application error, so increasing to ten minutes would increase latency without a proven fix.
- The in-browser polling window was reduced from 260 to **230 seconds**. This leaves a margin for checkout, browser startup, and cleanup before the next five-minute nominal trigger, reducing the chance that concurrent queued runs replace one another.
- Approval processing now re-fetches the card list after each click (avoiding shifting Playwright nth() indexes), watches the exact original card element for dismissal, and applies **at most three attempts per unchanged approval card per run**, with six-second pauses for unconfirmed clicks. If the card contents/controls change, the retry is stopped. This is an explicit best-effort policy, not proof that any external action was accepted; repeated clicks can have side effects.
- At that time, the [watchdog](.github/workflows/muse-watchdog.yml) checked every five minutes (separate concurrency group), requests recovery after missing watcher starts, and keeps its own task status. It does not guarantee GitHub will dispatch on time, even when the scheduled watchdog is enabled.
- The regression suite includes synthetic tests for card removal/reindexing, three-try caps, and changed-card safety. A live acceptance of the **new** code needs to be verified separately in Muse.

**Verification:** The automated [Muse watcher regression tests](.github/workflows/muse-regression-tests.yml) run on code changes without loading the Muse session secret. [tests/test_approve_all.py](tests/test_approve_all.py) uses synthetic DOM examples including sharing, uploads, multiple approvals, missing controls and a persistent-allow menu. These are not proof that the real Muse permission was accepted. Compare the run's `MUSE_APPROVAL=CLICKED`, `ui=DISMISSED/UNCONFIRMED`, and `MUSE_WATCHER=COMPLETE` lines with the Muse UI. The previous conservative engine is retained in [scripts/actions_approval_watcher.py](scripts/actions_approval_watcher.py) for rollback.

**Emergency stop:** change `enabled` or `approve_all` to `false` in `approval_policy.json`, or disable the workflow in GitHub Actions. In-flight jobs may need cancellation, and existing site-level persistent grants in Muse must be revoked separately if desired. Avoid untrusted pull requests or changes to the workflow in a public repository because maintainers with write access can modify jobs that consume secrets.

### Current 15-minute scheduling (2026-11-19)

- Muse approval watcher scheduled at **:03, :18, :33, :48 UTC each hour** (`3/15 * * * *`).
- Independent watchdog scheduled at **:11, :26, :41, :56 UTC each hour** (`11/15 * * * *`). Its recovery threshold is **22 minutes** since the latest main watcher attempt; warning threshold is **30 minutes**, and serious-outage alert threshold remains **90 minutes**.
- This prevents watchdog recovery calls during normal 15-minute spacing while still attempting recovery if a scheduled run is missed or delayed. GitHub may still delay or skip cron deliveries; this is not an exact SLA.
- The actual logged-in Muse polling time remains **230 seconds per run**. Approval policies, repeated attempts, and concurrency protection have not changed.

### Reliability and health checks (2026-11)

- The official GitHub schedule minimum is five minutes. The workflow stays inside the authenticated browser for **230 seconds** after startup and checks for approvals every ~1 second. Initial setup and GitHub schedule delays can still leave gaps.
- The dedicated [15-minute watchdog](.github/workflows/muse-watchdog.yml) checks GitHub's own scheduled-run history. It dispatches a recovery run after a long schedule gap, and fails only for serious or unresolvable conditions. The watchdog does not receive the Muse authentication secret. Like any GitHub schedule, it can itself be delayed or eventually disabled on inactive public repositories.
- The approval watcher now retries transient page startup failures, checks that authentication survives the run, opens the central review panel even if the current chat has pending cards, and emits a heartbeat about every 45 seconds. Only GitHub status and counts go into logs.
- GitHub may drop a scheduled run, and a button click does not prove Muse accepted the action. Check the Muse right-hand review queue when a click is marked `UNCONFIRMED`, and do not treat tests or a successful job alone as final approval.
- GitHub Actions standard runners on this public repository are free, but this is not a guaranteed always-on service. For a true sub-minute uninterrupted watcher, a locally running browser extension or dedicated server is more appropriate.

### Review queue navigation repair (2026-11-16)

Read-only headless diagnostics found nine visible `Needs review` accordion buttons in `hatch-status-panel-sliding-surface` but just one concurrently rendered panel approval card. The earlier watcher scanned the current card without iterating every review row. The new watcher cycles through the review rows and processes only the selected row's approval card; when a card disappears, it moves on. Stable server approval IDs are deduplicated globally, and fallback DOM IDs are scoped to their review row. All card contents, tokens, and request payloads remain omitted from job logs.

One authorized live verification [run 43785503725](https://github.com/mw5114-tech/muse-approval-watcher/actions/runs/43785503725) visited nine review rows and clicked five `Always allow` buttons, each with `ui=DISMISSED`; no additional actionable card was logged during the run. A subsequent read-only [UI inspection run 43786004597](https://github.com/mw5114-tech/muse-approval-watcher/actions/runs/43786004597) found zero rendered approval cards and zero primary approval actions. Some `Needs review` navigation items were still present, so this verifies clearing currently rendered permission cards, **not** that every task review status has been fully resolved. The live approval outcome of each underlying operation has not been independently verified.

### Muse authentication outage and guarded recovery (2026-11-23 KST)

**Evidence:** [Failure 45039724577](https://github.com/mw5114-tech/muse-approval-watcher/actions/runs/45039724577), [failure 45043770950](https://github.com/mw5114-tech/muse-approval-watcher/actions/runs/45043770950), and [failure 45043862057](https://github.com/mw5114-tech/muse-approval-watcher/actions/runs/45043862057) all ended before approvals with `AUTH_REQUIRED_OR_SITE_UNAVAILABLE`. Python/browser installation succeeded. A [read-only diagnostic](https://github.com/mw5114-tech/muse-approval-watcher/actions/runs/45047074066) received HTTP 200 from `muse.ai`, yet after 17 seconds and one reload the authenticated UI was still absent; a sign-in phrase was detected. Two of eight Muse-domain cookies had elapsed expiry timestamps. This is evidence of a signed-out or unverified session, but does NOT identify the particular cookie responsible.

**New safeguards:** Startup waits for authenticated UI hydration and retries at most three loads with backoff. Failed sign-in checks never click approval cards. The sign-in classifier recognizes changed prompt wording. After three recent consecutive watcher failures the watchdog pauses extra recovery dispatches for 45 minutes; the normal 15-minute schedule remains enabled and a successful run resets the breaker. The exporter refuses to write a known logged-out session to Actions Secrets. No secrets, screenshots, user text, or URLs are logged.

**Manual reauthentication:** On the trusted Windows computer that originally exported Muse authentication, run `scripts/export_muse_session.py` in PowerShell after installing repository dependencies and Playwright Chromium. Sign in through the dedicated visible browser window, verify the chat UI appears, and follow prompts to replace GitHub Actions Secret `MUSE_AUTH_B64` (or the split `MUSE_AUTH_B64_XX` secrets). Never copy session values into issues, commits, chats, or logs. Verify the next watcher reports `status=AUTHENTICATED_UI_PRESENT` before expecting approvals to resume.

**Limit:** An expired/revoked session, additional login checkpoint, region restriction, or Muse UI/server change cannot safely be cured with extra button clicks. The watcher intentionally fails closed when the signed-in UI is not verified.

### 2026-11-23 follow-up: persistent sign-in errors

- The [09:09 KST scheduled run](https://github.com/mw5114-tech/muse-approval-watcher/actions/runs/45087065736) reached `muse.ai` (`document_http=200`) but printed `LOGIN_REQUIRED` three times, then failed closed. Earlier runs at approximately 07:35 and 08:25 KST showed the same classification. These are authentication errors, not Playwright browser-install or approval-click exceptions.
- Startup now requires several consecutive `LOGIN_REQUIRED` observations before calling a page signed-out, and verifies on one further reload rather than doing all three lengthy retries when the signed-out UI persists. Transient `INCONCLUSIVE` states still get all bounded retries.
- A failed startup writes an actionable, secret-free GitHub step summary that identifies the classification and points to renewal of the saved Muse session. It never silently treats the failed approval check as successful.
- Watchdog temporarily stops extra recovery dispatches after **two** consecutive failed recent runs for up to **90 minutes**, because the main 15-minute schedule can be delayed significantly. Scheduled runs remain enabled, and successful runs reset this protective cooldown.
- **Manual step remains necessary:** refresh the `MUSE_AUTH_B64` secret from a newly authenticated, trusted local browser session using `scripts/export_muse_session.py`. Code changes alone do not restore expired or revoked credentials. Do not paste secret values in GitHub issues, logs, or chat.

