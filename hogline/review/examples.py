"""Read-only access to the explicitly generated, local one-shot demonstration."""

from fastapi import HTTPException
from fastapi.responses import FileResponse

FILES = {
    "index.html",
    "reconstruction.mp4",
    "source-shot.mp4",
    "poster.png",
    "trajectory.json",
    "observations.json",
    "manifest.json",
}


def register_examples(app, db):
    def artifact(name):
        root = (db.settings.data_dir / "shot-reconstruction" / "example").resolve()
        path = (root / name).resolve()
        if name not in FILES or path.parent != root or not path.is_file():
            raise HTTPException(404, "One-shot example artifact is unavailable")
        return FileResponse(path)

    @app.get("/examples/shot-reconstruction/", include_in_schema=False)
    def shot_example():
        return artifact("index.html")

    @app.get("/examples/shot-reconstruction/{name}", include_in_schema=False)
    def shot_example_artifact(name: str):
        return artifact(name)
