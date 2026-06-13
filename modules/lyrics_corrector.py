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


def _overlap(a_start, a_end, b_start, b_end) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


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


def _strip_k_tags(text: str) -> str:
    return re.sub(r"\\k\d+", "", text).replace("{}", "")


def _merge_consecutive_duplicates(lines: list[str]) -> list[str]:
    merged = []
    for line in lines:
        if not merged:
            merged.append(line)
            continue

        merge_pat = re.compile(
            r"^(Dialogue: \d+,)(\d+:\d+:\d+\.\d+),(\d+:\d+:\d+\.\d+),"
            r"Karaoke,,0,0,0,,(.*)$"
        )
        m_cur = merge_pat.match(line)
        m_prev = merge_pat.match(merged[-1])
        if not m_cur or not m_prev:
            merged.append(line)
            continue

        prev_start = _ass_time_to_sec(m_prev.group(2))
        prev_end = _ass_time_to_sec(m_prev.group(3))
        cur_start = _ass_time_to_sec(m_cur.group(2))
        cur_end = _ass_time_to_sec(m_cur.group(3))

        if abs(prev_end - cur_start) > 0.01:
            merged.append(line)
            continue

        prev_text = _strip_k_tags(m_prev.group(4))
        cur_text = _strip_k_tags(m_cur.group(4))

        if prev_text != cur_text:
            merged.append(line)
            continue

        merged_dur = cur_end - prev_start
        prev_dur = prev_end - prev_start
        scale = merged_dur / prev_dur if prev_dur > 0 else 1.0

        prev_line = m_prev.group(4)
        k_vals = [int(x) for x in re.findall(r"\\k(\d+)", prev_line)]
        if not k_vals:
            merged.append(line)
            continue

        rescaled = []
        for k in k_vals:
            rescaled.append(max(1, int(round(k * scale))))

        text = _strip_k_tags(prev_line)
        chars = list(text.replace(" ", ""))
        new_parts = []
        for i in range(len(rescaled)):
            if i < len(chars):
                new_parts.append(f"{{\\k{rescaled[i]}}}{chars[i]}")
            else:
                new_parts.append(f"{{\\k{rescaled[i]}}}{chars[-1]}")
        remaining = chars[len(rescaled):]
        if remaining:
            new_parts.append("".join(remaining))

        merged[-1] = (
            f"{m_prev.group(1)}{_seconds_to_ass(prev_start)},"
            f"{_seconds_to_ass(cur_end)},Karaoke,,0,0,0,,"
            f"{''.join(new_parts)}"
        )

    return merged


def _seconds_to_ass(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def correct_ass(ass_path: Path, query: str) -> Path:
    lrc_lines = _fetch_lrc(query)
    if not lrc_lines:
        return ass_path

    raw = ass_path.read_text(encoding="utf-8")

    dialogue_pattern = re.compile(
        r"^(Dialogue: \d+,(\d+:\d+:\d+\.\d+),(\d+:\d+:\d+\.\d+),Karaoke,,0,0,0,,)(.*)$",
        re.MULTILINE,
    )

    def _replace_text(m: re.Match) -> str:
        prefix = m.group(1)
        seg_start = _ass_time_to_sec(m.group(2))
        seg_end = _ass_time_to_sec(m.group(3))
        orig_text = m.group(4)

        best_line = None
        best_overlap = 0.0
        for lrc_start, lrc_end, text in lrc_lines:
            ov = _overlap(seg_start, seg_end, lrc_start, lrc_end)
            if ov > best_overlap:
                best_overlap = ov
                best_line = text

        if best_line is None or best_overlap <= 0:
            return m.group(0)

        corrected = s2t(best_line, "zh-tw") if LANGUAGE == "zh" else best_line
        orig_k_durations = re.findall(r"\\k(\d+)", orig_text)
        if not orig_k_durations:
            return m.group(0)

        k_vals = [int(d) for d in orig_k_durations]
        total_cs = sum(k_vals)
        chars = list(corrected.replace(" ", ""))

        if not chars:
            return m.group(0)

        new_parts = []
        if len(chars) >= len(k_vals):
            for i in range(len(k_vals)):
                new_parts.append(f"{{\\k{k_vals[i]}}}{chars[i]}")
            remaining = chars[len(k_vals):]
            if remaining:
                new_parts.append("".join(remaining))
        else:
            extra_cs = max(1, total_cs // len(chars))
            for ch in chars:
                new_parts.append(f"{{\\k{extra_cs}}}{ch}")

        return prefix + "".join(new_parts)

    corrected = dialogue_pattern.sub(_replace_text, raw)

    filtered_lines = []
    for line in corrected.splitlines():
        m = dialogue_pattern.match(line)
        if m and _is_gibberish(m.group(4)):
            continue
        filtered_lines.append(line)

    merged = _merge_consecutive_duplicates(filtered_lines)

    ass_path.write_text("\n".join(merged), encoding="utf-8")
    merged_count = len(filtered_lines) - len(merged)
    print(f"  校正完成：{len(merged)} 行（合併 {merged_count} 組重複，\k 時間已等比縮放）")
    return ass_path
