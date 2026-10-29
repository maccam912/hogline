"""Read-only end reconstruction plus bounded, versioned cached house-score analysis."""

import hashlib
import math
import time
from uuid import NAMESPACE_URL, uuid5

import cv2
import numpy as np
from PIL import Image
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.house_states import house_states_in_session
from hogline.analytics.ice_reviews import ice_reviews_in_session
from hogline.analytics.play import play_in_windows
from hogline.analytics.scorecards import scorecard_observations_in_session, select_inputs
from hogline.db import AnalysisWindow, Evidence, FrameObservation, GameRow
from hogline.domain.catalog import source_or_error
from hogline.domain.provenance import observation_manifest, resolve_observation_manifests
from hogline.perception.broadcast import broadcast_catalog_in_session
from hogline.perception.house_score import VERSION, inspect_house_score
from hogline.perception.pipeline import analyze_frame
from hogline.perception.scoreboard import persistent_board_claims
from hogline.perception.stones import VERSION as DETECTOR_VERSION
from hogline.perception.visibility import VERSION as VISIBILITY_VERSION
from hogline.reconstruction.ends import reconstruct_ends
from hogline.reconstruction.scorecards import reconstruct_scorecards
from hogline.reconstruction.scoring import reconcile_scores

SCORE_VERSION = f"{VERSION}:{DETECTOR_VERSION}:{VISIBILITY_VERSION}"


def house_scores_in_session(session):
    output = []
    for row in session.scalars(
        select(FrameObservation)
        .where(FrameObservation.model_version == SCORE_VERSION)
        .order_by(FrameObservation.id)
    ):
        document = row.document
        if (
            document["id"] != row.id
            or document["evidence_id"] != row.evidence_id
            or digest({k: v for k, v in document.items() if k != "sha256"}) != document["sha256"]
        ):
            raise ValueError("House-score observation checksum/reference mismatch")
        output.append(document)
    return output


def analyze_house_scores(db, source_id, limit=24, max_seconds=30):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 120:
        raise ValueError("House-score frame limit must be between one and 120")
    if not math.isfinite(max_seconds) or not 0 < max_seconds <= 120:
        raise ValueError("House-score time budget must be positive and at most 120 seconds")
    deadline = time.monotonic() + max_seconds
    with db.snapshot_session() as session:
        source_or_error(session, source_id)
        frames = list(
            session.scalars(
                select(Evidence)
                .where(Evidence.source_id == source_id)
                .order_by(Evidence.document["source_seconds"].as_float(), Evidence.id)
            )
        )
        done = {
            r.evidence_id
            for r in session.scalars(
                select(FrameObservation).where(FrameObservation.model_version == SCORE_VERSION)
            )
        }
    pending = [f for f in frames if f.id not in done and f.document.get("acquisition_tier") != "coarse"]
    output, skipped = [], []
    for frame in pending[:limit]:
        if time.monotonic() >= deadline:
            break
        with db.snapshot_session(write=True) as session:
            detector = analyze_frame(session, frame, db.settings.data_dir)
            resolved = resolve_observation_manifests(session, [detector])[0]
            manifest = observation_manifest(resolved)
            if not manifest or not frame.document.get("profile_sha256"):
                skipped.append({"evidence_id": frame.id, "reason": "unresolved_acquisition_manifest_or_crop"})
                continue
            composite = frame.document["image"]
            composite_path = (db.settings.data_dir / composite["path"]).resolve()
            if (
                not composite_path.is_relative_to((db.settings.data_dir / "evidence").resolve())
                or not composite_path.is_file()
                or hashlib.sha256(composite_path.read_bytes()).hexdigest() != composite["sha256"]
            ):
                raise ValueError("House-score original frame is missing or corrupt")
            with Image.open(composite_path) as image:
                rgb_sha = hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()
            views = {}
            for view, measurements in detector["views"].items():
                asset = frame.document["crops"][view]
                path = (db.settings.data_dir / asset["path"]).resolve()
                if (
                    not path.is_relative_to((db.settings.data_dir / "evidence").resolve())
                    or not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]
                ):
                    raise ValueError("House-score source crop is missing or corrupt")
                with Image.open(path) as image:
                    views[view] = inspect_house_score(np.asarray(image.convert("RGB")), measurements)
            identifier = str(uuid5(NAMESPACE_URL, f"{frame.id}:{SCORE_VERSION}"))
            document = {
                "id": identifier,
                "schema_version": "hogline-house-score-observation-v1",
                "model_version": SCORE_VERSION,
                "opencv_version": cv2.__version__,
                "detector_observation_id": detector["id"],
                "detector_observation_sha256": digest(detector),
                "evidence_id": frame.id,
                "source_id": source_id,
                "source_metadata_sha256": manifest,
                "profile_sha256": frame.document["profile_sha256"],
                "source_seconds": frame.document["source_seconds"],
                "decoded_rgb_sha256": rgb_sha,
                "pts": frame.document["pts"],
                "time_base_num": frame.document["time_base_num"],
                "time_base_den": frame.document["time_base_den"],
                "youtube_url": frame.document.get("youtube_url"),
                "input_crops": {v: frame.document["crops"][v] for v in views},
                "views": views,
            }
            document["sha256"] = digest(document)
            existing = session.get(FrameObservation, identifier)
            if existing is None:
                session.add(
                    FrameObservation(
                        id=identifier, evidence_id=frame.id, model_version=SCORE_VERSION, document=document
                    )
                )
            else:
                document = existing.document
            output.append(document)
    return {
        "observations": output,
        "processed_frames": len(output),
        "skipped_frames": skipped,
        "remaining_frames": len(pending) - len(output),
        "model_version": SCORE_VERSION,
        "status": "complete" if len(output) == len(pending) else "partial",
    }


