import json
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import quote

import syncedlyrics
from rapidfuzz import fuzz as _fuzz
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
    "詞曲", "曲詞", "詞：", "曲：",
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


def _similarity(text_a: str, text_b: str) -> float:
    return _fuzz.token_sort_ratio(text_a, text_b)


def _jaccard(text_a: str, text_b: str) -> float:
    chars_a = set(re.sub(r'\s', '', text_a))
    chars_b = set(re.sub(r'\s', '', text_b))
    if not chars_a or not chars_b:
        return 0.0
    return len(chars_a & chars_b) / len(chars_a | chars_b)


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
        best_score = 0
        best_offset = None
        for l_start, l_end, l_text in lrc_lines:
            score = _jaccard(w_text, l_text)
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


def _validate_lrc(
    whisper_segs: list[tuple[float, float, str]],
    lrc_lines: list[tuple[float, float, str]],
    threshold: float = 0.3,
) -> bool:
    """用前 12 句的 Jaccard 中位數判斷 LRC 是否與音訊為同一版本。"""
    scores = []
    for w_start, w_end, w_text in whisper_segs[:12]:
        best = 0.0
        for l_start, l_end, l_text in lrc_lines:
            s = _jaccard(w_text, l_text)
            if s > best:
                best = s
        scores.append(best)
    if not scores:
        return False
    scores.sort()
    median = scores[len(scores) // 2]
    return median >= threshold


def _seconds_to_lrc(sec: float) -> str:
    m = int(sec // 60)
    s = sec % 60
    return f"{m:02d}:{s:05.2f}"


def _save_raw_lyrics(text: str, suffix: str):
    path = ROOT_DIR / "output" / "subtitles" / f"lyrics_fetched{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"  原始歌詞已儲存：{path}")


def _fetch_plain_text() -> list[str] | None:
    """從 project_root/lyrics_plain.txt 讀取無時間軸歌詞。"""
    path = ROOT_DIR / "lyrics_plain.txt"
    if not path.exists():
        return None
    lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    lines = [l for l in lines if _is_lyric_line(l)]
    return lines if lines else None


def _correct_with_plain_text(
    ass_path: Path,
    lines_text: list[str],
    whisper_segs: list[tuple[float, float, str]],
) -> Path:
    """用純文字歌詞（無時間戳）逐行配對 Whisper segment 進行校正。"""
    raw = ass_path.read_text(encoding="utf-8")
    header_end = raw.find("[Events]")
    if header_end == -1:
        return ass_path
    header = raw[:header_end]
    events_header = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, "
        "MarginL, MarginR, MarginV, Effect, Text\n"
    )

    dialogue_pattern = re.compile(
        r"^(Dialogue: \d+,(\d+:\d+:\d+\.\d+),(\d+:\d+:\d+\.\d+),Karaoke,,0,0,0,,)(.*)$",
        re.MULTILINE,
    )

    matches = list(dialogue_pattern.finditer(raw))
    new_dialogues = []
    current_pos = 0
    seg_idx = 0

    while seg_idx < len(matches):
        m = matches[seg_idx]
        orig_text = m.group(4)

        if _is_gibberish(orig_text):
            new_dialogues.append(m.group(0))
            seg_idx += 1
            continue

        w_clean = re.sub(r"\{\\k\d+\}", "", orig_text).replace("{}", "").strip()
        if not w_clean or not _is_lyric_line(w_clean):
            seg_idx += 1
            continue

        # 找最佳匹配的 plain text line
        best_idx = None
        best_score = 0
        for i in range(current_pos, min(len(lines_text), current_pos + 8)):
            score = _similarity(w_clean, lines_text[i])
            if score > best_score:
                best_score = score
                best_idx = i

        if best_idx is None or best_score < 50:
            # 無匹配 → 嘗試用 chunk 合併看能否拼成一行
            if current_pos < len(lines_text):
                target = lines_text[current_pos]
                merged_buf = [seg_idx]
                prev_end = _ass_time_to_sec(m.group(3))
                matched = False
                for nxt in range(seg_idx + 1, min(seg_idx + 4, len(matches))):
                    nxt_start = _ass_time_to_sec(matches[nxt].group(2))
                    if nxt_start - prev_end > 5.0:
                        break
                    t = re.sub(r"\{\\k\d+\}", "", matches[nxt].group(4)).replace("{}", "").strip()
                    if not t or _is_gibberish(matches[nxt].group(4)) or not _is_lyric_line(t):
                        break
                    merged_buf.append(nxt)
                    merged_parts = []
                    for j in merged_buf:
                        tj = re.sub(r"\{\\k\d+\}", "", matches[j].group(4)).replace("{}", "").strip()
                        if tj:
                            merged_parts.append(tj)
                    m_clean = " ".join(merged_parts)
                    m_score = _similarity(m_clean, target)
                    w_chunks = len([c for c in m_clean.split() if c])
                    t_chunks = len([c for c in target.split() if c])
                    prev_end = _ass_time_to_sec(matches[nxt].group(3))
                    if m_score >= 60 and w_chunks >= t_chunks:
                        # 合併成功
                        corrected = s2t(target, "zh-tw") if LANGUAGE == "zh" else target
                        all_k = []
                        w_start = _ass_time_to_sec(matches[merged_buf[0]].group(2))
                        w_end = _ass_time_to_sec(matches[merged_buf[-1]].group(3))
                        for j in merged_buf:
                            all_k.extend(int(d) for d in re.findall(r"\\k(\d+)", matches[j].group(4)))
                        k_vals = all_k
                        if k_vals:
                            chars = list(corrected.replace(" ", ""))
                            if chars:
                                prefix = f"Dialogue: 0,{_seconds_to_ass(w_start)},{_seconds_to_ass(w_end)},Karaoke,,0,0,0,,"
                                new_parts = []
                                if len(chars) >= len(k_vals):
                                    for i in range(len(k_vals)):
                                        new_parts.append(f"{{\\k{k_vals[i]}}}{chars[i]}")
                                    remaining = chars[len(k_vals):]
                                    if remaining:
                                        new_parts.append("".join(remaining))
                                else:
                                    total_cs = max(1, int(round((w_end - w_start) * 100)))
                                    extra_cs = max(1, total_cs // len(chars))
                                    for ch in chars:
                                        new_parts.append(f"{{\\k{extra_cs}}}{ch}")
                                new_text = "".join(new_parts)
                                new_dialogues.append(f"{prefix}{new_text}")
                                seg_idx += len(merged_buf)
                                current_pos += 1
                                matched = True
                                break
                if not matched:
                    new_dialogues.append(m.group(0))
                    seg_idx += 1
            else:
                new_dialogues.append(m.group(0))
                seg_idx += 1
            continue

        # 有匹配行：依 chunk 數決定是否合併
        target_line = lines_text[best_idx]
        w_chunks = len([c for c in w_clean.split() if c])
        t_chunks = len([c for c in target_line.split() if c])

        if w_chunks >= t_chunks:
            # 1:1 匹配
            corrected = s2t(target_line, "zh-tw") if LANGUAGE == "zh" else target_line
            k_vals = [int(d) for d in re.findall(r"\\k(\d+)", orig_text)]
            if not k_vals:
                new_dialogues.append(m.group(0))
                seg_idx += 1
                continue
            chars = list(corrected.replace(" ", ""))
            if not chars:
                new_dialogues.append(m.group(0))
                seg_idx += 1
                continue
            w_end = _ass_time_to_sec(m.group(3))
            w_start = _ass_time_to_sec(m.group(2))
            new_parts = []
            if len(chars) >= len(k_vals):
                for i in range(len(k_vals)):
                    new_parts.append(f"{{\\k{k_vals[i]}}}{chars[i]}")
                remaining = chars[len(k_vals):]
                if remaining:
                    new_parts.append("".join(remaining))
            else:
                total_cs = max(1, int(round((w_end - w_start) * 100)))
                extra_cs = max(1, total_cs // len(chars))
                for ch in chars:
                    new_parts.append(f"{{\\k{extra_cs}}}{ch}")
            new_text = "".join(new_parts)
            new_dialogues.append(f"{m.group(1)}{new_text}")
            current_pos = best_idx + 1
            seg_idx += 1
        else:
            # 行中斷 → 往後合併，需同時滿足：時間連續 + 相似度達標 + chunk 數足夠
            merged_buf = [seg_idx]
            matched = False
            prev_end = _ass_time_to_sec(m.group(3))
            for nxt in range(seg_idx + 1, min(seg_idx + 4, len(matches))):
                nxt_start = _ass_time_to_sec(matches[nxt].group(2))
                if nxt_start - prev_end > 5.0:
                    break  # 時間不連續，不該合併
                t = re.sub(r"\{\\k\d+\}", "", matches[nxt].group(4)).replace("{}", "").strip()
                if not t or _is_gibberish(matches[nxt].group(4)) or not _is_lyric_line(t):
                    break
                merged_buf.append(nxt)
                m_parts = []
                for j in merged_buf:
                    tj = re.sub(r"\{\\k\d+\}", "", matches[j].group(4)).replace("{}", "").strip()
                    if tj:
                        m_parts.append(tj)
                m_clean = " ".join(m_parts)
                m_chunks = len([c for c in m_clean.split() if c])
                m_score = _similarity(m_clean, target_line)
                prev_end = _ass_time_to_sec(matches[nxt].group(3))
                if m_chunks >= t_chunks and m_score >= 60:
                    corrected = s2t(target_line, "zh-tw") if LANGUAGE == "zh" else target_line
                    all_k = []
                    w_start = _ass_time_to_sec(matches[merged_buf[0]].group(2))
                    w_end = _ass_time_to_sec(matches[merged_buf[-1]].group(3))
                    for j in merged_buf:
                        all_k.extend(int(d) for d in re.findall(r"\\k(\d+)", matches[j].group(4)))
                    k_vals = all_k
                    if k_vals:
                        chars = list(corrected.replace(" ", ""))
                        if chars:
                            new_prefix = f"Dialogue: 0,{_seconds_to_ass(w_start)},{_seconds_to_ass(w_end)},Karaoke,,0,0,0,,"
                            new_parts = []
                            if len(chars) >= len(k_vals):
                                for i in range(len(k_vals)):
                                    new_parts.append(f"{{\\k{k_vals[i]}}}{chars[i]}")
                                remaining = chars[len(k_vals):]
                                if remaining:
                                    new_parts.append("".join(remaining))
                            else:
                                total_cs = max(1, int(round((w_end - w_start) * 100)))
                                extra_cs = max(1, total_cs // len(chars))
                                for ch in chars:
                                    new_parts.append(f"{{\\k{extra_cs}}}{ch}")
                            new_text = "".join(new_parts)
                            new_dialogues.append(f"{new_prefix}{new_text}")
                            seg_idx += len(merged_buf)
                            current_pos = best_idx + 1
                            matched = True
                            break
                prev_end = _ass_time_to_sec(matches[nxt].group(3))
            if not matched:
                new_dialogues.append(m.group(0))
                seg_idx += 1

    ass_path.write_text(header + events_header + "\n".join(new_dialogues), encoding="utf-8")
    print(f"  純文字校正完成：{len(new_dialogues)} 行")
    return ass_path


def correct_ass(ass_path: Path, query: str) -> Path:
    raw = ass_path.read_text(encoding="utf-8")

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

    if not whisper_segs:
        print("  ⚠️  無有效 Whisper 段落，放棄校正")
        return ass_path

    # ── 手動歌詞優先（lyrics_plain.txt）──
    plain_lines = _fetch_plain_text()
    if plain_lines:
        print(f"  偵測到 lyrics_plain.txt，以手動歌詞為主（{len(plain_lines)} 行）")
        _save_raw_lyrics("\n".join(plain_lines), ".txt")
        return _correct_with_plain_text(ass_path, plain_lines, whisper_segs)

    # ── 嘗試取得 LRC ──
    lrc_lines = _fetch_lrc(query)
    if lrc_lines and not _validate_lrc(whisper_segs, lrc_lines):
        print("  LRC 版本與音訊不符（Jaccard < 0.3），捨棄")
        lrc_lines = []

    # ── LRC 有效 → 走時間軸校正 ──
    if lrc_lines:
        _save_raw_lyrics(
            "\n".join(f"[{_seconds_to_lrc(t)}]{txt}" for t, _, txt in lrc_lines),
            ".lrc",
        )
        offset = _estimate_offset(whisper_segs, lrc_lines)
        print(f"  偵測到 Whisper vs LRC 時間偏移：{offset:+.2f} 秒")

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

        # Phase 1：每個 Whisper segment 找最近的 LRC 行（允許重複）
        all_matches = list(dialogue_pattern.finditer(raw))
        seg_groups = []  # [(lrc_idx, lrc_text, [(m, orig_text, w_start, w_end), ...])]
        temp_group = None

        for m in all_matches:
            w_start = _ass_time_to_sec(m.group(2))
            w_end = _ass_time_to_sec(m.group(3))
            orig_text = m.group(4)

            if _is_gibberish(orig_text):
                continue

            lrc_time = w_start - offset
            best_idx = None
            best_dist = float("inf")
            for i, (l_start, l_end, l_text) in enumerate(lrc_lines):
                dist = abs(lrc_time - l_start)
                if dist < best_dist and dist <= 6.0:
                    best_dist = dist
                    best_idx = i

            if best_idx is None:
                seg_groups.append((None, None, [(m, orig_text, w_start, w_end)]))
                continue

            _, _, lrc_text = lrc_lines[best_idx]
            if temp_group is not None and temp_group[0] == best_idx:
                temp_group[2].append((m, orig_text, w_start, w_end))
            else:
                if temp_group is not None:
                    seg_groups.append(temp_group)
                temp_group = (best_idx, lrc_text, [(m, orig_text, w_start, w_end)])

        if temp_group is not None:
            seg_groups.append(temp_group)

        # Phase 2：逐組處理，若同 LRC 連續多個 segment 則拆分文字
        new_dialogues = []
        for lrc_idx, lrc_text, segs in seg_groups:
            if lrc_idx is None or len(segs) == 1:
                # 無匹配或只有 1 個 → 直接套用原邏輯
                for m, orig_text, w_start, w_end in segs:
                    if lrc_idx is None:
                        new_dialogues.append(m.group(0))
                        continue
                    corrected = s2t(lrc_text, "zh-tw") if LANGUAGE == "zh" else lrc_text
                    prefix = m.group(1)
                    k_vals = [int(d) for d in re.findall(r"\\k(\d+)", orig_text)]
                    if not k_vals:
                        new_dialogues.append(m.group(0))
                        continue
                    chars = list(corrected.replace(" ", ""))
                    if not chars:
                        new_dialogues.append(m.group(0))
                        continue
                    new_parts = []
                    if len(chars) >= len(k_vals):
                        for i in range(len(k_vals)):
                            new_parts.append(f"{{\\k{k_vals[i]}}}{chars[i]}")
                        remaining = chars[len(k_vals):]
                        if remaining:
                            new_parts.append("".join(remaining))
                    else:
                        total_cs = max(1, int(round((w_end - w_start) * 100)))
                        extra_cs = max(1, total_cs // len(chars))
                        for ch in chars:
                            new_parts.append(f"{{\\k{extra_cs}}}{ch}")
                    new_dialogues.append(f"{prefix}{''.join(new_parts)}")
            else:
                # 多個連續 segment 共用同一 LRC 行 → 按時長比例拆分文字
                is_zh = LANGUAGE == "zh"
                full_text = s2t(lrc_text, "zh-tw") if is_zh else lrc_text
                total_dur = sum(we - ws for _, _, ws, we in segs)
                char_pos = 0
                all_chars = list(full_text.replace(" ", ""))
                for idx_in_group, (m, orig_text, w_start, w_end) in enumerate(segs):
                    prefix = m.group(1)
                    seg_dur = w_end - w_start
                    if idx_in_group == len(segs) - 1:
                        seg_chars = all_chars[char_pos:]
                    else:
                        ratio = seg_dur / total_dur if total_dur > 0 else 1.0 / len(segs)
                        n_chars = max(1, int(round(len(all_chars) * ratio)))
                        remaining = len(all_chars) - char_pos - n_chars
                        n_others = len(segs) - idx_in_group - 1
                        if remaining < n_others:
                            n_chars = len(all_chars) - char_pos - n_others
                        seg_chars = all_chars[char_pos:char_pos + n_chars]
                        char_pos += n_chars

                    k_vals = [int(d) for d in re.findall(r"\\k(\d+)", orig_text)]
                    if not k_vals:
                        new_dialogues.append(m.group(0))
                        continue
                    new_parts = []
                    if len(seg_chars) >= len(k_vals):
                        for i in range(len(k_vals)):
                            new_parts.append(f"{{\\k{k_vals[i]}}}{seg_chars[i]}")
                        r = seg_chars[len(k_vals):]
                        if r:
                            new_parts.append("".join(r))
                    else:
                        total_cs = max(1, int(round(seg_dur * 100)))
                        extra_cs = max(1, total_cs // len(seg_chars))
                        for ch in seg_chars:
                            new_parts.append(f"{{\\k{extra_cs}}}{ch}")
                    new_dialogues.append(f"{prefix}{''.join(new_parts)}")

        ass_path.write_text(
            header + events_header + "\n".join(new_dialogues),
            encoding="utf-8",
        )
        print(f"  校正完成：{len(new_dialogues)} 行（LRC，偏移={offset:+.2f}s）")
        return ass_path

    print("  無外部歌詞，保留 Whisper 原始輸出")
    return ass_path