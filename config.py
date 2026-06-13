from pathlib import Path

# ── 專案根目錄 ──
ROOT_DIR = Path(__file__).resolve().parent

# ── 目錄路徑 ──
OUTPUT_DIR = ROOT_DIR / "output"
AUDIO_DIR = OUTPUT_DIR / "audio"
SUBTITLES_DIR = OUTPUT_DIR / "subtitles"
VIDEOS_DIR = OUTPUT_DIR / "videos"
TEMP_DIR = ROOT_DIR / "temp"

# ── 自動偵測 CUDA ──
try:
    import torch
    DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
except ImportError:
    DEVICE = "cpu"

# ── Whisper 模型設定 ──
MODEL_SIZE = "medium"   # tiny / base / small / medium / large-v3
LANGUAGE = "zh"         # zh / en / None (None = 自動偵測)
COMPUTE_TYPE = "float16" if DEVICE == "cuda" else "int8"

# ── KTV 字幕風格 ──
KTV_STYLE = {
    "font": "Noto Sans TC",
    "font_size": 48,
    "color_sung": "&H0000FF&",       # 已唱：紅色 (ASS BGR 格式)
    "color_unsung": "&H888888&",     # 未唱：灰色
    "outline": 2,
    "shadow": 2,
    "margin_v": 80,                  # 垂直邊距（距底部畫素）
    "karaoke_delay": 0.0,            # 走字延遲（秒）
}

# ── 下載設定 ──
YTDLP_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"
YTDLP_OUTPUT_TEMPLATE = str(AUDIO_DIR / "%(title)s.%(ext)s")

# ── 音訊處理 ──
SAMPLE_RATE = 16000
CHANNELS = 1  # mono
