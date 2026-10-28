import pytest
from PIL import Image, ImageDraw

from hogline.perception.scoreboard import card_visibility, read_board
from hogline.reconstruction.board_events import row_reading


@pytest.mark.parametrize("scale", [1, 2, 3])
def test_short_white_hat_and_dark_foreground_abstain_without_invalidating_other_row(scale):
    image = Image.new("RGB", (80, 60), (180, 25, 45))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 30, 79, 59), fill=(220, 200, 30))
    draw.rectangle((12, 1, 27, 28), fill="white")  # Full-height red end card.
    draw.rectangle((48, 48, 70, 59), fill="white")  # White cap enters only the bottom.
    draw.rectangle((0, 43, 39, 59), fill=(20, 20, 20))  # Dark foreground, no white card.
    image = image.resize((80 * scale, 60 * scale))

    class Reader:
        calls = 0

        def read(self, image, **kwargs):
            self.calls += 1
            return {"text": "2", "engine_confidence": 80}

    reader = Reader()
    board = read_board(
        image,
        {
            "bounds": [0, 0, 1, 1],
            "rows": {"red": [0, 0.5], "yellow": [0.5, 1]},
            "point_centers": [0.25, 0.75],
        },
        reader,
    )
    assert reader.calls == 2  # Two OCR modes for the real card; none for either obstruction.
    assert [(c["color"], c["end_number"]) for c in board["cards"]] == [("red", 2)]
    assert board["conflicting_end_numbers"] == []
    assert {s["reason"] for s in board["unreadable_slots"]} == {
        "row_background_obstructed_or_uncertain",
        "white_patch_not_full_height_card",
    }
    observation = {"scoreboard": {"status": "observed", **board}}
    assert row_reading(observation, "yellow") is None
    assert row_reading(observation, "red") == ((2, 1),)


def test_clear_colored_row_and_full_height_card_remain_distinct_from_uncertain_white_patch():
    for color, rgb in (("red", (180, 25, 45)), ("yellow", (220, 200, 30))):
        cell = Image.new("RGB", (20, 30), rgb)
        assert card_visibility(cell, color)[1]["status"] == "no_card_candidate"
        ImageDraw.Draw(cell).rectangle((4, 1, 15, 28), fill="white")
        assert card_visibility(cell, color)[1]["status"] == "card_candidate"
        # Height alone is insufficient: a patch that never reaches the top is not a card.
        cell = Image.new("RGB", (20, 30), rgb)
        ImageDraw.Draw(cell).rectangle((0, 10, 19, 29), fill="white")
        result = card_visibility(cell, color)[1]
        assert result["white_height_fraction"] >= 0.65
        assert result["status"] == "unreadable" and result["calibrated_probability"] is None
