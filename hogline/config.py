import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    max_cache_mb: int = 2048

    @classmethod
    def load(cls, data_dir: str | Path | None = None):
        root = Path(data_dir or os.getenv("HOGLINE_DATA_DIR", "data")).resolve()
        root.mkdir(parents=True, exist_ok=True)
        url = os.getenv("HOGLINE_DATABASE_URL", f"sqlite:///{root / 'catalog.sqlite3'}")
        return cls(root, url, int(os.getenv("HOGLINE_MAX_CACHE_MB", "2048")))


def profile():
    path = Path(os.getenv("HOGLINE_PROFILE", Path(__file__).parent / "media" / "wausau-v1.json"))
    layout = json.loads(path.read_text())
    required = {"house_top", "house_bottom", "perspective_left", "perspective_right"}
    if not required <= layout.get("views", {}).keys():
        raise ValueError("Layout profile must define both house and perspective views")
    for bounds in layout["views"].values():
        if len(bounds) != 4 or not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in bounds):
            raise ValueError("Crop bounds must contain four normalized coordinates")
        if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            raise ValueError("Crop bounds must have positive width and height")
    return layout
