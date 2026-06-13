import json
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import quote

import syncedlyrics
from zhconv import convert as s2t

from config import SUBTITLES_DIR, ROOT_DIR, LANGUAGE

_METADATA_PREFIXES = (
    "作词", "作曲", "作詞", "編曲", "编曲", "弦乐", "弦樂", "冲绳", "沖繩",
    "和声", "和聲", "合唱", "OP", "SP", "录音", "錄音", "混音", "母带", "母帶",
    "制作", "製作", "制作人", "製作人", "配唱", "吉他", "贝斯", "貝斯",
    "鼓", "键盘", "鍵盤", "钢琴", "鋼琴", "艺人", "藝人", "推广", "推廣",
    "未经", "未經", "词：", "曲：", "词 :", "曲 :", "OP：", "SP：",
    "原唱", "改编", "改編", "人声", "人聲", "音响", "音響", "乐队", "樂隊",
    "贝司", "貝司", "總監", "总监", "监制", "監製",
)

_METADATA_ANYWHERE = (
    "著作权人", "著作權人", "翻唱", "翻录", "翻錄", "不得",
)


def _fetch_lrc_local(query: str) -> list[tuple[float, float, str]] | None:
    stem = re.sub(r'[\\/:*?"<>| ]', "_", query)
    candidates = [
        ROOT_DIR / "lyrics.lrc",
        ROOT_DIR / f"{stem}.lrc",
        ROOT_DIR / f"{query}.lrc",
    ]
    for p in candidates:
        if p.exists():
            return _parse_lrc_text(p.read_text(encoding="utf-8"))
    return None


def _parse_lrc_text(raw: str) -> list[tuple[float, float, str]]:
    raw_lines = []
    for line in raw.strip().splitlines():
        m = re.match(r"\[(\d+):(\d+\.\d+)\](.*)", line)
        if m:
            mins, secs, text = int(m.group(1)), float(m.group(2)), m.group(3).strip()
            if text and _is_lyric_line(text):
                time_sec = mins * 60 + secs
                raw_lines.append((time_sec, text))

    raw_lines.sort(key=lambda x: x[0])
    result = []
    for i, (t, text) in enumerate(raw_lines):
        end_t = raw_lines[i + 1][0] if i + 1 < len(raw_lines) else t + 5.0
        result.append((t, end_t, text))
    return result


def _is_lyric_line(text: str) -> bool:
    lower = text.lower()
    if any(lower.startswith(p.lower()) for p in _METADATA_PREFIXES):
        return False
    if any(kw in text for kw in _METADATA_ANYWHERE):
        return False
    # 過濾純英數製作人員名單（Nick pyo / KIM HEEYOUNG 等）
    if re.match(r'^[A-Za-z0-9 @&\-_.]+$', text) and len(text) > 6:
        return False
    return True


def _netease_search_song(query: str) -> str | None:
    url = f"https://music.163.com/api/search/get/web?type=1&s={quote(query)}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=10) as resp:
            data = resp.read().decode("utf-8")
    except (HTTPError, URLError, OSError):
        return None
    body = json.loads(data)
    songs = body.get("result", {}).get("songs", [])
    if not songs:
        return None
    return str(songs[0]["id"])


def _netease_fetch_lyric(song_id: str) -> str | None:
    url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=1&tv=-1"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=10) as resp:
            data = resp.read().decode("utf-8")
    except (HTTPError, URLError, OSError):
        return None
    body = json.loads(data)
    lrc = body.get("lrc", {}).get("lyric", "")
    if not lrc:
        return None
    return lrc


def _fetch_lrc(query: str) -> list[tuple[float, float, str]]:
    local = _fetch_lrc_local(query)
    if local:
        return local

    raw = syncedlyrics.search(
        query,
        providers=["netease", "deezer", "lrclib"],
        synced_only=True,
    )
    if raw:
        return _parse_lrc_text(raw)

    print("  syncedlyrics 無結果，嘗試 NetEase API 備用...")
    time.sleep(0.5)
    song_id = _netease_search_song(query)
    if not song_id:
        print("  NetEase 搜尋不到歌曲")
        return []
    lrc = _netease_fetch_lyric(song_id)
    if not lrc:
        print("  NetEase 無歌詞")
        return []
    lines = _parse_lrc_text(lrc)
    if lines:
        print(f"  NetEase API 取得 {len(lines)} 行歌詞")
    return lines


