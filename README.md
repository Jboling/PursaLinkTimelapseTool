# PrusaLinkConnector

Local web app for **PrusaLink** monitoring and RTSP snapshots, with:

- per-layer snapshots from `sdpos` (Buddy metrics via `M334` + `M331`)
- plain `.gcode` and `.bgcode` layer parsing
- optional Prusa Connect SDK download fallback
- web UI for service control, stream embed, and API console
- photo-folder to MP4 tool (`/tools/photo-video`)

## Requirements

- Python 3.11+ (recommended)
- [FFmpeg](https://ffmpeg.org/) on `PATH` or set `FFMPEG_PATH` in `.env`
- Prusa printer with PrusaLink enabled
- (Optional) `prusactl` login if using Prusa Connect SDK download

## Setup

```powershell
cd C:\Coding\PrusaLinkConnector
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set at least:

- `PRUSA_BASE_URL`
- `PRUSA_USERNAME`
- `PRUSA_PASSWORD`
- `RTSP_URL`

## Run

```powershell
.\.venv\Scripts\python.exe -m app.main
```

Or:

```powershell
.\run-server.bat
```

Open `http://127.0.0.1:8765` (or your configured host/port).

## Getting Prusa Connect IDs (optional)

Enable this only if you want SDK-assisted file downloads:

- set `PRUSA_CONNECT_DOWNLOAD_ENABLED=true`
- set `PRUSA_CONNECT_PRINTER_ID` and optionally `PRUSA_CONNECT_TEAM_ID`

Use `prusactl` to fetch IDs:

```powershell
prusactl auth login
prusactl --format json printers
```

Use each printer object's `uuid` as `PRUSA_CONNECT_PRINTER_ID`.

If you use multiple teams/accounts, get your team ID from:

```powershell
prusactl --format json teams
```

and set `PRUSA_CONNECT_TEAM_ID` to that numeric `id`.

## UDP `sdpos` setup (for `sdpos_layer` mode)

In `.env`:

- `METRICS_UDP_ENABLED=true`
- `METRICS_UDP_BIND=0.0.0.0`
- `METRICS_UDP_PORT=9100`

On printer console (example):

```gcode
M334 <host-ip> 9100
M331 sdpos
```

Then in UI settings, choose snapshot mode: `sdpos_layer`.

## Snapshot folder behavior

- Capture service button **Open snapshot folder** opens the **current job's** output folder.
- Folder path honors your UI settings:
  - date subfolder on/off
  - job-id subfolder on/off
- If no active job ID is available, the endpoint returns an error.

## Stream panel behavior

- The stream panel embeds a browser-playable stream URL directly (no server-side frame polling).
- If using go2rtc, a typical URL looks like:
  - `http://127.0.0.1:1984/stream.html?src=<stream-name>`

## Main endpoints

- `GET /api/printer/status` - live PrusaLink status + job
- `GET /api/service` - worker status, layer progress, download/debug state
- `GET /api/metrics/sdpos` - latest UDP metrics + `sdpos`
- `POST /api/service/start` - start capture worker
- `POST /api/service/stop` - stop capture worker
- `POST /api/snapshot/test` - take one snapshot immediately
- `POST /api/folder/open-snapshot-dir` - open current job snapshot folder (localhost only)

## Configuration files

| Source | Purpose |
|---|---|
| `.env` | Connection secrets, RTSP, ffmpeg path, host/port, UDP metrics, optional Prusa Connect SDK toggles |
| `data/user_settings.json` | Snapshot interval/mode, output folder, clear-zone settings, filename template (managed via UI) |

Do **not** commit `.env` (gitignored).

## License

Use and modify for your own setup. Add a license file if redistributing.
