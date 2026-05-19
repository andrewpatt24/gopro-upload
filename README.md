# gopro-upload

Migrate videos from **GoPro cloud** ([gopro.com/media-library](https://gopro.com/media-library/)) to **Google Drive** with:

- **Low disk use** — chunked streaming (~16 MB buffer), not full-file downloads
- **Resume across sessions** — SQLite tracks progress; Drive resumable uploads survive sleep or Ctrl+C
- **Verification** — reconcile GoPro cloud, Drive folder, and local database

> Uses an **undocumented** GoPro API (same approach as [gopro-plus](https://github.com/itsankoff/gopro-plus)). May break if GoPro changes their backend. Not affiliated with GoPro or Google.

## Requirements

- Python 3.11+
- GoPro subscription with media in the [GoPro media library](https://gopro.com/media-library/)
- [Google Cloud](https://console.cloud.google.com/) project with **Google Drive API** enabled
- Enough Google Drive storage for your library

## Quick start

```bash
git clone https://github.com/andrewpatt24/gopro-upload.git
cd gopro-upload
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .

cp config.example.yaml config.yaml
cp credentials.json.example credentials.json   # then fill in OAuth client id/secret

# 1. GoPro cookies (see below)
python extract_gopro_cookie.py @cookies.txt    # or paste cookie string
eval "$(python extract_gopro_cookie.py @cookies.txt)"

# 2. Google OAuth + app-created folder
gopro-upload auth google
gopro-upload init-folder

# 3. Sanity check
gopro-upload doctor

# 4. Migrate
gopro-upload inventory
gopro-upload migrate --limit 1    # test one file
gopro-upload migrate              # full run
gopro-upload verify
```

---

## Step-by-step setup

### 1. Install the CLI

```bash
cd gopro-upload
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. GoPro authentication (browser cookies)

GoPro does not offer a public API key for cloud downloads. You copy session cookies from the website.

1. Open [https://gopro.com/media-library/](https://gopro.com/media-library/) and sign in.
2. Open DevTools (`Cmd+Option+I` on Mac) → **Network** tab.
3. Filter by `search` or `user`, then refresh or scroll the library.
4. Click a **200** request to `api.gopro.com` (or similar).
5. Find cookies **`gp_access_token`** (JWT, starts with `eyJ`) and **`gp_user_id`**.

**Option A — helper script (recommended)**

Save the full `Cookie:` header line to a file (e.g. `cookies.txt`), then:

```bash
python extract_gopro_cookie.py @cookies.txt
# Copy the two export lines into your terminal:
eval "$(python extract_gopro_cookie.py @cookies.txt)"
```

**Option B — interactive**

```bash
gopro-upload auth gopro
```

**Option C — environment variables**

```bash
export GOPRO_ACCESS_TOKEN='eyJ...'
export GOPRO_USER_ID='your-user-id'
```

Cookies are stored locally at `~/.config/gopro-upload/gopro_auth.json` (mode `600`).  
They **expire**; refresh from the browser when you see GoPro **401** errors.

### 3. Google Cloud OAuth

1. [Google Cloud Console](https://console.cloud.google.com/) → create a project.
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **OAuth consent screen**
   - User type: **External**
   - Add your Google account under **Test users** (while app is in Testing), **or** **Publish app** to Production and use the “unverified app” advanced flow.
   - If test user add fails (“Ineligible accounts”), publish the app or use the account that owns the Cloud project.
4. **Credentials → Create credentials → OAuth client ID → Desktop app**.
5. Copy **Client ID** and **Client secret** into `credentials.json`:

```bash
cp credentials.json.example credentials.json
# Edit credentials.json — do not commit this file
```

6. Authorize the CLI:

```bash
gopro-upload auth google
```

Token saved to `~/.config/gopro-upload/google_token.json`.

### 4. Create the Drive destination folder

This project uses the narrow **`drive.file`** scope: the app can only see **files and folders it creates**, not your entire Drive.

```bash
gopro-upload init-folder
# Optional: gopro-upload init-folder --name "My GoPro Videos"
```

This creates a folder in Drive and writes `drive_folder_id` into `config.yaml`.

Do **not** paste a folder ID from the Drive website URL — manually created folders are invisible to `drive.file`.

### 5. Verify

```bash
gopro-upload doctor
```

Expected:

```
GoPro: OK (sample page has media: True)
Drive: OK — folder 'GoPro Migration'
All checks passed
```

---

## Usage

| Command | Description |
|---------|-------------|
| `gopro-upload doctor` | Test GoPro + Drive connectivity |
| `gopro-upload inventory` | List GoPro cloud library → SQLite |
| `gopro-upload status` | Progress summary |
| `gopro-upload migrate` | Upload pending files (resumable) |
| `gopro-upload migrate --limit N` | Process N files only |
| `gopro-upload verify` | Reconcile GoPro / Drive / database |
| `gopro-upload failures` | Show recent errors |
| `gopro-upload retry-failed` | Reset failed items and clear partial uploads |
| `gopro-upload init-folder` | Create Drive folder (`drive.file` scope) |

### Typical migration session

```bash
gopro-upload inventory
gopro-upload status
gopro-upload migrate --limit 1   # smoke test
gopro-upload migrate             # run until done (Ctrl+C safe)
gopro-upload status
gopro-upload verify
```

Laptop sleep or closing the terminal is fine — run `migrate` again to resume.

---

## Configuration

Copy [`config.example.yaml`](config.example.yaml) to `config.yaml` (gitignored).

| Option | Description |
|--------|-------------|
| `drive_folder_id` | Set automatically by `init-folder` |
| `chunk_size_mb` | Download/upload chunk size (default 16) |
| `db_path` | SQLite database (default `data/migration.db`) |
| `google_scopes` | Default `drive.file` (recommended) |

Environment overrides: `GOPRO_ACCESS_TOKEN`, `GOPRO_USER_ID`.

---

## Debugging

### See what failed

```bash
gopro-upload failures
gopro-upload status
```

SQLite logs: `data/migration.db` → tables `assets`, `transfer_log`.

### GoPro: `401` / authentication failed

- Cookies expired. Re-copy from [gopro.com/media-library](https://gopro.com/media-library/) and run `auth gopro` or `eval "$(python extract_gopro_cookie.py @cookies.txt)"`.
- Wrong product (no cloud library) — `doctor` will fail on GoPro.

### GoPro: stops ~50% with `HTTP 416`

**Cause:** GoPro inventory `file_size` can be larger than the real CDN file. The tool now probes CDN size and prints `Size corrected inventory … → CDN …`.

**Fix:**

```bash
gopro-upload retry-failed
gopro-upload migrate --limit 1
```

Ensure you are on a recent version (includes CDN size probing).

### Drive: `File not found` for folder ID

- **Extra URL junk:** Use only the ID after `/folders/`, not `?lfhs=2` from the URL.
- **Manual folder + `drive.file`:** Run `gopro-upload init-folder` instead of pasting a web folder ID.
- **Wrong scope after changing config:** `gopro-upload auth google --force` then `init-folder`.

### Drive: OAuth blocked / test user issues

- Add your email under **OAuth consent screen → Test users**, or **Publish app**.
- “Ineligible accounts” — try publishing the app, or create the Cloud project while logged into that Google account.
- Re-auth: `gopro-upload auth google --force`

### Drive: `drive.file` vs full `drive` access

Default is **`drive.file`** (safer): only this app’s folder and uploads.  
Do not use a folder ID from the Drive UI unless you switch scopes and re-auth (not recommended for this repo).

### Progress stuck / partial upload

```bash
gopro-upload retry-failed   # clears failed rows + partial Drive state in DB
gopro-upload migrate
```

Orphaned partial files may remain in Drive from failed runs — delete manually in the migration folder if needed.

### Verify report

```bash
gopro-upload verify
```

Writes `reports/verify-*.json` with buckets: `ok`, `missing_on_drive`, `orphan_on_drive`, `mismatch`, `stale_sqlite`.

---

## Security & secrets

**Never commit or share:**

| File | Contains |
|------|----------|
| `config.yaml` | Folder IDs, optional tokens |
| `credentials.json` | Google OAuth client secret |
| `~/.config/gopro-upload/google_token.json` | Google refresh token |
| `~/.config/gopro-upload/gopro_auth.json` | GoPro session cookies |
| `cookies.txt`, `temp` | Raw browser cookies |
| `data/migration.db` | Your library metadata |

All of the above are in [`.gitignore`](.gitignore).  
Before your first push:

```bash
git status
# Ensure credentials.json, config.yaml, temp, data/ are NOT listed
```

Revoke access when finished: [Google Account permissions](https://myaccount.google.com/permissions).

---

## How it works

1. **Inventory** — `GET https://api.gopro.com/media/search` (paginated)
2. **Download** — `GET https://api.gopro.com/media/{id}/download` → signed CDN URL
3. **Transfer** — HTTP Range GET from GoPro → resumable PUT chunks to Google Drive
4. **Match** — `appProperties.gopro_media_id` on each Drive file

Peak local disk: about one chunk + SQLite (~20–50 MB).

---

## License

MIT — see [LICENSE](LICENSE). Use responsibly; comply with GoPro and Google terms of service.
