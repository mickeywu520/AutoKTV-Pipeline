import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)

from spleeter.separator import Separator

from config import AUDIO_DIR


def separate_vocals(audio_path: Path) -> tuple[Path, Path]:
    stem = audio_path.stem
    output_parent = AUDIO_DIR / stem
    output_parent.mkdir(parents=True, exist_ok=True)

    separator = Separator("spleeter:2stems")
    separator.separate_to_file(str(audio_path), str(AUDIO_DIR))

    vocals_path = output_parent / "vocals.wav"
    accompaniment_path = output_parent / "accompaniment.wav"

    errors = []
    if not vocals_path.exists():
        errors.append(f"vocals.wav not found at {vocals_path}")
    if not accompaniment_path.exists():
        errors.append(f"accompaniment.wav not found at {accompaniment_path}")

    if errors:
        raise FileNotFoundError("; ".join(errors))

    return vocals_path, accompaniment_path
