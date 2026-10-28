import numpy as np

VERSION = "sparse-panel-motion-v2"


def classify(previous, current):
    brightness = float(np.mean([np.mean(v) for v in current.values()]))
    if brightness < 18:
        return {"state": "dark_candidate", "brightness": brightness, "motion": None}
    if previous is None:
        return {"state": "unknown", "brightness": brightness, "motion": None}
    # Comparing camera panels excludes the changing center advertisement. This remains
    # a heuristic: adjacent-sheet movement and maintenance can still trigger it.
    changes = {name: float(np.mean(np.abs(frame - previous[name])) / 255) for name, frame in current.items()}
    motion = max(changes.values())
    return {
        "state": "activity_candidate" if motion >= 0.025 else "quiet_candidate",
        "brightness": brightness,
        "motion": motion,
        "panel_motion": changes,
    }


def propose(samples, start, end, interval, quiet_gap=300):
    active = [s for s in samples if s["state"] == "activity_candidate"]
    groups = []
    for item in active:
        if not groups or item["seconds"] - groups[-1][-1]["seconds"] > quiet_gap:
            groups.append([])
        groups[-1].append(item)
    return [
        {
            "start_seconds": max(start, g[0]["seconds"] - interval),
            "end_seconds": min(end, g[-1]["seconds"] + interval),
            "boundary_uncertainty_seconds": interval,
            "evidence_ids": [g[0]["evidence_id"], g[-1]["evidence_id"]],
            "reason": "possible_new_game",
            "acceptance": "proposed",
        }
        for g in groups
    ]
