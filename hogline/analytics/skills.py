"""Descriptive execution reports; no causal sweeping adjustment or inferred targets."""

from collections import Counter
from statistics import mean, stdev

from hogline.domain.skills import SkillDocument, participant_aliases

VERSION = "shot-execution-v5"


def distribution(values):
    return {
        "n": len(values),
        "mean": mean(values) if values else None,
        "sample_sd": stdev(values) if len(values) > 1 else None,
        "status": "descriptive" if len(values) >= 5 else "insufficient_sample",
    }


def skill_report(document: SkillDocument, duplicate_shot_ids=()):
    groups, excluded = {}, Counter()
    aliases = participant_aliases(document)
    unresolved = {p.id for p in document.participants if p.acceptance != "confirmed"}
    slots = {(s.end, s.side, s.position): s for s in document.slots if s.acceptance == "confirmed"}
    for shot in document.shots:
        if shot.id in duplicate_shot_ids:
            excluded["duplicate_delivery_review"] += 1
            continue
        if shot.acceptance != "confirmed":
            excluded[shot.acceptance] += 1
            continue
        position = (
            ("lead", "second", "third", "fourth")[(shot.team_delivery - 1) // 2]
            if shot.team_delivery
            else None
        )
        slot = slots.get((shot.end, shot.side, position))
        shot_participant = aliases.get(shot.participant, shot.participant)
        slot_participant = aliases.get(slot.participant, slot.participant) if slot else None
        participant = shot_participant or slot_participant
        if participant in unresolved:
            excluded["unreviewed_participant_continuity"] += 1
            continue
        if slot and shot.participant and slot_participant != shot_participant:
            excluded["participant_conflict"] += 1
            continue
        if not participant:
            if position is None:
                excluded["unknown_participant_and_position"] += 1
                continue
            # End-scoped until a reviewed assignment establishes continuity.
            participant = f"End {shot.end} {shot.side} {position or 'unknown position'}"
        if shot.call == "unknown" or shot.direction == "unknown":
            excluded["unknown_call_or_direction"] += 1
            continue
        line_calibration = shot.line_calibration or shot.calibration
        key = (participant, shot.call, shot.direction, shot.turn, shot.calibration, line_calibration)
        group = groups.setdefault(
            key,
            {
                "participant": participant,
                "call": shot.call,
                "direction": shot.direction,
                "turn": shot.turn,
                "calibration": shot.calibration,
                "line_calibration": line_calibration,
                "shot_ids": [],
                "weight": [],
                "line": [],
                "wide": [],
                "sweeping": [],
                "missing": Counter(),
            },
        )
        group["shot_ids"].append(shot.id)
        if shot.calibration and shot.speed_mps is not None and shot.target_speed_mps is not None:
            group["weight"].append(shot.speed_mps / shot.target_speed_mps)
        else:
            group["missing"]["weight_target_or_calibration"] += 1
        if (
            shot.line_error_degrees is not None
            and line_calibration
            and (shot.aim is None or shot.aim.acceptance == "confirmed")
        ):
            group["line"].append(shot.line_error_degrees)
            if shot.turn != "unknown":
                # Curling left: right of broom is outside; curling right: left is outside.
                group["wide"].append(shot.line_error_degrees * (1 if shot.turn == "left" else -1))
            else:
                group["missing"]["turn_for_narrow_wide"] += 1
        else:
            group["missing"]["line_or_calibration"] += 1
        if shot.swept_seconds is not None:
            group["sweeping"].append(shot.swept_seconds)
    # Pool both turns within an otherwise matched cohort so consistent outside/inside
    # errors remain visible even when they alternate between left and right.
    pooled = {}
    for group in groups.values():
        key = tuple(group[k] for k in ("participant", "call", "direction", "calibration", "line_calibration"))
        summary = pooled.setdefault(
            key,
            {
                **{
                    k: group[k]
                    for k in ("participant", "call", "direction", "calibration", "line_calibration")
                },
                "turn_counts": Counter(),
                "shot_ids": [],
                "missing": Counter(),
                **{k: [] for k in ("weight", "line", "wide", "sweeping")},
            },
        )
        summary["turn_counts"][group["turn"]] += len(group["shot_ids"])
        summary["shot_ids"].extend(group["shot_ids"])
        summary["missing"].update(group["missing"])
        for field in ("weight", "line", "wide", "sweeping"):
            summary[field].extend(group[field])
    rows, summaries = [], []
    for group in groups.values():
        for field in ("weight", "line", "wide", "sweeping"):
            group[field] = distribution(group[field])
        group["missing"] = dict(group["missing"])
        rows.append(group)
    for summary in pooled.values():
        for field in ("weight", "line", "wide", "sweeping"):
            summary[field] = distribution(summary[field])
        summary["missing"] = dict(summary["missing"])
        summary["turn_counts"] = dict(summary["turn_counts"])
        summaries.append(summary)
    return {
        "version": VERSION,
        "total_shots": len(document.shots),
        "excluded": dict(excluded),
        "groups": rows,
        "summaries": summaries,
        "definitions": {
            "weight": "Measured / target speed at the same calibrated reference plane; 1 is target",
            "line": "Degrees right-positive from called broom, viewed from delivery hack",
            "wide": "Degrees outside-positive, inside-negative relative to observed curl direction",
            "spread": "Sample standard deviation, not confidence interval; null for fewer than two shots",
            "sweeping": "Observed seconds; no inferred force or unswept outcome",
            "participant": "Only confirmed game-local actor assignments combine clip-local participants",
        },
    }
