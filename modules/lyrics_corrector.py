"""
lyrics_corrector.py
--------------------
以 Whisper 輸出的 ASS 時間軸與斷句為主，
將外部取得的正確歌詞文字填入對應的 \\k slot。

使用方式：
    python lyrics_corrector.py \
        --ass   song_whisper_raw.ass \
        --lyrics lyrics_fetched.txt \
        --output song_corrected.ass

演算法概述：
    1. 解析 ASS → 取出每行純文字（去除 \\k tag）
    2. 全局搜尋 + 副歌重複支援：對每個 ASS 行找最佳歌詞行
    3. 字元 slot 保留 \\k 時間，填入正確文字
    4. 字數不符時按比例重新分配，標記 [?] 待人工確認
    5. 重寫 ASS，時間軸完全不動
"""

import re
import sys
import time
import argparse
import unicodedata
from pathlib import Path
from copy import deepcopy
from rapidfuzz import fuzz
from config import ROOT_DIR

# 網路歌詞抓取（requests / bs4 為 optional；抓不到時 graceful fallback）
try:
    import requests
    from bs4 import BeautifulSoup
    _NET_OK = True
except ImportError:
    _NET_OK = False


# ---------------------------------------------------------------------------
# 1. ASS 解析
# ---------------------------------------------------------------------------

KTAG_RE = re.compile(r'\{([^}]*)\}')
KDU_RE  = re.compile(r'\\k(\d+)')


def parse_ass(path: str):
    with open(path, encoding='utf-8-sig') as f:
        raw = f.readlines()

    header_lines = []
    dialogues    = []
    footer_lines = []
    in_events    = False
    format_seen  = False

    for line in raw:
        stripped = line.rstrip('\n')
        if stripped.strip().startswith('[Events]'):
            in_events = True
            header_lines.append(stripped)
            continue
        if in_events and stripped.strip().startswith('Format:'):
            format_seen = True
            header_lines.append(stripped)
            continue
        if in_events and format_seen and stripped.strip().startswith('Dialogue:'):
            dialogues.append(_parse_dialogue(stripped))
            continue
        if in_events:
            footer_lines.append(stripped)
        else:
            header_lines.append(stripped)

    return header_lines, dialogues, footer_lines


def _parse_dialogue(line: str) -> dict:
    parts  = line.split(',', 9)
    prefix = ','.join(parts[:9]) + ','
    text   = parts[9] if len(parts) > 9 else ''
    slots, remainder = _parse_kslots(text)
    return {
        'raw'      : line,
        'prefix'   : prefix,
        'slots'    : slots,
        'remainder': remainder,
    }


def _parse_kslots(text: str):
    """
    把 Text 欄位解析成 (duration_cs, char) slot 列表。

    關鍵問題：Whisper 會把複音節詞放在同一個 \\k 後面，例如：
        {\\k68}是我  → 兩個字共用同一個 duration
        {\\k94}是不是 → 三個字共用同一個 duration
        {\\k6} 懸    → 空格 + 懸 共用

    處理策略：
        - 遇到多個字元共用一個 \\k 時，把 duration 均分給每個字元
        - 空格視為停頓，獨立佔用一個 slot（但後面的字元繼承前一個 \\k）
        - 非 \\k 的 override tag 收進 remainder
    """
    remainder = ''
    slots     = []

    tokens = re.split(r'(\{[^}]*\})', text)
    pending_dur = None

    for tok in tokens:
        if tok.startswith('{') and tok.endswith('}'):
            inner   = tok[1:-1]
            k_match = KDU_RE.search(inner)
            if k_match:
                if pending_dur is not None:
                    # 上一個 \\k 沒有對應字元
                    slots.append((pending_dur, ''))
                pending_dur = int(k_match.group(1))
            else:
                remainder += tok
        else:
            # 純文字片段，可能多字元（如「是我」「是不是」）
            if not tok:
                continue
            chars = list(tok)  # 含空格
            if pending_dur is not None:
                # 把 pending_dur 均分給這些字元
                n      = len(chars)
                base   = pending_dur // n
                extra  = pending_dur - base * n
                for i, ch in enumerate(chars):
                    dur = base + (1 if i < extra else 0)
                    slots.append((dur, ch))
                pending_dur = None
            else:
                # 沒有 pending \\k 的文字（少見的前綴文字）
                for ch in chars:
                    slots.append((0, ch))

    if pending_dur is not None:
        slots.append((pending_dur, ''))

    return slots, remainder


