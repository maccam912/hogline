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
    for template in layout.get("broadcast", {}).get("scoreboards", {}).values():
        reference = template.get("layout_reference")
        if reference:
            bounds = reference.get("header_bounds", [])
            if (
                not isinstance(reference.get("source_key"), str)
                or not isinstance(reference.get("source_seconds"), (int, float))
                or not 0 <= reference["source_seconds"] < float("inf")
                or len(bounds) != 4
                or not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in bounds)
                or bounds[0] >= bounds[2]
                or bounds[1] >= bounds[3]
            ):
                raise ValueError("Layout reference needs a source/time and ordered normalized header bounds")
        for scope in template.get("source_intervals", []):
            if (
                not isinstance(scope.get("source_key"), str)
                or not isinstance(scope.get("start_seconds"), (int, float))
                or not isinstance(scope.get("end_seconds"), (int, float))
                or not 0 <= scope["start_seconds"] < scope["end_seconds"] < float("inf")
            ):
                raise ValueError("Scoreboard scope needs a stable source key and finite ordered source times")
    return layout
