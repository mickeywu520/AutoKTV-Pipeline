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
    載入歌詞，並自動把「空格分隔的兩句」拆成兩行。
    歌詞網常見格式：「像泡沫懸浮著 在風裡小心穿梭」→ 拆成兩行。
    拆分條件：全形空格或半形空格出現在字元之間，
             且空格兩側都有中文字元（避免歌手名稱誤拆）。
    """
    import re as _re
    with open(path, encoding='utf-8') as f:
        raw_lines = [l.strip() for l in f.readlines()]
    result = []
    for line in raw_lines:
        if not line:
            continue
        # 在 CJK字元 + 空格 + CJK字元 處拆行
        parts = _re.split(r'(?<=[\u4e00-\u9fff\u3040-\u30ff])[ \u3000]+(?=[\u4e00-\u9fff\u3040-\u30ff])', line)
        for p in parts:
            p = p.strip()
            if p:
                result.append(p)
    return result


def normalize(text: str) -> str:
    """正規化用於相似度比對：全形→半形、去標點空格"""
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'[\s　，。！？、：；「」『』【】〔〕…—～·\-_]', '', text)
    return text


# ---------------------------------------------------------------------------
# 3. 行級對齊
# ---------------------------------------------------------------------------

def align_ass_to_lyrics(
    dialogues    : list,
    lyric_lines  : list,
    sim_threshold: float = 0.45,
    verbose      : bool  = False,
) -> list:
    """
    對每個 Dialogue 行找最佳對齊歌詞。
    策略：
      - 優先從上次位置往後 12 行（保持順序性）
      - 若信心不足，全局搜尋（支援副歌重複）
      - 同時嘗試單行與連續兩行合併
    """
    norm_lyrics = [normalize(l) for l in lyric_lines]
    result      = []
    lyric_ptr   = 0

    for dia in dialogues:
        ass_text = slots_to_text(dia['slots'])
        norm_ass = normalize(ass_text)

        if not norm_ass:
            d = deepcopy(dia)
            d.update({'lyric_match': ass_text, 'confidence': 1.0, 'needs_review': False})
            result.append(d)
            continue

        best = {'score': -1, 'idx': lyric_ptr, 'text': ''}

        def try_range(start, end):
            for li in range(start, min(end, len(norm_lyrics))):
                # 單行
                s1 = fuzz.ratio(norm_ass, norm_lyrics[li]) / 100.0
                if s1 > best['score']:
                    best.update({'score': s1, 'idx': li, 'text': lyric_lines[li]})
                # 雙行合併
                if li + 1 < len(norm_lyrics):
                    s2 = fuzz.ratio(norm_ass, norm_lyrics[li] + norm_lyrics[li+1]) / 100.0
                    if s2 > best['score']:
                        best.update({'score': s2, 'idx': li,
                                     'text': lyric_lines[li] + lyric_lines[li+1]})

        try_range(lyric_ptr, lyric_ptr + 12)
        if best['score'] < sim_threshold:
            try_range(0, len(norm_lyrics))  # 全局搜尋（副歌重複）

        if best['score'] >= sim_threshold:
            lyric_ptr = best['idx'] + 1

        needs_review = best['score'] < sim_threshold

        if verbose:
            tag = '[?]' if needs_review else '   '
            print(f"{tag} [{best['score']:.2f}]  ASS: {ass_text!r:22s}  →  LYRIC: {best['text']!r}")

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

def fill_slots(dia: dict) -> dict:
    """
    把 lyric_match 的正確文字填回 \\k slot，時間優先保留。

    Case A（字數相同）: 1:1 替換，時間完全不動。
    Case B（字數不同）: 按正確字數等分總時間，標記需複查。
    """
    d           = deepcopy(dia)
    correct_raw = d.get('lyric_match', '')
    correct     = normalize(correct_raw)

    if not correct:
        return d

    # 過濾空格 slot，只處理實際文字字元
    orig_slots = d['slots']
    nonsp_idx  = [i for i, (dur, ch) in enumerate(orig_slots) if ch and ch.strip()]

    if len(nonsp_idx) == len(correct):
        # Case A
        new_slots = list(orig_slots)
        for slot_i, new_ch in zip(nonsp_idx, correct):
            dur = new_slots[slot_i][0]
            new_slots[slot_i] = (dur, new_ch)
        d['slots']        = new_slots
        d['was_remapped'] = False

    else:
        # Case B：等分總時間（含原始空格的時間）
        total_cs = sum(dur for dur, _ in orig_slots)
        n        = len(correct)
        base_cs  = total_cs // n
        rem_cs   = total_cs - base_cs * n
        new_slots = []
        for i, ch in enumerate(correct):
            dur = base_cs + (1 if i < rem_cs else 0)
            new_slots.append((dur, ch))
        d['slots']        = new_slots
        d['was_remapped'] = True
        d['needs_review'] = True

    return d


# ---------------------------------------------------------------------------
# 5. ASS 重建
# ---------------------------------------------------------------------------

def rebuild_dialogue(dia: dict) -> str:
    text = slots_to_ass_text(dia['slots'], dia.get('remainder', ''))
    if dia.get('needs_review'):
        # 在前面加一行 Comment 標記，不污染 Dialogue 本身
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

    total    = len(filled)
    ok       = sum(1 for d in filled if not d.get('needs_review'))
    review   = sum(1 for d in filled if d.get('needs_review'))
    remapped = sum(1 for d in filled if d.get('was_remapped'))
    print(f'\n完成！共 {total} 行：✅ {ok} 行自動校正，'
          f'⚠️  {review} 行需確認（其中 {remapped} 行字數不符重新分配）')
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
            correct_lyrics(str(ass_path), str(plain_path), str(ass_path), threshold)
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
    """把「兩句並排在同一行（空格分隔）」的歌詞拆開，與 load_lyrics 邏輯一致。"""
    result = []
    for line in lines:
        parts = re.split(
            r'(?<=[\u4e00-\u9fff\u3040-\u30ff])[ \u3000]+(?=[\u4e00-\u9fff\u3040-\u30ff])',
            line
        )
        for p in parts:
            p = p.strip()
            if p:
                result.append(p)
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