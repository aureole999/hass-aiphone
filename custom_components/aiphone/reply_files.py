"""Local prerecorded reply selection; no network or HA imports."""

from pathlib import Path

MAX_REPLY_SECONDS = 30
MAX_REPLY_BYTES = 10 * 1024 * 1024
REPLY_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".flac"}


def validate_reply_filename(filename: str) -> str:
    """Accept a basename, never an arbitrary path or URL."""
    filename = filename.strip()
    if (
        not filename
        or len(filename) > 128
        or filename.startswith(".")
        or "/" in filename
        or "\\" in filename
        or ":" in filename
        or Path(filename).suffix.lower() not in REPLY_EXTENSIONS
    ):
        raise ValueError(
            "Choose a WAV, MP3, M4A, OGG or FLAC filename, without a path."
        )
    return filename


def resolve_reply_file(directory: Path, filename: str) -> Path:
    root = directory.resolve()
    path = (root / validate_reply_filename(filename)).resolve()
    if path.parent != root:
        raise ValueError("Reply audio must stay inside aiphone/replies.")
    if not path.is_file():
        raise ValueError("Put the selected audio file in aiphone/replies first.")
    if not 0 < path.stat().st_size <= MAX_REPLY_BYTES:
        raise ValueError("Reply audio must be non-empty and at most 10 MiB.")
    return path
