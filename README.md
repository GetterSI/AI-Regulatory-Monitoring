# Cloud Regulatory Watch — setup

This runs entirely on GitHub's own servers, on a schedule — your PC does not need
to be on. It checks 213 regulatory/compliance URLs daily and posts anything that
actually changed to Monday.com, where you'll get an email notification.

One-time setup, about 10 minutes.

## 1. Create a GitHub account (free)

Go to [github.com/join](https://github.com/join) and sign up — it's free, no
card needed.

## 2. Create a new repository

- Click the **+** in the top right → **New repository**.
- Name it e.g. `cloud-regulatory-watch`.
- Set it to **Private** (recommended — it's internal compliance tooling).
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

## 4. Get your Monday.com API token

1. In monday.com, click your profile picture (top right) → **Developers**.
   (If you don't see that, you're an account admin — use **Administration →
   Connections → Personal API token** instead.)
2. Click **API token → Show**, and copy it.

Keep this private — it acts as you inside Monday.

## 5. Add the token as a repo secret

- In your GitHub repo: **Settings → Secrets and variables → Actions → New
  repository secret**.
- Name: `MONDAY_API_TOKEN`
- Value: paste the token you copied.
- Click **Add secret**.

## 6. Let the workflow commit its own results

- **Settings → Actions → General**, scroll to **Workflow permissions**.
- Select **Read and write permissions**.
- Click **Save**.

(Without this, the job can still fetch pages and post to Monday, but it can't
save its own progress between days — every run would think it's starting from
scratch.)

## 7. Test it

- Go to the **Actions** tab → **Daily Regulatory Watch** (on the left) →
  **Run workflow** → **Run workflow** (green button).
- Wait ~2–5 minutes, then click into the run to watch the log. You should see
  a line like `DONE. checked=213 new=213 ...` — the first run is always a
  baseline (everything is "new" since there's nothing to compare against yet),
  so it's normal that nothing posts to Monday on this first run.
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
an extra Monday post costs you a glance and a missed fee change doesn't.

Anything it can't fetch (blocked, empty, timed out) is marked as a coverage
gap — never reported as "no change," since a broken fetch and an unchanged
page must never look the same.

## Where things are

- **Change alerts**: monday.com board "Cloud Regulatory Watch" → item "Daily
  URL Change Monitor". Subscribe to that item to get Monday's own
  notification email.
- **Run history**: `runs.json` in the repo — one entry per day, with counts
  and every change/gap.
- **Current status per page**: `snapshots.json` — what was last seen, when,
  and whether it's currently a gap.

## Note on the earlier Cowork-based version

An earlier version of this same watch ran as a Cowork scheduled task and
wrote to a live dashboard artifact. That task has been deleted to avoid
double-posting to Monday. The dashboard artifact still exists but will no
longer receive new data — say if you'd like it repointed at this repo's data
or retired.
