# Cloud Regulatory Watch — setup

This runs entirely on GitHub's own servers, on a schedule — your PC does not need
to be on. It checks 213 regulatory/compliance URLs daily and, if anything
actually changed, opens a GitHub Issue listing the changes — GitHub emails you
automatically whenever an issue is opened on a repo you own, so that email IS
the notification. No external service, account, or token needed for this part.

It also writes a `DASHBOARD.md` on every run — a live status page (latest
counts, changes, coverage gaps, run history, and current status of all 213
pages) viewable at the repo's normal file URL. The repo is public so
teammates can open that link with no GitHub account needed; only the list of
public regulatory URLs and extracts of their public page text are visible —
nothing else about your account or company.

One-time setup, about 10 minutes.

## 1. Create a GitHub account (free)

Go to [github.com/join](https://github.com/join) and sign up — it's free, no
card needed.

## 2. Create a new repository

- Click the **+** in the top right → **New repository**.
- Name it e.g. `cloud-regulatory-watch`.
- Set it to **Public** so the dashboard is viewable with no GitHub account
  needed (only public regulatory-page data ever appears in it — see above).
  Private also works, but then only people you add as collaborators can view
  anything, dashboard included.
- Leave everything else default, click **Create repository**.

## 3. Upload these files

On your new repo's page: **Add file → Upload files**, then drag in *everything*
in this folder, keeping the folder structure — including the `.github` folder
with `workflows/daily-watch.yml` inside it. (If GitHub's uploader flattens
folders for you, instead use "Add file → Create new file", type the path
`.github/workflows/daily-watch.yml` as the filename, and paste that file's
contents in.)

Files you should end up with in the repo:
```
monitor.py
requirements.txt
watchlist.json
snapshots.json
runs.json
.github/workflows/daily-watch.yml
```

Commit them (the green "Commit changes" button).

## 4. Let the workflow write issues and commit its own results

- **Settings → Actions → General**, scroll to **Workflow permissions**.
- Select **Read and write permissions**.
- Click **Save**.

This one setting covers both jobs the workflow needs to do on its own: open
an issue when it finds a real change, and save its own progress (snapshots
and run history) between days. Nothing to sign up for, no token to create —
GitHub provides this automatically to every workflow run.

## 5. Test it

- Go to the **Actions** tab → **Daily Regulatory Watch** (on the left) →
  **Run workflow** → **Run workflow** (green button).
- Wait ~2–5 minutes, then click into the run to watch the log. You should see
  a line like `DONE. checked=213 new=213 ...` — the first run is always a
  baseline (everything is "new" since there's nothing to compare against yet),
  so it's normal that no issue opens on this first run.
- Check that `snapshots.json` and `runs.json` in the repo were updated with a
  new commit from "cloud-regulatory-watch-bot".

From here it runs automatically every day. **One thing to check:** the
schedule in `daily-watch.yml` is set to 8am UTC — GitHub Actions cron always
runs in UTC, not your local time. Open that file and adjust the cron line to
match 8am wherever you are (a few common examples are commented right above
it in the file).

## How it decides what's "real"

For each page it strips obvious site chrome (nav menus, cookie banners,
social links, footers) before comparing today's text to yesterday's. A
difference only counts as a real change if it's not just whitespace/reordering
and isn't a near-total match (>99.5% similar) — genuine wording tweaks with no
substance get filtered out automatically. When in doubt, it errs toward
flagging something as a change rather than staying silent, on the theory that
an extra issue costs you a glance and a missed fee change doesn't.

Anything it can't fetch (blocked, empty, timed out) is marked as a coverage
gap — never reported as "no change," since a broken fetch and an unchanged
page must never look the same.

## Where things are

- **The dashboard**: `DASHBOARD.md` in the repo — click it on github.com and
  it renders as a page: latest run counts, this run's changes and gaps, a
  30-run history table, and a collapsible full status table for all 213
  pages. Rewritten every run. Share the link with anyone — no GitHub account
  needed to view it, since the repo is public.
- **Change alerts**: the repo's **Issues** tab — one issue per day that had
  real changes, listing every page that changed and why. GitHub emails you
  the moment one opens (as long as your GitHub notification settings send
  email for "Issues" on repos you own — that's the default).
- **Run history**: `runs.json` in the repo — one entry per day, with counts
  and every change/gap.
- **Current status per page**: `snapshots.json` — what was last seen, when,
  and whether it's currently a gap.

## Note on the earlier Cowork/Monday-based version

Earlier versions of this watch ran as a Cowork scheduled task and separately
posted updates to a Monday.com board — both have been retired: the Cowork
task because it only runs while the desktop app is open, and the Monday
posting because it needed an admin-generated API token this account doesn't
have. The dashboard artifact from the Cowork version still exists but no
longer receives new data — say if you'd like it repointed at this repo's data
or retired.