def slots_to_text(slots) -> str:
    return ''.join(ch for _, ch in slots if ch)


def slots_to_ass_text(slots, remainder='') -> str:
    """
    重建 ASS text 欄位。
    相鄰且 dur 相近（均分殘留）的字元合回同一個 \\k 比較理想，
    但為了簡單可靠，每個字元各自輸出一個 \\k tag 亦完全合法。
    """
    parts = [remainder] if remainder else []
    for dur, ch in slots:
        parts.append(f'{{\\k{dur}}}{ch}')
    return ''.join(parts)


# ---------------------------------------------------------------------------
# 2. 歌詞載入
# ---------------------------------------------------------------------------

def load_lyrics(path: str) -> list:
    """
    載入歌詞檔案，每行即一個歌詞片段，原樣保留（含句內空格）。
    不做自動拆行——手動整理的歌詞每行已是正確粒度。
    網路歌詞的拆行由 _split_lyric_lines() 在另一路徑處理。
    """
    with open(path, encoding='utf-8') as f:
        raw_lines = [l.strip() for l in f.readlines()]
    return [l for l in raw_lines if l]

def normalize(text: str) -> str:
    """正規化用於相似度比對：全形→半形、去標點空格"""
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'[\s　，。！？、：；「」『』【】〔〕…—～·\-_]', '', text)
    return text


# ---------------------------------------------------------------------------
# 3-0. 非歌詞行過濾
# ---------------------------------------------------------------------------

_SIMPLIFIED_CHARS = set('请订转赏爱们说话这样来过还没对让')

def _cjk_ratio(text: str) -> float:
    if not text: return 0.0
    return sum(1 for c in text if '\u4e00' <= c <= '\u9fff') / len(text)

def _simplified_ratio(text: str) -> float:
    cjk = [c for c in text if '\u4e00' <= c <= '\u9fff']
    if not cjk: return 0.0
    return sum(1 for c in cjk if c in _SIMPLIFIED_CHARS) / len(cjk)

_GARBAGE_KEYWORDS = [
    '詞曲', '作詞', '作曲', '編曲', '製作', '出版', '發行',
    '版權', 'copyright', '©', '℗',
    '訂閱', '点赞', '转发', '打赏', '明镜', '点点栏目', 'subscribe',
]

def _is_garbage_line(text: str) -> bool:
    norm = normalize(text)
    if len(norm) <= 1:
        return True
    low = text.lower()
    for kw in _GARBAGE_KEYWORDS:
        if kw.lower() in low:
            return True
    if re.search(r'https?://|www\\.|@', text):
        return True
    if _simplified_ratio(text) > 0.30:
        return True
    if len(norm) > 5 and _cjk_ratio(text) < 0.50:
        return True
    return False



# ---------------------------------------------------------------------------
# 3. 行級對齊
# ---------------------------------------------------------------------------

def _hybrid_score(a: str, b: str) -> float:
    """
    混合分數 = content 相似度 × 長度懲罰

    content:
      - partial_ratio（7 成）：在長字串中找短字串的最佳子視窗
      - token_set_ratio（3 成）：不分順序比對共用字元集
    長度懲罰： min(len1, len2) / max(len1, len2)
    避免 2 字的 ASS 行靠子字串匹配到 20 字的歌詞行。
    """
    pr = fuzz.partial_ratio(a, b) / 100.0
    ts = fuzz.token_set_ratio(a, b) / 100.0
    content = 0.7 * pr + 0.3 * ts
    len_ratio = min(len(a), len(b)) / max(len(a), len(b))
    return content * len_ratio


