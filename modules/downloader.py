import subprocess
from pathlib import Path

import yt_dlp

from config import AUDIO_DIR, YTDLP_FORMAT, YTDLP_OUTPUT_TEMPLATE


def _resolve_input(query: str) -> str:
    if query.startswith("http://") or query.startswith("https://"):
        return query
    return f"ytsearch1:{query}"


_AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".flac", ".opus", ".aac", ".ogg"}


def _is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in _AUDIO_EXTENSIONS and path.stat().st_size > 0


def download_audio(query: str) -> Path:
    url = _resolve_input(query)

    ydl_opts = {
        "format": YTDLP_FORMAT,
        "outtmpl": YTDLP_OUTPUT_TEMPLATE,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    downloads = info.get("requested_downloads")
    if downloads:
        fp = Path(downloads[0]["filepath"])
        if _is_audio_file(fp):
            return fp

    files = sorted(
        [f for f in AUDIO_DIR.iterdir() if _is_audio_file(f)],
        key=lambda f: f.stat().st_mtime, reverse=True,
    )
    if files:
        return files[0]

    raise FileNotFoundError(f"Download completed but no audio file found for: {query}")


def validate_audio(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    return True
