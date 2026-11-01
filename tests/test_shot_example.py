from fastapi.testclient import TestClient

from hogline.review.app import create_app


def test_example_serves_only_generated_artifacts_and_video_ranges(db):
    root = db.settings.data_dir / "shot-reconstruction" / "example"
    root.mkdir(parents=True)
    (root / "index.html").write_text("<h1>Reviewed one-shot example</h1>")
    (root / "reconstruction.mp4").write_bytes(bytes(range(100)))
    (root / "private.txt").write_text("private intermediate")
    client = TestClient(create_app(db.settings, start_worker=False))
    assert client.get("/examples/shot-reconstruction/").status_code == 200
    r = client.get("/examples/shot-reconstruction/reconstruction.mp4", headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == bytes(range(10, 20))
    assert r.headers["content-type"] == "video/mp4"
    assert client.get("/examples/shot-reconstruction/private.txt").status_code == 404
    assert client.get("/examples/shot-reconstruction/observations.json").status_code == 404


def test_example_refuses_artifact_symlinks_outside_its_folder(db):
    root = db.settings.data_dir / "shot-reconstruction" / "example"
    root.mkdir(parents=True)
    private = db.settings.data_dir / "private.json"
    private.write_text("not an example artifact")
    (root / "manifest.json").symlink_to(private)
    client = TestClient(create_app(db.settings, start_worker=False))
    assert client.get("/examples/shot-reconstruction/manifest.json").status_code == 404
