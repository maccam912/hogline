"""Evidence-backed estimates never promote themselves into confirmed shot measurements."""

from collections import Counter
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import select

from hogline.adapters.sources import digest, video_link
from hogline.db import Evidence, GameLineup, GameRow, LineupLink, LineupPerson, SkillEstimateRevision
from hogline.domain.catalog import Conflict, source_or_error
from hogline.domain.lineups import checked
from hogline.domain.models import Model
from hogline.domain.skill_scope import compatible_game_scope
from hogline.media.frames import read_image_bytes
from hogline.orchestration.skills import sequences_in_session
from hogline.reconstruction.skill_candidates import delivery_candidates

VERSION = "evidence-backed-player-estimates-v1"


class BroomFeedback(Model):
    called_evidence_id: str
    indicated_evidence_id: str
    view: Literal["house_top", "house_bottom"]
    called_x: float = Field(ge=0, le=1)
    indicated_x: float = Field(ge=0, le=1)
    ring_left_x: float = Field(ge=0, le=1)
    ring_right_x: float = Field(ge=0, le=1)
    point_uncertainty: float = Field(default=0.03, ge=0.005, le=0.15)
    interpretation: Literal["possible_feedback", "assistant_interpreted_feedback"] = "possible_feedback"
    basis: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def ring_width(self):
        if self.ring_right_x - self.ring_left_x < 0.25:
            raise ValueError("Broom comparison needs a visible outer-ring diameter")
        return self


class EstimatedDelivery(Model):
    candidate_id: str
    person_id: str
    # Independently observed deliveries fill scan gaps without inventing vision jobs.
    source_id: str | None = None
    observed_start_seconds: float | None = Field(default=None, ge=0)
    observed_end_seconds: float | None = Field(default=None, ge=0)
    basis: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=12)
    end: int | None = Field(default=None, ge=1, le=30)
    summary: str = Field(default="", max_length=3000)
    finish: Literal["unknown", "inside_rings", "in_front_of_rings", "outside_rings", "contact_observed"] = (
        "unknown"
    )
    receiving_view: Literal["house_top", "house_bottom"] | None = None
    feedback: BroomFeedback | None = None


class EstimatedFinding(Model):
    person_id: str
    text: str = Field(min_length=1, max_length=3000)
    candidate_ids: list[str] = Field(min_length=1, max_length=24)


