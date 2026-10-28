"""Display projections of durable job state; progress never implies reconstruction accuracy."""

PHASES = ["survey", "boards", "detail", "detail_boards", "reconstruct"]
LABELS = {
    "survey": "Surveying the recording",
    "boards": "Reading scoreboards",
    "detail": "Inspecting game and end evidence",
    "detail_boards": "Reading detailed score evidence",
    "reconstruct": "Assembling games, ends and scores",
    "scan": "Scanning for activity",
    "delivery_detail": "Refining delivery evidence",
    "ocr": "Reading scoreboards",
}


def analysis_progress(document, units, jobs, executions, state):
    phase = document.get("phase", "scan")
    whole = document.get("campaign_purpose") == "recording_analysis"
    if whole and phase == "survey":
        ids = {u["job_id"] for u in units if u["kind"] == "scan"}
    elif whole:
        ids = {j for p in document.get("phase_history", []) if p["phase"] == phase for j in p["job_ids"]}
    else:
        stage_history = [p for p in document.get("phase_history", []) if p["phase"] == phase]
        ids = (
            {j for p in stage_history for j in p["job_ids"]}
            if stage_history
            else {u["job_id"] for u in units}
        )
    selected = [u for u in units if u["job_id"] in ids]
    completed = sum(u["state"] == "succeeded" for u in selected)
    total = len(selected)
    fraction = sum(
        1
        if u["state"] == "succeeded"
        else min(0.99, max(0, jobs[u["job_id"]].document.get("progress", 0)))
        if u["state"] == "running"
        else 0
        for u in selected
    )
    if whole and phase in ("boards", "detail_boards"):
        pages = [p for p in document["phase_history"] if p["phase"] == phase]
        total = max(total, pages[0].get("pending_eligible_frames", total) if pages else total)
    stage_fraction = fraction / total if total else 0
    overall = ((PHASES.index(phase) + stage_fraction) / len(PHASES)) if whole and phase in PHASES else None
    if state == "completed":
        overall = 1
    running = [e for e in executions if e.state == "running"]
    starts = [e.document["started_at"] for e in executions if e.document.get("started_at")]
    return {
        "phase": phase,
        "phase_label": LABELS.get(phase, "Processing recording"),
        "stage_index": PHASES.index(phase) + 1 if whole and phase in PHASES else None,
        "stage_count": len(PHASES) if whole else None,
        "completed_tasks": completed,
        "total_tasks": total,
        "stage_fraction": stage_fraction,
        "overall_fraction": overall,
        "processing_elapsed_seconds": document.get("spent_worker_seconds", 0),
        "started_at": min(starts) if starts else None,
        "live_updated_at": (
            running[0].document.get("updated_at", running[0].document.get("started_at")) if running else None
        ),
        "running_jobs": sum(u["state"] == "running" for u in units),
        "queued_jobs": sum(u["state"] == "queued" for u in units),
        "failed_jobs": sum(u["state"] == "failed" for u in units),
    }
