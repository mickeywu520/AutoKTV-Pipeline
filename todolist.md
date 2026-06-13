# AutoKTV-Pipeline 開發進度

## ✅ 已完成

### Stage 1：專案基底
- [x] 修正 README.md 編碼為 UTF-8，內容重構
- [x] 建立完整目錄結構（modules/, output/audio|subtitles|videos/）
- [x] `config.py` — 統一設定檔，含 CUDA 自動偵測
- [x] `.gitignore` — 排除輸出檔、虛擬環境、快取
- [x] `requirements.txt` — 釐清所有依賴

### Stage 2：核心 Pipeline
- [x] `modules/downloader.py` — yt-dlp 封裝，支援 URL 與關鍵字
- [x] `modules/separator.py` — Spleeter 2stems 人聲/伴奏分離
- [x] `modules/transcriber.py` — faster-whisper 轉寫 + 逐字 `\k` .ass 輸出

### Stage 3：整合與校正
- [x] `main.py` — CLI 入口，四階段串聯（下載 → 分離 → 轉寫 → 校正）
- [x] ffmpeg 安裝（winget）
- [x] 支援 CPU fallback（無 GPU 可跑）
- [x] 語音預處理（16kHz mono 轉換）提升時間軸準度
- [x] `modules/lyrics_corrector.py` — LRC 網頁爬取 + 重疊匹配校正
- [x] NetEase API 備用（syncedlyrics 失敗時自動切換）
- [x] Used LRC 防重複配對（解決「一瞬間變灰色」）
- [x] Whisper 原始輸出自動備份（`_whisper_raw.ass`）
- [x] 多語支援：`config.LANGUAGE` 控制（zh/en/None），非 zh 跳過簡轉繁
- [x] GPU 加速（Spleeter TensorFlow + faster-whisper CTranslate2）

### 其他修正
- [x] 下載器排除非音訊檔（避免誤抓到 YouTube 自動字幕 .ass）
- [x] 分離器先轉 WAV 再餵 Spleeter（解決 FFmpeg backend `StopIteration`）
- [x] LRC 無結果時，NetEase 網頁 API 備用搜尋
- [x] 跳過 LRC 元資料行 + 亂碼過濾 + 連續重複合併
- [x] 保留 Whisper 原始 `\k` 比例（LRC + 純文字校正路徑），不走均分
- [x] 純文字校正改為：for each Whisper segment → match plain text line（不 merge segment）
- [x] 引入 RapidFuzz（`partial_ratio`）取代 Jaccard 做純文字配對
- [x] 純文字校正改為 `current_pos` 順序匹配（避免副歌亂配）

---

## 🔜 待實作

### Stage 4：KTV 視覺風格
- [ ] `.ass` Style 客製化 — 字型、大小、已唱/未唱顏色（config.py 已定義參數，待整合至產出）
- [ ] 提供多組主題預設（例如：經典紅灰、霓虹藍粉）

### Stage 5：Burn-in 壓制
- [ ] `modules/renderer.py` — FFmpeg 將 .ass 字幕壓入影片
- [ ] `output/videos/xxx_ktv.mp4` 產出
- [ ] 支援 `--burn-in` CLI 開關

### Stage 6：強化與邊角處理
- [ ] 錯誤處理 — 所有 module 的驗證邏輯、loguru 日誌
- [ ] 批次處理（多首歌一次跑）
- [ ] 進度條顯示

### Stage 7：進階功能
- [ ] UVR5 (MDX-Net) 取代 Spleeter（分離品質更好）
- [ ] GUI 介面（選項）
