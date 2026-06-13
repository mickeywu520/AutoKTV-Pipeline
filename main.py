import shutil
import sys
import argparse
from pathlib import Path

# 確保專案根目錄在 import 路徑中
sys.path.insert(0, str(Path(__file__).resolve().parent))

from modules.downloader import download_audio, validate_audio
from modules.separator import separate_vocals
from modules.transcriber import transcribe_to_ass
from modules.lyrics_corrector import correct_ass


def main():
    parser = argparse.ArgumentParser(description="AutoKTV-Pipeline")
    parser.add_argument("query", help="YouTube URL 或關鍵字")
    args = parser.parse_args()

    # ── Step 1: 下載 ──
    print(f"[1/4] 正在下載: {args.query}")
    audio_path = download_audio(args.query)
    print(f"      已下載: {audio_path.name} ({audio_path.stat().st_size / 1024:.0f} KB)")

    if not validate_audio(audio_path):
        print("驗證失敗：檔案有問題"); sys.exit(1)

    # ── Step 2: 人聲分離 ──
    print(f"[2/4] 正在分離人聲/伴奏…")
    vocals, accompaniment = separate_vocals(audio_path)
    print(f"      人聲: {vocals.name} ({vocals.stat().st_size / 1024:.0f} KB)")
    print(f"      伴奏: {accompaniment.name} ({accompaniment.stat().st_size / 1024:.0f} KB)")

    # ── Step 3: AI 轉寫 + .ass ──
    print(f"[3/4] 正在轉寫歌詞並產生 KTV 字幕…")
    ass_path = transcribe_to_ass(vocals)
    print(f"      字幕: {ass_path.name} ({ass_path.stat().st_size / 1024:.0f} KB)")

    # 備份 Whisper 原始輸出（校正前）
    raw_backup = ass_path.with_stem(ass_path.stem + "_whisper_raw")
    shutil.copy2(ass_path, raw_backup)
    print(f"      原始備份: {raw_backup.name}")

    # ── Step 4: 外部歌詞校正文詞 ──
    print(f"[4/4] 正在從網路取得歌詞進行校正…")
    before = ass_path.stat().st_size
    correct_ass(ass_path, args.query)
    after = ass_path.stat().st_size
    if after != before:
        print(f"      歌詞校正完成（{before} → {after} bytes）")
    else:
        print("      無外部歌詞，保留 Whisper 原始輸出")
    print("完成")


if __name__ == "__main__":
    main()
