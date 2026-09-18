# Metabase → Google Sheet sync

Pulls two Metabase saved questions and writes them into tabs of one Google Sheet, on a daily GitHub Actions cron.

- Metabase question **3608** ("User Level Attendance") → sheet tab **`Attendance`**
- Metabase question **7939** ("Assignments") → sheet tab **`Assignment`**

Target sheet: `https://docs.google.com/spreadsheets/d/1_zIfrxLxwRKXKLZAk4xcIIS62jqL-iuYGZ_SWLP0gGA/edit`

Each run does a **full overwrite** of both tabs — clears them and rewrites the current full result set (no `Batch`/`Date`/etc. filters are sent, so it always pulls every batch). This matches the fact that both questions are cumulative snapshots, not event logs, so there's no risk of duplicate rows piling up.

## 1. Get a Metabase API key

In Metabase: **Admin settings → Authentication → API Keys → Create API Key**. Give it a name, grab the key (you only see it once). The key needs read access to the `Newton School` database / whatever collection the two questions live in.

## 2. Create a Google service account and share the sheet

1. In Google Cloud Console, create (or reuse) a project, enable the **Google Sheets API**.
2. **IAM & Admin → Service Accounts → Create Service Account**. No special roles needed (auth happens via the sheet share, not IAM roles).
3. Open the service account → **Keys → Add key → Create new key → JSON**. Download it.
4. Open the JSON file and copy the `client_email` value (looks like `something@your-project.iam.gserviceaccount.com`).
5. Open the target Google Sheet → **Share** → paste that email → give it **Editor** access.

## 3. Put this code in a GitHub repo

Create a new repo (private is fine) and push this folder as-is — it already contains:

```
metabase_to_sheets.py
requirements.txt
.github/workflows/sync_metabase_to_sheets.yml
```

## 4. Add repo secrets

In the repo: **Settings → Secrets and variables → Actions → New repository secret**. Add:

| Secret name | Value |
|---|---|
| `METABASE_URL` | `https://metabase-lierhfgoeiwhr.newtonschool.co` |
| `METABASE_API_KEY` | the API key from step 1 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the **entire contents** of the service account JSON file from step 2 (paste the whole `{...}` as one secret) |
| `SPREADSHEET_ID` | `1_zIfrxLxwRKXKLZAk4xcIIS62jqL-iuYGZ_SWLP0gGA` (the long ID in the sheet's URL) |

Don't commit any of these values into the repo itself — they only ever live as GitHub secrets and as environment variables inside the Action run.

## 5. Run it

- It's scheduled for `0 3 * * *` (03:00 UTC = 08:30 IST) daily — edit the `cron:` line in `.github/workflows/sync_metabase_to_sheets.yml` if you want a different time or frequency.
- You can also trigger it on demand: repo → **Actions** tab → "Sync Metabase attendance & assignments to Google Sheet" → **Run workflow**.
- Check the run's logs in the Actions tab for row counts and any errors.

## Notes / things to know

- **GitHub Actions cron can be delayed** during high load on GitHub's shared runners — it's "best effort," usually within a few minutes, occasionally more. Fine for a daily report; not for anything second-sensitive.
- If a Metabase question's columns ever change (renamed/added/removed), the sheet tab's header row will simply reflect the new columns on the next run, since the whole tab is rewritten.
- To point this at different questions or tab names without touching the code, set `ATTENDANCE_QUESTION_ID`, `ASSIGNMENTS_QUESTION_ID`, `ATTENDANCE_SHEET_NAME`, `ASSIGNMENTS_SHEET_NAME` as additional repo secrets (or plain repo variables) and uncomment the matching lines in the workflow file.
- If you ever want it scoped to one batch again (like the original attendance link's `Batch=DS SQL - January 2024`), that needs a small code change to pass `parameters` in the Metabase API call — say so and it's a quick add.
