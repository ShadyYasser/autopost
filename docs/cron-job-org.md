# Reliable triggering with cron-job.org

GitHub's own `schedule` trigger is best-effort: in practice it skipped most of the
30-minute runs on this repository. cron-job.org (free) calls GitHub's
`workflow_dispatch` API every 10 minutes instead. The GitHub schedule stays in the
workflow as a backup.

Nothing publishes until `cron_enabled: true` in `config/settings.yaml`. Runs started
with `trigger=cron` obey that switch exactly like GitHub's schedule, so you can set
this up now.

## 1. Create a fine-grained personal access token (PAT)

1. GitHub → your avatar → **Settings** → **Developer settings** →
   **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. **Token name:** `cron-job.org autopost`
3. **Resource owner:** `ShadyYasser`
4. **Expiration:** the longest you are comfortable with (max 1 year). Put a reminder in
   your calendar to renew it; when it expires the runs stop and only the GitHub backup
   schedule remains.
5. **Repository access:** **Only select repositories** → `ShadyYasser/autopost`
6. **Permissions → Repository permissions:**
   - **Actions: Read and write**
   - (Metadata: Read-only is added automatically. Leave everything else at *No access*.)
7. **Generate token** and copy it (it starts with `github_pat_`). It is shown only once.
   Do not paste it anywhere else, and never commit it.

## 2. Create the cron-job.org job

Sign up / log in at https://console.cron-job.org → **Create cronjob**.

**Common tab**

| Field | Value |
|---|---|
| Title | `autopost` |
| URL | `https://api.github.com/repos/ShadyYasser/autopost/actions/workflows/publish.yml/dispatches` |
| Enable job | on |
| Save responses in job history | on (helps troubleshooting) |
| Execution schedule | **Custom** → Days of month: every · Days of week: every · Months: every · Hours: every · Minutes: `3, 13, 23, 33, 43, 53` |
| Time zone | any (minutes are what matter) |

**Advanced tab**

| Field | Value |
|---|---|
| Request method | `POST` |
| Request timeout | 30 seconds |
| Treat redirects with HTTP 3xx as success | off |

Headers (add each one with **Add header**):

| Key | Value |
|---|---|
| `Accept` | `application/vnd.github+json` |
| `Authorization` | `Bearer github_pat_…` (the token from step 1) |
| `X-GitHub-Api-Version` | `2022-11-28` |
| `Content-Type` | `application/json` |

Request body:

```json
{"ref":"claude/vibrant-cerf-dpwgwd","inputs":{"command":"run","dry_run":"false","trigger":"cron"}}
```

`ref` is the branch the bot runs from. If the code is later merged into `main`, change
it to `"main"`.

**Notifications tab:** turn on *notify me when an execution fails* and *when the job is
disabled because of too many failures*.

Save, then press **Test run**. A correct setup returns **HTTP 204 No Content** and a new
run appears under GitHub → Actions → autopost. With `cron_enabled: false` that run logs
`cron_enabled is false … scheduled run does nothing` and ends.

Common errors:

- 401: wrong or expired token.
- 403: the token lacks **Actions: Read and write**.
- 404: wrong URL, or the token cannot see the repository.
- 422: wrong `ref` or input name.

## 3. Why overlapping triggers are safe

- **One run at a time.** The workflow's `concurrency: group: autopost`
  (`cancel-in-progress: false`) lets one run execute while at most one more waits. If a
  third trigger arrives, GitHub cancels the waiting one and keeps the newest. Nothing
  runs twice in parallel.
- **The waiting run sees the latest state.** Each run checks out the branch tip when it
  starts (`ref: github.ref_name`), not the commit from when it was triggered. It
  therefore sees the `state/state.db` the previous run committed. State is also
  committed after each slot, each platform and each run.
- **Work is claimed in state.** A slot moves `scheduled → in_progress` before anything
  is posted. Each (video, platform) delivery is a row with a unique key. A later run
  only resumes deliveries that are still open; it never starts a slot again.
- **A busy run keeps watching.** A run that is still posting picks up any other slot
  that becomes due, so a waiting run is never needed for that.
- **Push conflicts.** If someone pushes while a run is committing state,
  `scripts/commit_state.sh` rebuilds the state commit on top of the new tip and retries.
- **Timing.** Triggers every 10 minutes with a 15-minute look-ahead mean every target
  is seen by some run before it is due. The run then sleeps until the exact target.
  The GitHub `7,37 * * * *` schedule remains as a backup if cron-job.org is down.
