"""Target missed-delivery review using sweeping observations inside order gaps."""

from hogline.adapters.sources import digest
from hogline.db import Job, SkillSequence
from hogline.domain.skills import SkillDocument, read_skills
from hogline.orchestration.skills import SequenceRequest, enqueue_sequence, sequences_in_session
from hogline.reconstruction.skill_candidates import delivery_candidates
from hogline.reconstruction.skill_order import order_report


def gap_search_plan(session, lead, sequences):
    opposite = "yellow" if lead["side"] == "red" else "red"
    hints = []
    for doc in sequences:
        if doc.get("stale") or doc["source_id"] != lead["source_id"]:
            continue
        job = session.get(Job, doc["job_id"])
        if job is None or digest(job.document.get("profile")) != doc.get("profile_sha256"):
            continue
        layout = job.document["profile"]
        for activity in doc["proposal"].get("activities", []):
            if activity["action"] != "sweeping" or activity["stone_color"] != opposite:
                continue
            seconds = doc["samples"][activity["first_frame"]]["source_seconds"]
            if not lead["start_seconds"] < seconds < lead["end_seconds"]:
                continue
            view = doc["request"].get("view", "composite")
            if view == "composite":
                ax, ay, bx, by = activity["bbox"]
                matches = [
                    name
                    for name in ("perspective_left", "perspective_right")
                    if layout["views"][name][0] <= ax < bx <= layout["views"][name][2]
                    and layout["views"][name][1] <= ay < by <= layout["views"][name][3]
                ]
                if len(matches) != 1:
                    continue
                view = matches[0]
            if view not in ("perspective_left", "perspective_right"):
                continue
            start, end = max(lead["start_seconds"], seconds - 8), min(lead["end_seconds"], seconds + 4)
            if end - start < 2:
                continue
            request = SequenceRequest(
                source_id=lead["source_id"],
                start_seconds=start,
                end_seconds=end,
                interval_seconds=max(0.25, (end - start) / 16),
                view=view,
            )
            sequence = session.get(SkillSequence, doc["id"])
            hints.append(
                {
                    "request": request.model_dump(),
                    "anchor_sequence_id": doc["id"],
                    "anchor_sha256": sequence.sha256,
                    "activity_tag": activity["actor_tag"],
                    "sweep_start_seconds": seconds,
                    "basis": f"Visible sweeping near a {opposite} stone inside a same-color gap; "
                    "search preceding frames for a delivery. This is not a missing-shot assertion.",
                }
            )
    return min(hints, key=lambda h: (h["sweep_start_seconds"], h["anchor_sequence_id"])) if hints else None


def enqueue_gap_search(db, game_id, lead_id):
    with db.snapshot_session() as session:
        review = read_skills(session, game_id)
        if review["stale"]:
            raise ValueError("Review changed game/source evidence before searching order gaps")
        document = SkillDocument.model_validate(review["document"])
        sequences = sequences_in_session(session, game_id)
        candidates = delivery_candidates(sequences, document.shots)
        report = order_report(candidates, document)
        lead = next((g for g in report["search_leads"] if g["id"] == lead_id), None)
        if lead is None:
            raise ValueError("Order gap is no longer present; reload")
        plan = gap_search_plan(session, lead, sequences)
        if plan is None:
            raise ValueError("No supported sweeping cue for a focused delivery search")
    return {
        "job": enqueue_sequence(db, game_id, SequenceRequest.model_validate(plan["request"])),
        "plan": plan,
    }
