from datetime import UTC, datetime

from hogline.reconstruction.boundaries import boundary_links


def corpus():
    sources = [
        {"id": "left", "sheet": 4, "duration_seconds": 1000, "metadata_sha256": "left-v1"},
        {"id": "right", "sheet": 4, "duration_seconds": 500, "metadata_sha256": "right-v1"},
    ]
    origin = datetime(2026, 3, 17, 12, tzinfo=UTC).timestamp()
    observations = []
    for source, times, offset in [
        (sources[0], [970, 985, 999], origin),
        (sources[1], [0, 15, 30], origin + 1170),
    ]:
        for i, seconds in enumerate(times):
            observations.append(
                {
                    "source_id": source["id"],
                    "evidence_id": f"{source['id']}-{i}",
                    "source_metadata_sha256": source["metadata_sha256"],
                    "source_seconds": seconds,
                    "youtube_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA&t=0s",
                    "clock": {
                        "candidates": [
                            {"utc": datetime.fromtimestamp(offset + seconds + shift, UTC).isoformat()}
                            for shift in [0, 43200]
                        ]
                    },
                    "scoreboard": {
                        "status": "observed",
                        "layout_alignment": {"status": "compatible_seed"},
                        "cards": [
                            {"color": "red", "end_number": 1, "cumulative_points": 2},
                            {"color": "red", "end_number": 2, "cumulative_points": 3},
                        ],
                        "unreadable_slots": [],
                        "conflicting_end_numbers": [],
                    },
                }
            )
    return sources, observations


def test_clock_board_candidate_keeps_gap_and_am_pm_but_never_invents_game_or_result():
    sources, observations = corpus()
    result = boundary_links(sources, observations, "clock-v7")
    assert len(result) == 1
    link = result[0]
    assert link["kind"] == "possible_continuation"
    assert link["estimated_recording_gap_seconds"] == 170
    assert link["unobserved_between_nearest_anchors_seconds"] == 171
    assert len(link["clock_alternatives"]) == 2
    assert link["board_comparison"]["agreeing_colors"] == ["red"]
    assert link["left"]["nearest_evidence_id"] == "left-2"
    assert link["right"]["nearest_evidence_id"] == "right-0"
    assert link["game_count"] is None and link["result"] is None
    assert link["acceptance"] == "proposed"


def test_source_hash_sheet_missing_evidence_and_far_extrapolation_are_not_silent_links():
    sources, observations = corpus()
    sources[1]["metadata_sha256"] = "changed"
    assert not boundary_links(sources, observations, "clock-v7")
    sources[1]["metadata_sha256"] = "right-v1"
    sources[1]["sheet"] = 5
    assert not boundary_links(sources, observations, "clock-v7")
    sources[1]["sheet"] = 4
    ids = {o["evidence_id"] for o in observations if o["evidence_id"] != "right-2"}
    assert not boundary_links(sources, observations, "clock-v7", ids)
    sources[0]["duration_seconds"] = 2000
    assert not boundary_links(sources, observations, "clock-v7")


def test_board_change_is_unresolved_and_duplicate_frames_do_not_increase_support():
    sources, observations = corpus()
    for observation in observations[3:]:
        observation["scoreboard"]["cards"][1]["cumulative_points"] = 4
    link = boundary_links(sources, observations, "clock-v7")[0]
    assert link["kind"] == "nearby_recording_gap"
    assert link["board_comparison"]["status"] == "changed_readable_rows"
    duplicate = [{**observations[0], "evidence_id": str(i)} for i in range(5)]
    assert not boundary_links(sources, duplicate + observations[3:], "clock-v7")