class EstimateEdit(Model):
    expected_revision: int = Field(ge=0)
    deliveries: list[EstimatedDelivery] = Field(default_factory=list, max_length=256)
    findings: list[EstimatedFinding] = Field(default_factory=list, max_length=64)
    author: str = Field(default="Assistant review", min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def unique_candidates(self):
        ids = [d.candidate_id for d in self.deliveries]
        if len(ids) != len(set(ids)):
            raise ValueError("A delivery can belong to only one player")
        return self


def roster_in_session(session, game):
    """Use explicit operator links in overlapping lineups, within this game's sources."""
    roster, contexts = {}, {}
    for span in game.document["spans"]:
        source = source_or_error(session, span["source_id"])
        context = source.get("operator_skill_context") or {}
        if context.get("game_id") == game.id:
            contexts[span["source_id"]] = context
        for row in session.scalars(select(GameLineup).where(GameLineup.source_id == span["source_id"])):
            lineup = checked(row)
            if lineup["source_metadata_sha256"] != source["metadata_sha256"] or max(
                lineup["start_seconds"], span["start_seconds"]
            ) >= min(lineup["end_seconds"], span["end_seconds"]):
                continue
            player_ids = {p["id"] for p in lineup["players"]}
            for link in session.scalars(select(LineupLink).where(LineupLink.lineup_id == row.id)):
                decision = checked(link)
                person = session.get(LineupPerson, link.person_id)
                if link.id not in player_ids or person is None:
                    continue
                named = checked(person)
                if context.get("team") and named.get("team") != context["team"]:
                    continue
                entry = roster.setdefault(
                    person.id,
                    {
                        "person_id": person.id,
                        "name": named["name"],
                        "team": named.get("team"),
                        "position": decision.get("position"),
                        "lineup_players": [],
                        "sources": [],
                    },
                )
                entry["lineup_players"].append(
                    {"lineup_id": row.id, "player_id": link.id, "link_sha256": link.sha256}
                )
                if span["source_id"] not in entry["sources"]:
                    entry["sources"].append(span["source_id"])
    return roster, contexts


def latest_revision(session, game_id):
    row = session.scalar(
        select(SkillEstimateRevision)
        .where(SkillEstimateRevision.game_id == game_id)
        .order_by(SkillEstimateRevision.number.desc())
        .limit(1)
    )
    if row and digest(row.document) != row.sha256:
        raise ValueError("Estimated report review checksum mismatch")
    return row


def inputs(session, game_id):
    game = session.get(GameRow, game_id)
    if game is None or game.document.get("retired_into"):
        raise ValueError("Choose an active game")
    roster, contexts = roster_in_session(session, game)
    sequences = sequences_in_session(session, game_id)
    return game, roster, contexts, {c["id"]: c for c in delivery_candidates(sequences)}, sequences


def checked_frame(session, game, evidence_id):
    frame = session.get(Evidence, evidence_id)
    if frame is None:
        raise ValueError("Estimate evidence frame is missing")
    document = frame.document
    source = source_or_error(session, frame.source_id)
    if document.get("source_metadata_sha256") != source["metadata_sha256"]:
        raise ValueError("Estimate evidence source changed")
    if not any(
        s["source_id"] == frame.source_id
        and s["start_seconds"] <= document["source_seconds"] <= s["end_seconds"]
        for s in game.document["spans"]
    ):
        raise ValueError("Estimate evidence must lie inside this game")
    return frame


def reviewed_candidate(session, game, delivery):
    if not delivery.candidate_id.startswith("reviewed-delivery:"):
        raise ValueError("Estimate needs one current, unambiguous delivery candidate")
    start, end = delivery.observed_start_seconds, delivery.observed_end_seconds
    if start is None or end is None or not 0.1 <= end - start <= 30:
        raise ValueError("Independent delivery needs an observed interval of 0.1–30 seconds")
    if not any(
        s["source_id"] == delivery.source_id and s["start_seconds"] <= start < end <= s["end_seconds"]
        for s in game.document["spans"]
    ):
        raise ValueError("Independent delivery must lie inside this game")
    frames = [checked_frame(session, game, eid) for eid in delivery.evidence_ids]
    times = {
        f.document["source_seconds"]
        for f in frames
        if f.source_id == delivery.source_id and start <= f.document["source_seconds"] <= end
    }
    if len(times) < 2:
        raise ValueError("Independent delivery needs at least two original frames during the action")
    return {
        "id": delivery.candidate_id,
        "source_id": delivery.source_id,
        "start_seconds": start,
        "end_seconds": end,
        "stone_color": "unknown",
        "ambiguous_overlap": False,
        "method": "independent-assistant-video-review-v1",
    }


def save_estimates(db, game_id, edit):
    # Snapshot and short transaction: no media decoding, model calls or paid work here.
    with db.snapshot_session(write=True) as session:
        game, roster, contexts, candidates, sequences = inputs(session, game_id)
        previous = latest_revision(session, game_id)
        if (previous.number if previous else 0) != edit.expected_revision:
            raise Conflict("Estimated report changed; reload before saving")
        pins, frame_pins = {}, {}
        independent = {}
        for delivery in edit.deliveries:
            candidate = candidates.get(delivery.candidate_id)
            if candidate is None:
                candidate = reviewed_candidate(session, game, delivery)
                independent[candidate["id"]] = candidate
            person = roster.get(delivery.person_id)
            if candidate is None or candidate["ambiguous_overlap"]:
                raise ValueError("Estimate needs one current, unambiguous delivery candidate")
            if person is None or candidate["source_id"] not in person["sources"]:
                raise ValueError("Estimate needs a named player linked to this recording")
            color = contexts.get(candidate["source_id"], {}).get("stone_color")
            if color not in ("red", "yellow"):
                raise ValueError("Record the team's stone color before attributing deliveries")
            if candidate["stone_color"] not in (color, "unknown"):
                raise ValueError("Delivery stone color conflicts with the named team")
            pins[candidate["id"]] = digest(candidate)
            for eid in delivery.evidence_ids:
                frame = checked_frame(session, game, eid)
                if frame.source_id != candidate["source_id"]:
                    raise ValueError("Delivery evidence belongs to another source")
                if (
                    not candidate["start_seconds"] - 120
                    <= frame.document["source_seconds"]
                    <= (candidate["end_seconds"] + 120)
                ):
                    raise ValueError("Evidence is too far from the delivery")
                frame_pins[eid] = digest(frame.document)
                read_image_bytes(db.settings.data_dir, frame.document["image"])
            if delivery.feedback:
                feedback = delivery.feedback
                if not {feedback.called_evidence_id, feedback.indicated_evidence_id} <= set(
                    delivery.evidence_ids
                ):
                    raise ValueError("Both broom positions need retained evidence")
                called = checked_frame(session, game, feedback.called_evidence_id)
                indicated = checked_frame(session, game, feedback.indicated_evidence_id)
                if not (
                    candidate["start_seconds"] - 120
                    <= called.document["source_seconds"]
                    <= candidate["start_seconds"]
                    < indicated.document["source_seconds"]
                    <= candidate["end_seconds"] + 60
                ):
                    raise ValueError("Keep the original call before delivery and later feedback after it")
        # Do not double count a manually found delivery beside an automatic observation of it.
        all_reviewed = {**candidates, **independent}
        for left_index, left in enumerate(edit.deliveries):
            a = all_reviewed[left.candidate_id]
            for right in edit.deliveries[left_index + 1 :]:
                b = all_reviewed[right.candidate_id]
                if a["source_id"] == b["source_id"] and max(a["start_seconds"], b["start_seconds"]) <= min(
                    a["end_seconds"], b["end_seconds"]
                ):
                    raise ValueError("Reviewed delivery intervals overlap; resolve duplicates")
        assignments = {d.candidate_id: d.person_id for d in edit.deliveries}
        for finding in edit.findings:
            if any(assignments.get(cid) != finding.person_id for cid in finding.candidate_ids):
                raise ValueError("Findings must cite this player's reviewed deliveries")
        document = {
            "version": VERSION,
            "game_revision": game.revision,
            "contexts_sha256": digest(contexts),
            "roster_sha256": digest(roster),
            "candidate_pins": pins,
            "independent_candidates": independent,
            "frame_pins": frame_pins,
            "source_pins": {
                s["source_id"]: source_or_error(session, s["source_id"])["metadata_sha256"]
                for s in game.document["spans"]
            },
            "review": edit.model_dump(exclude={"expected_revision"}),
        }
        row = SkillEstimateRevision(
            game_id=game_id, number=edit.expected_revision + 1, document=document, sha256=digest(document)
        )
        session.add(row)
        session.flush()
        return {"revision": row.number, "sha256": row.sha256}


def feedback_comparison(feedback):
    """A tee-line indication in one fixed view; never an initial-trajectory measurement."""
    value = BroomFeedback.model_validate(feedback)
    diameter = value.ring_right_x - value.ring_left_x
    delta = value.indicated_x - value.called_x
    uncertainty = 2 * value.point_uncertainty
    # Nominal 12-foot diameter. The interval reflects point picking only, not all geometry error.
    scale = 3.6576 / diameter
    return {
        "interpretation": value.interpretation,
        "image_direction": "right" if delta > uncertainty else "left" if delta < -uncertainty else "unclear",
        "nominal_lateral_shift_m": round(delta * scale, 2),
        "point_picking_range_m": [
            round((delta - uncertainty) * scale, 2),
            round((delta + uncertainty) * scale, 2),
        ],
        "basis": value.basis,
        "notice": "Possible skip feedback. Nominal outer-ring scale; image left/right, not wide/narrow. "
        "This is the indicated tee-line difference, not a measured release path or a confidence interval.",
    }


def estimate_report(db, game_id):
    with db.snapshot_session() as session:
        game, roster, contexts, candidates, sequences = inputs(session, game_id)
        row = latest_revision(session, game_id)
        stale_reasons = []
        if row:
            document = row.document
            candidates.update(document.get("independent_candidates", {}))
            if not compatible_game_scope(session, game, document["game_revision"]):
                stale_reasons.append("game_changed")
            if document["contexts_sha256"] != digest(contexts):
                stale_reasons.append("team_context_changed")
            if document["roster_sha256"] != digest(roster):
                stale_reasons.append("named_lineup_changed")
            for sid, sha in document["source_pins"].items():
                if source_or_error(session, sid)["metadata_sha256"] != sha:
                    stale_reasons.append("source_changed")
            for cid, sha in document["candidate_pins"].items():
                if cid not in candidates or digest(candidates[cid]) != sha:
                    stale_reasons.append("delivery_evidence_changed")
            for eid, sha in document["frame_pins"].items():
                frame = session.get(Evidence, eid)
                if frame is None or digest(frame.document) != sha:
                    stale_reasons.append("review_frame_changed")
                else:
                    try:
                        read_image_bytes(db.settings.data_dir, frame.document["image"])
                    except ValueError:
                        stale_reasons.append("review_image_unavailable")
            review = document["review"] if not stale_reasons else {"deliveries": [], "findings": []}
        else:
            review = {"deliveries": [], "findings": []}
        from hogline.domain.delivery_rays import current_rays

        rays = (
            current_rays(session, db.settings.data_dir, game_id, row.sha256)
            if row and not stale_reasons
            else {}
        )
        players = []
        for person in roster.values():
            deliveries = []
            for item in review["deliveries"]:
                if item["person_id"] != person["person_id"]:
                    continue
                candidate = candidates[item["candidate_id"]]
                source = source_or_error(session, candidate["source_id"])
                evidence = []
                for eid in item["evidence_ids"]:
                    frame = session.get(Evidence, eid)
                    evidence.append(
                        {
                            "id": eid,
                            "seconds": frame.document["source_seconds"],
                            "image_url": f"/api/evidence/{eid}/composite",
                            "video_url": video_link(source, frame.document["source_seconds"]),
                        }
                    )
                deliveries.append(
                    {
                        **item,
                        "delivery_ray": rays.get(item["candidate_id"]),
                        "seconds": candidate["start_seconds"],
                        "end_seconds": candidate["end_seconds"],
                        "stone_color": candidate["stone_color"],
                        "video_url": video_link(source, max(0, candidate["start_seconds"] - 5)),
                        "evidence": evidence,
                        "acceptance": "assistant_estimate",
                        "feedback_comparison": feedback_comparison(item["feedback"])
                        if item.get("feedback")
                        else None,
                    }
                )
            players.append(
                {
                    **person,
                    "deliveries": sorted(deliveries, key=lambda d: d["seconds"]),
                    "observed_deliveries": len(deliveries),
                    "finish_observations": dict(Counter(d["finish"] for d in deliveries)),
                    "findings": [f for f in review["findings"] if f["person_id"] == person["person_id"]],
                    "weight_accuracy": None,
                    "release_aim_accuracy": None,
                    "sweeping_effectiveness": None,
                    "sample_status": "limited_examples" if len(deliveries) < 5 else "descriptive_examples",
                }
            )
        order = {"lead": 0, "second": 1, "third": 2, "skip": 3}
        players.sort(key=lambda p: (order.get(p["position"], 4), p["name"]))
        assigned = {d["candidate_id"] for p in players for d in p["deliveries"]}
        return {
            "version": VERSION,
            "game_id": game_id,
            "label": game.document.get("label", "Game"),
            "revision": row.number if row else 0,
            "review_sha256": row.sha256 if row else None,
            "author": row.document["review"]["author"] if row else None,
            "stale": bool(stale_reasons),
            "stale_reasons": sorted(set(stale_reasons)),
            "contexts": contexts,
            "players": players,
            "coverage": {
                "sequence_clips": len(sequences),
                "delivery_candidates": len(candidates),
                "attributed_deliveries": len(assigned),
                "unattributed_candidates": len(candidates) - len(assigned),
                "notice": "Selected observations; neither a complete throw count "
                "nor a shot-success percentage.",
            },
            "notice": "Estimated reports from linked player identities and assistant-reviewed video. "
            "Unknown calls, weight and release aim remain unknown. "
            "Assistant review is not human confirmation.",
        }
