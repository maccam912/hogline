import cv2
import numpy as np
import pytest

from hogline.perception.ice_score import IceInspector


def house():
    image = np.full((400, 400, 3), 245, dtype=np.uint8)
    cv2.circle(image, (200, 200), 175, (40, 50, 180), -1)
    cv2.circle(image, (200, 200), 120, (245, 245, 245), -1)
    cv2.circle(image, (200, 200), 55, (50, 150, 100), -1)
    cv2.circle(image, (200, 200), 15, (235, 235, 225), -1)
    return image


def inspector(reference=None):
    return IceInspector(
        house() if reference is None else reference,
        {"status": "visually_reviewed_empty_scoring_house", "evidence_id": "reference"},
    )


def stone(image, point, color, radius=13):
    cv2.circle(image, point, radius, (130, 130, 130), -1)
    cv2.circle(image, point, 6, (220, 20, 20) if color == "red" else (235, 200, 20), -1)


def test_empty_ice_requires_reference_and_positive_pixel_match():
    with pytest.raises(ValueError, match="reviewed"):
        IceInspector(house(), {})
    result = inspector().inspect(house())
    assert result["empty_house"]
    assert result["scoring"]["provisional_score"] == {"red": 0, "yellow": 0}


def test_clear_stone_bodies_score_from_granite_boundaries():
    image = house()
    stone(image, (200, 205), "yellow")
    stone(image, (270, 245), "red")
    result = inspector().inspect(image)
    assert not result["empty_house"]
    assert result["scoring"]["provisional_score"] == {"red": 0, "yellow": 1}
    assert len(result["footprints"]) == 2


def test_multiple_nearer_same_color_stones_count():
    image = house()
    stone(image, (180, 200), "red")
    stone(image, (225, 200), "red")
    stone(image, (300, 255), "yellow")
    result = inspector().inspect(image)
    assert result["scoring"]["provisional_score"] == {"red": 2, "yellow": 0}


def test_uncolored_occluder_cannot_be_classified_as_empty():
    image = house()
    cv2.rectangle(image, (160, 70), (240, 215), (30, 40, 100), -1)
    result = inspector().inspect(image)
    assert not result.get("empty_house", False)
    assert result.get("scoring", {}).get("provisional_score") is None


def test_small_translation_and_lighting_preserve_empty_house_without_erasing_stones():
    reference = house()
    shifted = cv2.warpAffine(
        reference, np.float32([[1, 0, 2], [0, 1, -2]]), (400, 400), borderMode=cv2.BORDER_REFLECT
    )
    shifted = np.clip(shifted.astype(float) * 0.9 + 5, 0, 255).astype(np.uint8)
    assert inspector().inspect(shifted)["empty_house"]
    stone(shifted, (202, 198), "red")
    result = inspector().inspect(shifted)
    assert not result["empty_house"]


def test_static_colored_logo_is_not_a_counted_stone():
    reference = house()
    cv2.circle(reference, (200, 200), 5, (235, 200, 20), -1)
    image = reference.copy()
    stone(image, (245, 245), "red")
    result = inspector(reference).inspect(image)
    assert result["scoring"]["provisional_score"] == {"red": 1, "yellow": 0}
    assert result["excluded_static_handles"] >= 1