def source_end_timeline(db, source_id, start=None, end=None):
    if (start is None) != (end is None):
        raise ValueError("Both scope bounds are required")
    if start is not None and (not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end):
        raise ValueError("End timeline scope must have finite ordered nonnegative bounds")
    with db.snapshot_session() as session:
        source_or_error(session, source_id)
        broadcast = broadcast_catalog_in_session(session, db.settings.data_dir)
        windows = [
            {**w.document, "id": w.id, "source_id": w.source_id}
            for w in session.scalars(select(AnalysisWindow).where(AnalysisWindow.source_id == source_id))
        ]
        if start is not None:
            windows = [
                w
                for w in windows
                if start <= w["observed_first_seconds"] and w["observed_last_seconds"] < end
            ]
        play = play_in_windows(db.settings.data_dir, windows)
        result = reconstruct_ends(
            source_id,
            broadcast["observations"],
            house_states_in_session(session, full=True) + play["results"],
            house_scores_in_session(session),
            (start, end) if start is not None else None,
        )
        eligible = [
            o
            for o in broadcast["observations"]
            if o["source_id"] == source_id and (start is None or start <= o["source_seconds"] < end)
        ]
        result["partial_score_proposals"] = persistent_board_claims(eligible)
        inspected = scorecard_observations_in_session(session, source_id)
        result["scorecard_reconstruction"] = reconstruct_scorecards(
            source_id,
            select_inputs(source_id, eligible, inspected),
            (start, end) if start is not None else None,
        )
        result["scorecard_inspected_frames"] = len(inspected)
        result["reconciled_score_history"] = reconcile_scores(
            result["scorecard_reconstruction"], ice_reviews_in_session(session, source_id)
        )
        result["play_input_exclusions"] = play["exclusions"]
        result["sha256"] = digest({k: v for k, v in result.items() if k != "sha256"})
        return result


def game_end_timeline(db, game_id):
    with db.snapshot_session() as session:
        row = session.get(GameRow, game_id)
        if row is None or row.document.get("retired_into"):
            raise ValueError("Active game not found")
        broadcast = broadcast_catalog_in_session(session, db.settings.data_dir)
        states = house_states_in_session(session, full=True)
        scores = house_scores_in_session(session)
        game = row.document
        window_rows = list(session.scalars(select(AnalysisWindow)))
        selected = [
            {**w.document, "id": w.id, "source_id": w.source_id}
            for w in window_rows
            if any(
                w.source_id == s["source_id"]
                and s["start_seconds"] <= w.document["observed_first_seconds"]
                and w.document["observed_last_seconds"] < s["end_seconds"]
                for s in game["spans"]
            )
        ]
        play = play_in_windows(db.settings.data_dir, selected)
        states += play["results"]
        # Source timebases never become a guessed shared clock across continuations.
        parts = [
            reconstruct_ends(
                span["source_id"],
                broadcast["observations"],
                states,
                scores,
                (span["start_seconds"], span["end_seconds"]),
            )
            for span in game["spans"]
        ]
        confirmed = [e for e in game["ends"] if e["acceptance"] == "human_confirmed"]
        numbers = sorted(e["official_number"] for e in confirmed if e["official_number"] is not None)
        count = (
            len(numbers)
            if game["acceptance"] == "human_confirmed"
            and game["completion"] != "unknown"
            and game["coverage"] == "complete"
            and len(numbers) == len(game["ends"])
            and numbers == list(range(1, len(numbers) + 1))
            and numbers
            else None
        )
        document = {
            "schema_version": "hogline-game-end-timeline-v1",
            "game_id": game_id,
            "game_revision": row.revision,
            "game_document_sha256": digest(game),
            "source_timelines": parts,
            "play_input_exclusions": play["exclusions"],
            "reviewed_ends": confirmed,
            "ends_played": count,
            "scheduled_ends": None,
            "game_completion": game["completion"],
            "result": game["result"],
            "notice": "Source spans remain separate until their clocks and overlap are verified.",
        }
        return {"sha256": digest(document), **document}