def align_ass_to_lyrics(
    dialogues    : list,
    lyric_lines  : list,
    sim_threshold: float = 0.45,
    verbose      : bool  = False,
) -> list:
    """
    對每個 Dialogue 行找最佳對齊歌詞。

    策略：
      A. 比例錨點（Proportional Anchor）
         依 ASS 行索引在歌詞中推算「期望位置」，搜尋視窗以此為中心。
         解決 ASS 行數 ≠ 歌詞行數導致後段偏移的問題。

      B. 局部搜尋 → 若信心不足 → 全局搜尋（支援副歌重複）

      C. 每個位置同時嘗試：
         - 單行
         - 連續兩行合併（Whisper 有時把兩句壓成一行）
         - 歌詞行的後半段（歌詞一行 Whisper 拆兩行的情況）
    """
    norm_lyrics   = [normalize(l) for l in lyric_lines]
    n_lyric       = len(norm_lyrics)
    result        = []
    lyric_ptr     = 0

    # 比例錨點：只計算非垃圾行
    valid_indices = [i for i, d in enumerate(dialogues)
                     if not _is_garbage_line(slots_to_text(d['slots']))]
    n_valid    = max(len(valid_indices), 1)
    valid_rank = {idx: rank for rank, idx in enumerate(valid_indices)}

    for ass_idx, dia in enumerate(dialogues):
        ass_text = slots_to_text(dia['slots'])
        norm_ass = normalize(ass_text)

        # 垃圾行（廣告/版權/空行）：跳過校正，原文保留
        if not norm_ass or _is_garbage_line(ass_text):
            d = deepcopy(dia)
            d.update({'lyric_match': ass_text, 'confidence': 1.0,
                      'needs_review': False, 'was_garbage': True})
            result.append(d)
            if verbose and ass_text.strip():
                print(f'  [SKIP] {ass_text!r:35s}  （非歌詞行）')
            continue

        # 比例錨點：依非垃圾行排名算期望位置
        rank        = valid_rank.get(ass_idx, n_valid // 2)
        ratio       = rank / max(n_valid - 1, 1)
        anchor      = int(ratio * (n_lyric - 1))
        anchor      = max(anchor, lyric_ptr)
        window_half = max(6, n_lyric // 6)

        best = {'score': -1, 'idx': anchor, 'text': ''}

        def try_range(start, end):
            for li in range(max(0, start), min(end, n_lyric)):
                # 單行
                s1 = _hybrid_score(norm_ass, norm_lyrics[li])
                if s1 > best['score']:
                    best.update({'score': s1, 'idx': li, 'text': lyric_lines[li]})

                # 雙行合併（Whisper 斷句較細時）
                if li + 1 < n_lyric:
                    merged = norm_lyrics[li] + norm_lyrics[li + 1]
                    s2 = _hybrid_score(norm_ass, merged)
                    if s2 > best['score']:
                        best.update({'score': s2, 'idx': li,
                                     'text': lyric_lines[li] + lyric_lines[li + 1]})

                # 後半段切片（歌詞一行 Whisper 拆成兩行時）
                half = len(norm_lyrics[li]) // 2
                if half >= 2:
                    back_half = norm_lyrics[li][half:]
                    s3 = _hybrid_score(norm_ass, back_half)
                    if s3 > best['score']:
                        best.update({'score': s3, 'idx': li,
                                     'text': lyric_lines[li][half:]})

        # 第一優先：以錨點為中心的視窗
        try_range(anchor - window_half, anchor + window_half + 1)

        # 信心不足 → 全局搜尋（副歌重複）
        if best['score'] < sim_threshold:
            try_range(lyric_ptr, n_lyric)

        # 更新下限指標（只前進不後退）
        if best['score'] >= sim_threshold:
            lyric_ptr = best['idx'] + 1

        needs_review = best['score'] < sim_threshold

        if verbose:
            tag = '[?]' if needs_review else '   '
            print(f"{tag} [{best['score']:.2f}]  ASS: {ass_text!r:30s}  →  LYRIC: {best['text']!r}")

        d = deepcopy(dia)
        d.update({
            'lyric_match' : best['text'],
            'confidence'  : best['score'],
            'needs_review': needs_review,
        })
        result.append(d)

    return result


# ---------------------------------------------------------------------------
# 4. 字元填充
# ---------------------------------------------------------------------------

def _lcs_align(orig_chars: list, correct_chars: list) -> list:
    """
    用 LCS（最長公共子序列）把 orig_chars 的時間對應到 correct_chars。

    參數：
        orig_chars    : list of (duration_cs: int, char: str)
        correct_chars : list of str

    回傳 list of (duration_cs, char)，長度 = len(correct_chars)。

    演算法：
      1. SequenceMatcher 找出兩個字串的 matching blocks（公共子序列）
      2. 公共字元直接繼承 orig 的時間
      3. 非公共的 correct 字元（新增字）：
         均分「orig 中未被命中的時間」給這些字元
      4. 若 correct 比 orig 長，額外字元各得 1cs（最小合法值）
    """
    import difflib

    orig_text    = ''.join(ch for _, ch in orig_chars)
    correct_text = ''.join(correct_chars)
    orig_durs    = [dur for dur, _ in orig_chars]

    sm     = difflib.SequenceMatcher(None, orig_text, correct_text, autojunk=False)
    blocks = sm.get_matching_blocks()   # list of Match(a, b, size)

    # 建立映射：correct 位置 → orig 位置
    correct_to_orig = {}
    orig_used       = set()
    for blk in blocks:
        for k in range(blk.size):
            ci = blk.b + k
            oi = blk.a + k
            if 0 <= ci < len(correct_chars) and 0 <= oi < len(orig_chars):
                correct_to_orig[ci] = oi
                orig_used.add(oi)

    # 未被命中的 orig 時間（分給新增字元用）
    spare_dur = sum(orig_durs[oi] for oi in range(len(orig_chars)) if oi not in orig_used)
    gaps      = [ci for ci in range(len(correct_chars)) if ci not in correct_to_orig]
    per_gap   = (spare_dur // len(gaps)) if gaps else 0
    extra     = spare_dur - per_gap * len(gaps) if gaps else 0

    result    = [None] * len(correct_chars)
    gap_k     = 0

    # Step 1：LCS 命中字元繼承時間
    for ci, oi in correct_to_orig.items():
        result[ci] = (orig_durs[oi], correct_chars[ci])

    # Step 2：非命中字元均分剩餘時間
    for ci in gaps:
        dur        = per_gap + (1 if gap_k < extra else 0)
        result[ci] = (max(1, dur), correct_chars[ci])
        gap_k     += 1

    return result


def fill_slots(dia: dict) -> dict:
    """
    把 lyric_match 的正確文字填回 \\k slot，時間優先保留。

    Case A（字數相同）: 1:1 替換，時間完全不動。
    Case B（字數不同）: LCS 對齊 — 保留公共字元的時間，剩餘時間均分給新增字元。
                       時間分配比「等分全部」更精準，\'needs_review\' 仍標記。
    """
    d           = deepcopy(dia)
    correct_raw = d.get('lyric_match', '')
    correct     = normalize(correct_raw)

    if not correct:
        return d

    # 過濾空格 slot，只處理實際文字字元
    orig_slots = d['slots']
    nonsp_idx  = [i for i, (dur, ch) in enumerate(orig_slots) if ch and ch.strip()]
    orig_chars = [(orig_slots[i][0], orig_slots[i][1]) for i in nonsp_idx]  # (dur, ch)

    if len(orig_chars) == len(correct):
        # Case A：字數相同，1:1 替換，時間完全不動
        new_slots = list(orig_slots)
        for slot_i, new_ch in zip(nonsp_idx, correct):
            dur = new_slots[slot_i][0]
            new_slots[slot_i] = (dur, new_ch)
        d['slots']        = new_slots
        d['was_remapped'] = False

    else:
        # Case B：字數不同，LCS 時間對齊
        correct_chars = list(correct)   # list of str
        aligned       = _lcs_align(orig_chars, correct_chars)
        # aligned = [(dur, ch), ...]，長度 = len(correct)
        new_slots = [(dur, ch) for dur, ch in aligned]
        d['slots']        = new_slots
        d['was_remapped'] = True
        d['needs_review'] = True

    return d


# ---------------------------------------------------------------------------
# 5. ASS 重建
# ---------------------------------------------------------------------------

def rebuild_dialogue(dia: dict) -> str:
    text = slots_to_ass_text(dia['slots'], dia.get('remainder', ''))
    if dia.get('was_garbage'):
        return dia['prefix'] + text   # 廣告/版權行原樣輸出，不加標記
    if dia.get('needs_review'):
        orig_text = slots_to_text(dia['slots'])
        comment   = (f"Comment: 0,0:00:00.00,0:00:00.00,Karaoke,,0,0,0,,"
                     f"[REVIEW] conf={dia.get('confidence',0):.2f} {orig_text}")
        return comment + '\n' + dia['prefix'] + text
    return dia['prefix'] + text


def write_ass(path: str, header_lines, dialogues, footer_lines):
    with open(path, 'w', encoding='utf-8') as f:
        for line in header_lines:
            f.write(line + '\n')
        for dia in dialogues:
            f.write(rebuild_dialogue(dia) + '\n')
        for line in footer_lines:
            f.write(line + '\n')


# ---------------------------------------------------------------------------
# 6. 主流程
# ---------------------------------------------------------------------------

def correct_lyrics(
    ass_path    : str,
    lyrics_path : str,
    output_path : str,
    threshold   : float = 0.45,
    verbose     : bool  = False,
):
    print(f'[1/4] 解析 ASS：{ass_path}')
    header, dialogues, footer = parse_ass(ass_path)

    print(f'[2/4] 載入歌詞：{lyrics_path}')
    lyric_lines = load_lyrics(lyrics_path)

    print(f'[3/4] 行級對齊（threshold={threshold}）...')
    aligned = align_ass_to_lyrics(dialogues, lyric_lines, threshold, verbose)

    print(f'[4/4] 字元填充 + 輸出：{output_path}')
    filled = [fill_slots(d) for d in aligned]

    write_ass(output_path, header, filled, footer)

    if verbose:
        for i, d in enumerate(filled):
            if d.get('was_garbage'):
                print(f'  [SKIP] 行{i:2d}: 非歌詞行保留  "{slots_to_text(d["slots"])[:25]}"')
            elif d.get('was_remapped'):
                n_s = len(d['slots'])
                n_l = len(normalize(d.get('lyric_match', '')))
                print(f'  ⚠️ 行{i:2d}: 字數 {n_s}→{n_l}  '
                      f'(conf={d["confidence"]:.2f})  "{d.get("lyric_match", "")[:25]}"')

    garbage  = sum(1 for d in filled if d.get('was_garbage'))
    real     = len(filled) - garbage
    ok       = sum(1 for d in filled if not d.get('needs_review') and not d.get('was_garbage'))
    review   = sum(1 for d in filled if d.get('needs_review'))
    remapped = sum(1 for d in filled if d.get('was_remapped'))
    print(f'\n完成！共 {real} 行歌詞（跳過 {garbage} 非歌詞行）：'
          f'✅ {ok} 行自動校正，⚠️  {review} 行需確認（其中 {remapped} 行字數重新分配）')
    print(f'輸出：{output_path}')


# ---------------------------------------------------------------------------
# 7. 網路歌詞抓取
# ---------------------------------------------------------------------------

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    )
}

def _fetch_mojim(query: str) -> list:
    """
    從魔鏡歌詞網 (mojim.com) 抓取歌詞行列表。
    成功回傳 list[str]，失敗回傳 []。
    """
    try:
        # 1. 搜尋頁
        search_url = f'https://mojim.com/twzh_{requests.utils.quote(query)}.htm'
        r = requests.get(search_url, headers=_HEADERS, timeout=10)
        r.encoding = 'utf-8'
        soup = BeautifulSoup(r.text, 'html.parser')

        # 找第一個歌曲連結
        link_tag = soup.select_one('dl.search_result dt a, .srchst a')
        if not link_tag:
            # 嘗試直接解析搜尋結果表格
            link_tag = soup.find('a', href=re.compile(r'/tw\d+x\d+x\d+\.htm'))
        if not link_tag:
            return []

        song_url = 'https://mojim.com' + link_tag['href']
        time.sleep(0.5)

        # 2. 歌詞頁
        r2 = requests.get(song_url, headers=_HEADERS, timeout=10)
        r2.encoding = 'utf-8'
        soup2 = BeautifulSoup(r2.text, 'html.parser')

        # 魔鏡歌詞放在 #fsZx2 或 #fsZx3 的 <dd> 下
        lyric_div = soup2.select_one('#fsZx2, #fsZx3')
        if not lyric_div:
            return []

        # 取出純文字，以 <br> 為換行
        lines = []
        for part in lyric_div.stripped_strings:
            part = part.strip()
            if part and not part.startswith('更多歌詞') and len(part) > 1:
                lines.append(part)
        return lines

    except Exception:
        return []


def _fetch_gecimi(query: str) -> list:
    """
    從 歌詞 API (gecimi.com) 抓取，作為備用來源。
    """
    try:
        api = f'http://gecimi.com/api/lyric/{requests.utils.quote(query)}'
        r = requests.get(api, headers=_HEADERS, timeout=8)
        data = r.json()
        if not data.get('result') or not data['result'].get('count'):
            return []
        lrc_url = data['result']['results'][0].get('lrc')
        if not lrc_url:
            return []
        r2 = requests.get(lrc_url, headers=_HEADERS, timeout=8)
        # 解析 LRC 格式：去除 [mm:ss.xx] 時間戳記
        lines = []
        for line in r2.text.splitlines():
            text = re.sub(r'\[\d+:\d+\.\d+\]', '', line).strip()
            if text:
                lines.append(text)
        return lines
    except Exception:
        return []


def fetch_lyrics(query: str) -> list:
    """
    依序嘗試各歌詞來源，回傳歌詞行列表。
    失敗則回傳空列表（main.py 會 fallback 保留 Whisper 原始輸出）。
    """
    if not _NET_OK:
        print('      [lyrics_corrector] 缺少 requests/bs4，跳過網路歌詞抓取')
        return []

    print(f'      正在搜尋歌詞：{query}')

    lines = _fetch_mojim(query)
    if lines:
        print(f'      [mojim] 取得 {len(lines)} 行歌詞')
        return lines

    lines = _fetch_gecimi(query)
    if lines:
        print(f'      [gecimi] 取得 {len(lines)} 行歌詞')
        return lines

    print('      找不到外部歌詞，保留 Whisper 原始輸出')
    return []


# ---------------------------------------------------------------------------
# 8. correct_ass：main.py 呼叫的入口
# ---------------------------------------------------------------------------

def correct_ass(ass_path, query: str, threshold: float = 0.45) -> bool:
    """
    main.py 呼叫介面：
        correct_ass(ass_path: Path | str, query: str) -> bool

    - 先檢查 lyrics_plain.txt（手動歌詞優先）
    - 再從網路抓取 query 對應的歌詞
    - 對 ass_path 做 in-place 校正（覆寫原檔）
    - 成功回傳 True，無歌詞或失敗回傳 False（main.py 以 file size 判斷）
    """
    ass_path = Path(ass_path)

    # 0. 手動歌詞優先（lyrics_plain.txt）
    plain_path = ROOT_DIR / "lyrics_plain.txt"
    if plain_path.exists():
        lines = [l.strip() for l in plain_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if lines:
            print(f"  偵測到 lyrics_plain.txt，以手動歌詞為主（{len(lines)} 行）")
            # 儲存原始備份供確認
            save_dir = ROOT_DIR / "output" / "subtitles"
            save_dir.mkdir(parents=True, exist_ok=True)
            (save_dir / "lyrics_fetched.txt").write_text("\n".join(lines), encoding="utf-8")
            correct_lyrics(str(ass_path), str(plain_path), str(ass_path), threshold, verbose=True)
            # 驗證結果
            filled = parse_ass(str(ass_path))[1]
            ok = sum(1 for d in filled if not d.get('needs_review'))
            review = sum(1 for d in filled if d.get('needs_review'))
            print(f'      校正結果：{ok}/{len(filled)} 行自動完成，{review} 行標記待確認')
            return True

    # 1. 抓歌詞（網路）
    lyric_lines = fetch_lyrics(query)
    if not lyric_lines:
        return False

    # 儲存原始歌詞供確認
    save_dir = ROOT_DIR / "output" / "subtitles"
    save_dir.mkdir(parents=True, exist_ok=True)
    (save_dir / "lyrics_fetched.txt").write_text("\n".join(lyric_lines), encoding="utf-8")
    print(f"  原始歌詞已儲存：{save_dir / 'lyrics_fetched.txt'}")

    # 2. 自動拆行（歌詞網常把兩句並排在同一行，以空格分隔）
    lyric_lines = _split_lyric_lines(lyric_lines)

    # 3. 解析 ASS
    header, dialogues, footer = parse_ass(str(ass_path))

    # 4. 對齊 + 填充
    aligned = align_ass_to_lyrics(dialogues, lyric_lines, threshold)
    filled  = [fill_slots(d) for d in aligned]

    # 5. in-place 覆寫
    write_ass(str(ass_path), header, filled, footer)

    total    = len(filled)
    ok       = sum(1 for d in filled if not d.get('needs_review'))
    review   = sum(1 for d in filled if d.get('needs_review'))
    print(f'      校正結果：{ok}/{total} 行自動完成，{review} 行標記待確認')
    return True


def _split_lyric_lines(lines: list) -> list:
    """
    把「兩句並排在同一行（空格分隔）」的歌詞拆開。
    與 load_lyrics 邏輯一致：只有「恰好一個分隔點，且兩側各 >=4 字」才拆。
    """
    result = []
    for line in lines:
        splits = list(re.finditer(
            r'(?<=[\u4e00-\u9fff\u3040-\u30ff])[ \u3000]+(?=[\u4e00-\u9fff\u3040-\u30ff])',
            line
        ))
        if len(splits) == 1:
            m     = splits[0]
            left  = re.sub(r'\s', '', line[:m.start()])
            right = re.sub(r'\s', '', line[m.end():])
            if len(left) >= 5 and len(right) >= 5:
                for p in [line[:m.start()].strip(), line[m.end():].strip()]:
                    if p: result.append(p)
                continue
        # 多個空格或不符合拆分條件：整行保留
        line = line.strip()
        if line:
            result.append(line)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='以 Whisper ASS 斷句為主，將外部正確歌詞填入 \\k slot'
    )
    parser.add_argument('--ass',       required=True,  help='Whisper 輸出的 .ass 檔案')
    parser.add_argument('--lyrics',    required=True,  help='外部正確歌詞 .txt')
    parser.add_argument('--output',    required=True,  help='輸出 .ass 路徑')
    parser.add_argument('--threshold', type=float, default=0.45,
                        help='行對齊最低信心分（預設 0.45）')
    parser.add_argument('--verbose',   action='store_true',
                        help='顯示每行對齊結果')
    args = parser.parse_args()

    correct_lyrics(
        ass_path    = args.ass,
        lyrics_path = args.lyrics,
        output_path = args.output,
        threshold   = args.threshold,
        verbose     = args.verbose,
    )


if __name__ == '__main__':
    main()