from scripts.check_repository import violations


def test_publication_guard_catches_credentials_and_artifacts_without_echoing_them():
    assert violations("data/manifest.json", b"{}")
    assert violations("detector.pt", b"weights")
    assert violations(".env.production", b"configuration")
    assert violations("source.py", ("ghp_" + "a" * 36).encode())
    assert violations("large.bin", bytes(1024 * 1024 + 1))
    assert violations("renamed-catalog.txt", b"SQLite format 3\0private data")
    assert violations("small-image.dat", b"\x89PNG\x00artifact")
    assert violations("source.py", ("-----BEGIN " + "PRIVATE KEY-----").encode())
    assert not violations("hogline/domain/models.py", b"class Model: pass")
    assert not violations(".env.example", b"HOGLINE_DATA_DIR=data")
