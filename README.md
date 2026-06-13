# AutoKTV-Pipeline

> 讓使用者輸入 YouTube 關鍵字或 URL，自動下載影片 → 人聲/伴奏分離 → AI 逐字時間軸 → 產生 KTV 走字效果 `.ass` →（可選）用 FFmpeg 硬壓至影片

## 1. 輸出結構 (Outputs)

所有輸出集中於 `output/`：

```
output/
├── audio/               # 分離後音訊
│   ├── vocals.wav       # 人聲
│   └── accompaniment.wav # 伴奏
├── subtitles/           # KTV 字幕 .ass
│   └── xxx.ass
└── videos/              # (可選) Burn-in 後影片
    └── xxx_ktv.mp4
```

## 2. 系統流程 (Workflow)

```
輸入關鍵字/URL
    │
    ▼
1. YouTube 下載（yt-dlp）
    │
    ▼
2. 人聲/伴奏分離（Spleeter / UVR5）
    │
    ▼
3. AI 逐字時間軸（faster-whisper / stable-whisper）
    │
    ▼
4. KTV 走字字幕（.ass，含 \k karaoke 標籤）
    │
    ▼
5. (可選) 字幕硬壓制（FFmpeg）
```

## 3. 環境需求與安裝

### 3.1 前置需求

- Python 3.10+
- 系統工具：`ffmpeg`（確認 `ffmpeg -version` 可執行）
- 建議使用 UVR5 (MDX-Net) 取代 Spleeter 以獲得更好分離品質

### 3.2 建立虛擬環境

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

### 3.3 安裝 Python 依賴

```bash
pip install yt-dlp faster-whisper spleeter
```

> 註：`stable-ts` 與 `stable-whisper` 為同一專案的不同版本名稱，本專案統一使用 `faster-whisper` 以獲得更好的速度與準確度。

### 3.4 安裝 ffmpeg

**Ubuntu：**
```bash
sudo apt update && sudo apt install ffmpeg -y
```

**Windows：** 下載 ffmpeg 並加入 PATH，確認 `ffmpeg -version` 可執行。

## 4. 專案目錄結構

```
AutoKTV-Pipeline/
├── config.py              # 統一設定（模型、裝置、輸出路徑、暫存目錄）
├── main.py                # 串聯 Pipeline 主入口 / CLI
├── requirements.txt       # 專案依賴
├── .gitignore
│
├── modules/
│   ├── __init__.py
│   ├── downloader.py      # YouTube 下載（yt-dlp）
│   ├── separator.py       # 人聲分離（Spleeter / UVR5）
│   ├── transcriber.py     # AI 逐字對齊，輸出 .ass
│   └── renderer.py        # (可選) FFmpeg burn-in
│
└── output/
    ├── audio/
    ├── subtitles/
    └── videos/
```

## 5. 參數設計（全集中至 `config.py`）

| 參數 | 說明 | 建議值 |
|---|---|---|
| `MODEL_SIZE` | Whisper 模型大小 | `base` / `small` |
| `DEVICE` | 運算裝置 | `cpu` / `cuda` |
| `KTV_STYLE.font` | 字型 | `Noto Sans TC` |
| `KTV_STYLE.color_sung` | 已唱顏色 | 紅色 / 黃色 |
| `KTV_STYLE.color_unsung` | 未唱顏色 | 灰色 |
| `KTV_STYLE.font_size` | 字體大小 | 依解析度調整 |
| `KTV_STYLE.outline` | 描邊 | 2px |
| `KTV_STYLE.shadow` | 陰影 | 2px |
| `TEMP_DIR` | 暫存目錄 | `temp/` |
| `ASS_EXPORT_PATH` | 字幕輸出目錄 | `output/subtitles/` |

## 6. 模組責任與驗證

### 6.1 `modules/downloader.py`

**行為：**
- 支援 URL 及關鍵字輸入
- 關鍵字使用 `ytsearch1:` 搜尋第一個結果
- 統一轉換為標準格式（mp4 + aac）

**驗證：**
- 確認檔案存在
- 用 `ffprobe` 確認至少包含音訊串流

