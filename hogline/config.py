import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    max_cache_mb: int = 2048
    workers: int = 4
    working_video_mb: int = 512
    working_video_dir: Path | None = None

    def __post_init__(self):
        if not 1 <= self.workers <= 32:
            raise ValueError("Worker count must be between 1 and 32")
        if not 0 <= self.working_video_mb <= 4096:
            raise ValueError("Working video allowance must be between 0 and 4096 MiB per worker")

    @classmethod
    def load(cls, data_dir: str | Path | None = None):
        root = Path(data_dir or os.getenv("HOGLINE_DATA_DIR", "data")).resolve()
        root.mkdir(parents=True, exist_ok=True)
        url = os.getenv("HOGLINE_DATABASE_URL", f"sqlite:///{root / 'catalog.sqlite3'}")
        return cls(
            root,
            url,
            int(os.getenv("HOGLINE_MAX_CACHE_MB", "2048")),
            int(os.getenv("HOGLINE_WORKERS", "4")),
            int(os.getenv("HOGLINE_WORKING_VIDEO_MB", "512")),
        )


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
    broadcast = layout.get("broadcast", {})
    templates = list(broadcast.get("scoreboards", {}).values())
    auto = broadcast.get("automatic_board_header")
    if auto:
        if not isinstance(auto.get("reference"), dict):
            raise ValueError("Automatic board reading needs an explicit retained header reference")
        templates.append(auto["reference"])
    for template in templates:
        bounds, centers = template.get("bounds", []), template.get("point_centers", [])
        if (
            len(bounds) != 4
            or not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in bounds)
            or bounds[0] >= bounds[2]
            or bounds[1] >= bounds[3]
            or len(centers) < 2
            or not all(isinstance(v, (int, float)) and 0 < v < 1 for v in centers)
            or any(a >= b for a, b in zip(centers, centers[1:], strict=False))
        ):
            raise ValueError("Board templates need ordered crop bounds and increasing point centers")
        if auto and template is auto["reference"] and len(centers) != 15:
            raise ValueError("Automatic header registration currently requires fifteen point columns")
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
