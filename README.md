# PursaLinkTimelapseTool

Local web app for **PrusaLink** printer monitoring (status, RTSP snapshots) and a **photos-to-video** tool that builds MP4 timelapses with ffmpeg (frame selection, optional hold on the last frame).

## Requirements

- Python 3.11+ (recommended)
- [FFmpeg](https://ffmpeg.org/) on `PATH` or set `FFMPEG_PATH` in `.env`
- A Prusa printer with **PrusaLink** (network API + optional RTSP stream)

## Setup

```powershell
cd PursaLinkTimelapseTool
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
# Edit .env: Prusa URL, digest credentials, RTSP URL, HOST/PORT, optional FFMPEG_PATH
```

## Run

```powershell
.\run-server.bat
```

Or:

```powershell
.\.venv\Scripts\python.exe -m app.main
```

Open **http://127.0.0.1:8765** (or your configured host/port).

- **/** — Printer dashboard and snapshot capture service  
- **/tools/photo-video** — Assemble images from a folder into an MP4

## Configuration

| Source        | Purpose                                      |
|---------------|----------------------------------------------|
| `.env`        | Secrets, Prusa URL, RTSP, ffmpeg, bind host  |
| `data/user_settings.json` | Snapshot interval, output folder, filename template (via UI) |

Do **not** commit `.env` (it is gitignored).

## License

Use and modify for your own setup. Add a license file if you redistribute.