### 6.2 `modules/separator.py`

**行為：**
- 使用 Spleeter 2stems 進行人聲分離
- 保留 UVR5 介面以便後續升級
- 輸出 `vocals.wav` 與 `accompaniment.wav`

**驗證：**
- 確認 `{video_filename}/vocals.wav` 已存在
- 若不存在則拋出明確錯誤並中止 Pipeline

### 6.3 `modules/transcriber.py`

**行為：**
- 使用 faster-whisper 進行語音轉寫，啟用 `word_timestamps=True`
- 輸出含 `\k` karaoke 標籤的 `.ass` 字幕

**驗證：**
- `audio_path` 必須為可解碼音訊（建議統一取樣率 16kHz mono）
- 輸出 `.ass` 檔案大小 > 0
- 支援 `language` 參數強制指定語言

### 6.4 `modules/renderer.py`（可選）

**行為：**
- 使用 FFmpeg 將 `.ass` 字幕 Burn-in 至影片

**參數：**
- 輸出至 `output/videos/xxx_ktv.mp4`
- 支援 `preset` / `crf` 編碼參數調整

## 7. `.ass` 走字客製化流程

建議分階段實作：

1. **產生 baseline `.ass`** — 確認 `\k` 標籤正常運作
2. **調整 Style 與顏色** — 已唱/未唱顏色、字型設定
3. **精調視覺** — 字型、描邊、陰影、行距

實作方式：
- 在 `transcriber` 產出 `.ass` 後，以字串處理修改 Style / Override
- 使用 ASS 標準 Override Tag（如 `\c&H...&`、`\1c` 等）

## 8. 開發里程碑 (Milestones)

### Phase 1：環境搭建與下載（Day 1-2）
- [ ] `ffmpeg -version` 確認可執行
- [ ] Python 虛擬環境建立
- [ ] `yt-dlp` 測試下載關鍵字第一筆
- [ ] 確認 `output/` 目錄結構建立

### Phase 2：音訊分離與 AI 轉寫（Day 3-5）
- [ ] Spleeter 對單一影片輸出 `vocals.wav`
- [ ] faster-whisper 轉寫 vocals 並產出 `.ass`
- [ ] 確認 `.ass` 可播放且走字正常

### Phase 3：KTV 字幕客製化（Day 6-8）
- [ ] 字型/大小/顏色/陰影視覺調整
- [ ] 已唱/未唱顏色策略實作
- [ ] （可選）歌詞逐字精確對齊校正

### Phase 4：一鍵執行整合（Day 9-10）
- [ ] `main.py` 串聯完整 Pipeline
- [ ] FFmpeg burn-in 輸出 `output/videos/xxx_ktv.mp4`
- [ ] 批次處理支援

## 9. 常見失誤與排查

| 問題 | 原因 | 解決方式 |
|---|---|---|
| yt-dlp 下載格式不相容 | 未統一轉換格式 | 下載後統一轉 mp4 + aac |
| Spleeter 找不到 vocals.wav | 輸出路徑錯誤 | 檢查 separator 輸出路徑是否正確 |
| stable-whisper 輸出空白 .ass | audio 無法解碼或模型太小 | 檢查音訊格式，改用 `small` 以上模型 |
| 字幕顏色/走字不顯示 | ASS 缺少 karaoke tag 或 Style 錯誤 | 確認 `\k` 標籤存在且 Style 設定正確 |

## 10. CLI 使用方式

```bash
python main.py "<YouTube URL or keyword>" \
  --model base \
  --device cuda \
  --burn-in
```

---

## 你的方向是正確的

這份 README 旨在幫你「一次跑通」——Pipeline 從 Day 1 到 Day 10 每一步都可驗證輸出，避免進入後端才發現前端模組沒產出正確檔案。

---

## REF:
```
https://musely.ai/zh/tools/karaoke-subtitle-maker
https://github.com/leeyoshinari/karaoke
https://github.com/chidiwilliams/buzz
https://github.com/ddmmbb-2/OpenKTV-AI/
```

