import pytest

from hogline.config import profile
from hogline.perception import layout


def test_packaged_references_work_without_catalog_or_data(tmp_path):
    config = profile()["broadcast"]
    templates = [*config["scoreboards"].values(), config["automatic_board_header"]["reference"]]
    for template in templates:
        image, manifest = layout.reference_image(None, tmp_path / "nonexistent-data", template)
        assert image.size == (1920, 1080)
        assert manifest["image_sha256"] == template["layout_reference"]["asset"]["sha256"]
        assert manifest["source_key"] == template["layout_reference"]["source_key"]
    assert not (tmp_path / "nonexistent-data").exists()


def test_missing_or_corrupt_assets_fail_preflight(tmp_path, monkeypatch):
    monkeypatch.setattr(layout, "REFERENCE_ROOT", tmp_path)
    config = profile()
    with pytest.raises(ValueError, match="reference is missing"):
        layout.validate_reference_assets(config)
    template = config["broadcast"]["scoreboards"]["4"]
    path = tmp_path / template["layout_reference"]["asset"]["path"]
    path.parent.mkdir(parents=True)
    path.write_bytes(b"bad")
    with pytest.raises(ValueError, match="reference is corrupt"):
        layout.validate_reference_assets(config)
