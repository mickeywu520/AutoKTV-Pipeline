import subprocess
import tempfile
from pathlib import Path

from faster_whisper import WhisperModel

from config import (
    MODEL_SIZE, DEVICE, COMPUTE_TYPE, LANGUAGE,
    SUBTITLES_DIR, KTV_STYLE,
)


def _convert_to_16k_mono(input_path: Path) -> Path:
    fd, tmp_path = tempfile.mkstemp(suffix=".wav")
    import os
    os.close(fd)

    subprocess.run(
        ["ffmpeg", "-y", "-i", str(input_path),
         "-ar", "16000", "-ac", "1",
         "-sample_fmt", "s16",
         tmp_path],
        capture_output=True, check=True,
    )
    return Path(tmp_path)


def _seconds_to_ass(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _build_ass_style() -> str:
    s = KTV_STYLE
    return (
        f"Style:Karaoke,{s['font']},{s['font_size']},"
        f"{s['color_unsung']},{s['color_sung']},&H000000,&H64000000,"
        f"0,0,0,0,100,100,0,0,1,{s['outline']},{s['shadow']},"
        f"2,80,80,{s['margin_v']},1"
    )


def _words_to_ass_dialogue(words: list, start: float, end: float) -> str:
    line_parts = []
    for w in words:
        dur_cs = max(1, int(round((w.end - w.start) * 100)))
        line_parts.append(f"{{\\k{dur_cs}}}{w.word}")
    text = "".join(line_parts)
    return (
        f"Dialogue: 0,{_seconds_to_ass(start)},{_seconds_to_ass(end)},"
        f"Karaoke,,0,0,0,,{text}"
    )


def transcribe_to_ass(vocals_path: Path, output_name: str = None) -> Path:
    SUBTITLES_DIR.mkdir(parents=True, exist_ok=True)

    if output_name is None:
        output_name = vocals_path.parent.stem
    ass_path = SUBTITLES_DIR / f"{output_name}.ass"

    converted = _convert_to_16k_mono(vocals_path)

    try:
        model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
        segments, info = model.transcribe(
            str(converted), word_timestamps=True,
            language=LANGUAGE,
        )

        lines = []
        for seg in segments:
            if not seg.words:
                continue
            words = list(seg.words)
            lines.append(_words_to_ass_dialogue(
                words, seg.start, seg.end
            ))
    finally:
        converted.unlink(missing_ok=True)

    header = (
        "[Script Info]\n"
        "Title: AutoKTV-Pipeline\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{_build_ass_style()}\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    ass_path.write_text(header + "\n".join(lines), encoding="utf-8")

    if ass_path.stat().st_size == 0:
        raise RuntimeError(f"Transcription produced empty .ass: {ass_path}")

    return ass_path
