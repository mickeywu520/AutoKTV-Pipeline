import subprocess
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)

from spleeter.separator import Separator

from config import AUDIO_DIR


def separate_vocals(audio_path: Path) -> tuple[Path, Path]:
    stem = audio_path.stem
    output_parent = AUDIO_DIR / stem
    output_parent.mkdir(parents=True, exist_ok=True)

    _, tmp_wav = tempfile.mkstemp(suffix=".wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(audio_path), "-acodec", "pcm_s16le",
         "-ar", "44100", "-ac", "2", tmp_wav],
        capture_output=True, check=True,
    )

    separator = Separator("spleeter:2stems")
    separator.separate_to_file(tmp_wav, str(AUDIO_DIR))

    tmp_stem = Path(tmp_wav).stem
    tmp_output_parent = AUDIO_DIR / tmp_stem
    vocals_path = tmp_output_parent / "vocals.wav"
    accompaniment_path = tmp_output_parent / "accompaniment.wav"

    errors = []
    if not vocals_path.exists():
        errors.append(f"vocals.wav not found at {vocals_path}")
    if not accompaniment_path.exists():
        errors.append(f"accompaniment.wav not found at {accompaniment_path}")

    if errors:
        raise FileNotFoundError("; ".join(errors))

    new_vocals = output_parent / "vocals.wav"
    new_accompaniment = output_parent / "accompaniment.wav"
    new_vocals.unlink(missing_ok=True)
    new_accompaniment.unlink(missing_ok=True)
    vocals_path.rename(new_vocals)
    accompaniment_path.rename(new_accompaniment)
    tmp_output_parent.rmdir()

    return new_vocals, new_accompaniment
