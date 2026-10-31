from types import SimpleNamespace

from hogline.reconstruction.skill_candidates import delivery_candidates


def sequence(identifier, first=0, last=3, color="red", call="unknown", source="source"):
    return {
        "id": identifier,
        "created_at": identifier,
        "source_id": source,
        "samples": [{"source_seconds": n} for n in range(10)],
        "proposal": {
            "deliveries": [
                {
                    "first_frame": first,
                    "last_frame": last,
                    "release_frame": None,
                    "stone_color": color,
                    "call": call,
                    "thrower_description": "Gray jacket",
                }
            ]
        },
    }


def test_overlapping_views_form_one_delivery_and_reuse_saved_draft():
    a, b = sequence("a"), sequence("b", first=2, last=5)
    shot = SimpleNamespace(id="saved", evidence=["skill-sequence:b:frame:2"])
    candidates = delivery_candidates([b, a], [shot])
    assert len(candidates) == 1
    assert len(candidates[0]["observations"]) == 2
    assert candidates[0]["draft_id"] == "saved"
    assert delivery_candidates([a, b], [shot]) == candidates


def test_separate_deliveries_sources_colors_and_stale_observations():
    stale = {**sequence("z"), "stale": True}
    result = delivery_candidates(
        [
            sequence("a"),
            sequence("b", first=4, last=7),
            sequence("c", color="yellow"),
            sequence("d", source="other"),
            stale,
        ]
    )
    assert len(result) == 4


def test_conflicting_call_and_duplicate_reviews_remain_visible():
    shots = [
        SimpleNamespace(id="one", evidence=["skill-sequence:a:frame:1"]),
        SimpleNamespace(id="two", evidence=["skill-sequence:b:frame:2"]),
    ]
    result = delivery_candidates([sequence("a", call="draw"), sequence("b", call="takeout")], shots)[0]
    assert result["call"] == "unknown" and result["call_conflict"]
    assert result["duplicate_review"]
    assert result["linked_shot_ids"] == ["one", "two"]


def test_unknown_color_merges_only_with_one_known_delivery_and_keeps_review():
    unknown = sequence("a", first=0, last=4, color="unknown")
    red = sequence("b", first=3, last=6)
    saved = SimpleNamespace(id="saved", evidence=["skill-sequence:b:frame:4"])
    result = delivery_candidates([red, unknown], [saved])
    assert len(result) == 1
    assert result[0]["stone_color"] == "red"
    assert result[0]["draft_id"] == "saved"
    assert len(result[0]["observations"]) == 2
    assert not result[0]["ambiguous_overlap"]
    assert delivery_candidates([unknown, red], [saved]) == result


def test_unknown_color_cannot_bridge_different_colors_or_separate_red_deliveries():
    for color in ("red", "yellow"):
        result = delivery_candidates(
            [
                sequence("a", first=0, last=2),
                sequence("b", first=5, last=7, color=color),
                sequence("c", first=1, last=6, color="unknown"),
            ]
        )
        assert len(result) == 3
        unresolved = next(c for c in result if c["stone_color"] == "unknown")
        assert unresolved["ambiguous_overlap"]
        assert all(len(c["observations"]) == 1 for c in result)


def test_unknown_color_does_not_attach_across_sources_or_without_overlap():
    result = delivery_candidates(
        [
            sequence("a", first=0, last=2),
            sequence("b", first=1, last=3, color="unknown", source="other"),
            sequence("c", first=4, last=6, color="unknown"),
        ]
    )
    assert len(result) == 3
    assert not any(c["ambiguous_overlap"] for c in result)
