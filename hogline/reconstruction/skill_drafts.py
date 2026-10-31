"""Prepare review drafts without accepting observations or overwriting operator decisions."""

from hogline.domain.skills import Shot


def candidate_review(candidate, shots):
    """A read-only suggestion: saving still requires the normal revisioned review route."""
    if candidate["duplicate_review"]:
        return {"status": "needs_duplicate_resolution", "draft": None}
    by_id = {shot.id: shot for shot in shots}
    active = candidate["linked_shot_ids"]
    excluded = candidate.get("excluded_shot_ids", [])
    existing = by_id[(active or excluded)[0]] if active or excluded else None
    # A visible release and linked thrower are preferable; shortest observation is a
    # deterministic tie-breaker, not a confidence score or verified release timing.
    observations = sorted(
        candidate["observations"],
        key=lambda o: (
            o["release_seconds"] is None,
            o["thrower_actor_id"] is None,
            o["end_seconds"] - o["start_seconds"],
            o["created_at"],
            o["sequence_id"],
            o["delivery_index"],
        ),
    )
    primary = observations[0]
    references = list(existing.evidence) if existing else []
    for observation in observations:
        for frame in (observation["first_frame"], observation["last_frame"]):
            ref = f"skill-sequence:{observation['sequence_id']}:frame:{frame}"
            if ref not in references:
                references.append(ref)
    if existing:
        draft = existing.model_copy(update={"evidence": references[:32]})
    else:
        draft = Shot(
            id=candidate["draft_id"],
            source_id=candidate["source_id"],
            seconds=primary["release_seconds"]
            if primary["release_seconds"] is not None
            else primary["start_seconds"],
            side=candidate["stone_color"],
            participant=primary["thrower_actor_id"],
            call=candidate["call"],
            evidence=references[:32],
            note=(
                "Vision proposal from overlapping observations. Time is a proposed release when visible, "
                "otherwise the first observed delivery frame. End, delivery order, continuity and "
                "measurements require review."
            ),
        )
    return {
        "status": ("excluded" if existing.acceptance == "excluded" else "existing") if existing else "new",
        "draft": draft.model_dump(),
        "primary_sequence_id": primary["sequence_id"],
        "release_proposals_seconds": sorted(
            {o["release_seconds"] for o in observations if o["release_seconds"] is not None}
        ),
        "unattached_reference_count": max(0, len(references) - 32),
    }


def candidate_coverage(candidates):
    statuses = [c["review"]["status"] for c in candidates]
    return {
        "candidate_count": len(candidates),
        "unreviewed": statuses.count("new"),
        "with_saved_review": statuses.count("existing"),
        "excluded": statuses.count("excluded"),
        "duplicate_review": statuses.count("needs_duplicate_resolution"),
        "ambiguous_overlap": sum(c["ambiguous_overlap"] for c in candidates),
        "notice": "Candidates are proposed observations, not a complete or verified shot count.",
    }
