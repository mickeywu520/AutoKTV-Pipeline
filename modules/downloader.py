import subprocess
from pathlib import Path

import yt_dlp

from config import AUDIO_DIR, YTDLP_FORMAT, YTDLP_OUTPUT_TEMPLATE


def _resolve_input(query: str) -> str:
    if query.startswith("http://") or query.startswith("https://"):
        return query
    return f"ytsearch1:{query}"


def download_audio(query: str) -> Path:
    url = _resolve_input(query)

    ydl_opts = {
        "format": YTDLP_FORMAT,
        "outtmpl": YTDLP_OUTPUT_TEMPLATE,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    downloads = info.get("requested_downloads")
    if downloads:
        return Path(downloads[0]["filepath"])

    files = sorted(AUDIO_DIR.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
    if files:
        return files[0]

    raise FileNotFoundError(f"Download completed but no file found for: {query}")


def validate_audio(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    return True
