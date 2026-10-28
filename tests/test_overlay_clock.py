from hogline.media.clocks import fit_clock_hypotheses, parse_overlay
from hogline.perception.overlay_clock import combine_clock_reads


def clock(dates, times):
    return combine_clock_reads([{"text": d} for d in dates], [{"text": t} for t in times], "America/Chicago")


def test_conflicting_dates_remain_separate_and_repeated_reads_are_not_independent_frames():
    read = clock(["09/17/2026", "03/17/2026"], ["", "07:50:58"])
    assert read["local_date"] is None and read["date_text"] == ""
    assert read["ocr_conflict"] is True and len(read["candidates"]) == 4
    matching = clock(["03/17/2026", "3/17/2026"], ["07:50:58", "7:50:58"])
    assert not matching["ocr_conflict"] and len(matching["candidates"]) == 2
    assert len(matching["alternatives"][0]["supporting_reads"]) == 4
    assert not fit_clock_hypotheses(
        [{"source_id": "s", "source_seconds": 315, "evidence_id": "one", "clock": matching}] * 3
    )


def test_temporal_consistency_supports_reading_without_rewriting_frame_alternatives():
    readings = [
        clock(["03/17/2026"], ["07:50:43"]),
        clock(["09/17/2026", "03/17/2026"], ["07:50:58"]),
        clock(["03/17/2026"], ["07:51:13"]),
    ]
    frames = [
        {"source_id": "s", "source_seconds": 300 + i * 15, "evidence_id": str(i), "clock": c}
        for i, c in enumerate(readings)
    ]
    segments = fit_clock_hypotheses(frames)
    assert len(segments) == 2  # AM/PM alternatives remain unresolved.
    assert all(len(s["evidence_ids"]) == 3 for s in segments)
    assert readings[1]["ocr_conflict"] and len(readings[1]["candidates"]) == 4
    assert not clock(["garbled"], ["invalid"])["candidates"]


def test_eight_observed_date_digits_can_survive_missing_separators():
    result = parse_overlay("0317/2026", "09:53:18")
    assert result["local_date"] == "2026-03-17"
    assert result["date_text"] == "0317/2026"
    assert result["date_syntax"] == "eight_digit_mdy_missing_separator"
    assert not parse_overlay("0182026", "12:48:38")["candidates"]
    assert not parse_overlay("103/17/2026", "12:48:38")["candidates"]