def _ass_time_to_sec(t: str) -> float:
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _seconds_to_ass(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _is_gibberish(text: str) -> bool:
    stripped = re.sub(r"\\k\d+", "", text)
    stripped = re.sub(r"[{}]", "", stripped)
    cleaned = stripped.strip()
    if not cleaned:
        return True
    latin = sum(1 for c in cleaned if c.isascii() and c.isalpha())
    total_alpha = sum(1 for c in cleaned if c.isalpha())
    if total_alpha == 0:
        return False
    return latin / total_alpha > 0.5


def _build_k_tags(text: str, total_cs: int) -> str:
    """把 text 的每個字按 total_cs 等比分配 \\k，最後一字補餘數。"""
    chars = list(text.replace(" ", ""))
    if not chars:
        return text
    per_char = max(1, total_cs // len(chars))
    remainder = total_cs - per_char * len(chars)
    parts = []
    for i, ch in enumerate(chars):
        k = per_char + (remainder if i == len(chars) - 1 else 0)
        parts.append(f"{{\\k{k}}}{ch}")
    return "".join(parts)


def _estimate_offset(
    whisper_segs: list[tuple[float, float, str]],
    lrc_lines: list[tuple[float, float, str]],
) -> float:
    """
    用前幾句估算 Whisper 時間軸相對 LRC 的偏移（秒）。
    offset = whisper_start - lrc_start
    正值代表 Whisper 時間比 LRC 早（人聲檔頭部被裁掉）。
    取中位數避免極端值影響。
    """
    offsets = []
    for w_start, w_end, w_text in whisper_segs[:12]:
        w_chars = set(re.sub(r'\s', '', w_text))
        best_score = 0
        best_offset = None
        for l_start, l_end, l_text in lrc_lines:
            l_chars = set(re.sub(r'\s', '', l_text))
            if not l_chars:
                continue
            # Jaccard 字元相似度
            intersection = len(w_chars & l_chars)
            union = len(w_chars | l_chars)
            score = intersection / union if union > 0 else 0
            if score > best_score and score >= 0.4:
                best_score = score
                best_offset = w_start - l_start
        if best_offset is not None:
            offsets.append(best_offset)

    if not offsets:
        return 0.0

    offsets.sort()
    mid = len(offsets) // 2
    return offsets[mid]


def correct_ass(ass_path: Path, query: str) -> Path:
    lrc_lines = _fetch_lrc(query)
    if not lrc_lines:
        print("  無外部歌詞，保留 Whisper 原始輸出")
        return ass_path

    raw = ass_path.read_text(encoding="utf-8")

    # ── 解析所有 Whisper Dialogue（用於估算偏移） ──
    dialogue_pattern = re.compile(
        r"^(Dialogue: \d+,(\d+:\d+:\d+\.\d+),(\d+:\d+:\d+\.\d+),Karaoke,,0,0,0,,)(.*)$",
        re.MULTILINE,
    )
    whisper_segs = []
    for m in dialogue_pattern.finditer(raw):
        w_start = _ass_time_to_sec(m.group(2))
        w_end = _ass_time_to_sec(m.group(3))
        text_raw = re.sub(r"\{\\k\d+\}", "", m.group(4)).replace("{}", "").strip()
        if text_raw:
            whisper_segs.append((w_start, w_end, text_raw))

    # ── 估算時間偏移 ──
    offset = _estimate_offset(whisper_segs, lrc_lines)
    print(f"  偵測到 Whisper vs LRC 時間偏移：{offset:+.2f} 秒")

    # ── 重建 Events：時間用 Whisper，文字用 LRC ──
    header_end = raw.find("[Events]")
    if header_end == -1:
        print("  ⚠️  找不到 [Events] 區塊，放棄校正")
        return ass_path
    header = raw[:header_end]

    events_header = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, "
        "MarginL, MarginR, MarginV, Effect, Text\n"
    )

    new_dialogues = []
    used_lrc = set()

    for m in dialogue_pattern.finditer(raw):
        prefix = m.group(1)
        w_start = _ass_time_to_sec(m.group(2))
        w_end = _ass_time_to_sec(m.group(3))
        orig_text = m.group(4)

        # 跳過亂碼行
        if _is_gibberish(orig_text):
            continue

        # 把 Whisper 時間換算回 LRC 時間軸，找最接近的 LRC 行
        lrc_time = w_start - offset

        best_idx = None
        best_dist = float("inf")
        for i, (l_start, l_end, l_text) in enumerate(lrc_lines):
            dist = abs(lrc_time - l_start)
            # 同一行只配對一次，距離容忍 6 秒
            if dist < best_dist and dist <= 6.0 and i not in used_lrc:
                best_dist = dist
                best_idx = i

        if best_idx is None:
            # 找不到對應 LRC 行 → 保留 Whisper 原始輸出
            new_dialogues.append(m.group(0))
            continue

        used_lrc.add(best_idx)
        _, _, lrc_text = lrc_lines[best_idx]
        corrected = s2t(lrc_text, "zh-tw") if LANGUAGE == "zh" else lrc_text

        # \k 總量用 Whisper segment 時長（保留原始進歌點節奏）
        total_cs = max(1, int(round((w_end - w_start) * 100)))
        new_text = _build_k_tags(corrected, total_cs)

        new_dialogues.append(f"{prefix}{new_text}")

    ass_path.write_text(
        header + events_header + "\n".join(new_dialogues),
        encoding="utf-8",
    )
    print(f"  校正完成：{len(new_dialogues)} 行"
          f"（時間軸=Whisper，文字=LRC，偏移={offset:+.2f}s）")
    return ass_path