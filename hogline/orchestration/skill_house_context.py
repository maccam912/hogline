"""Bounded, reproducible local analysis of existing survey contact sheets."""

import io
import json
import math
import time
from collections import defaultdict
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from PIL import Image
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.db import GameRow, Job, SkillHouseContext, SkillSequence, now
from hogline.domain.skill_scope import compatible_game_scope
from hogline.media.frames import read_image_bytes
from hogline.orchestration.skills import sequences_in_session
from hogline.perception.shot_sequence import VERSION as SEQUENCE_VERSION
from hogline.perception.stones import HSVHandleDetector
from hogline.perception.visibility import VERSION as VISIBILITY_VERSION
from hogline.perception.visibility import visibility_observation
from hogline.reconstruction.skill_house_context import VERSION, infer_house_context

MAX_SEQUENCES = 128
MAX_FRAMES = 1200
MAX_SECONDS = 30
MAX_RESULT_BYTES = 8 * 1024 * 1024


def inputs_in_session(session, game_id):
    game = session.get(GameRow, game_id)
    if game is None or game.document.get("retired_into"):
        raise ValueError("Choose an active game for house context")
    eligible, excluded = [], []
    for doc in sorted(
        sequences_in_session(session, game_id), key=lambda d: (d["request"]["start_seconds"], d["id"])
    ):
        if doc["request"].get("view", "composite") != "composite":
            continue
        reason = None
        job = session.get(Job, doc["job_id"])
        if doc["stale"]:
            reason = "stale_game_or_source"
        elif doc["version"] not in ("curling-sequence-v3", SEQUENCE_VERSION):
            reason = "unsupported_contact_sheet_version"
        elif job is None or digest(job.document.get("profile")) != doc["profile_sha256"]:
            reason = "missing_or_changed_frozen_profile"
        elif len(eligible) >= MAX_SEQUENCES:
            reason = "sequence_limit"
        if reason:
            excluded.append({"sequence_id": doc["id"], "reason": reason})
            continue
        row = session.get(SkillSequence, doc["id"])
        eligible.append(
            {"id": row.id, "sha256": row.sha256, "document": row.document, "profile": job.document["profile"]}
        )
    return game.revision, eligible, excluded


def crop_reference(sequence, index, view):
    doc, layout = sequence["document"], sequence["profile"]
    if not 0 <= index < len(doc["samples"]):
        raise ValueError("Survey frame is unavailable")
    sample = doc["samples"][index]
    # The retained composite layout is exact only for these source dimensions.
    if (
        sample["width"] < 512
        or sample["height"] < 288
        or sample["width"] * 9 != sample["height"] * 16
        or sample["normalized_crop_bounds"] != [0, 0, 1, 1]
    ):
        raise ValueError("Unsupported retained composite dimensions")
    bounds = layout["views"].get(view, [])
    if (
        view not in ("house_top", "house_bottom")
        or len(bounds) != 4
        or not all(isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 1 for v in bounds)
    ):
        raise ValueError("Unsupported frozen house bounds")
    crop = [round(v * (512 if n % 2 == 0 else 288)) for n, v in enumerate(bounds)]
    if not 0 <= crop[0] < crop[2] <= 512 or not 0 <= crop[1] < crop[3] <= 288:
        raise ValueError("Empty retained house crop")
    x, y = (index % 2) * 512, ((index % 4) // 2) * 320 + 32
    return {
        "sequence_id": sequence["id"],
        "sequence_sha256": sequence["sha256"],
        "frame_index": index,
        "view": view,
        "sheet_index": index // 4,
        "pixel_bounds": [crop[0] + x, crop[1] + y, crop[2] + x, crop[3] + y],
        "resolution": "downsampled_retained_contact_sheet",
    }


def crop_image(root, sequence, reference, cache=None):
    index = reference["sheet_index"]
    cache = cache if cache is not None else {}
    if index not in cache:
        assets = sequence["document"]["sheet_assets"]
        if not 0 <= index < len(assets):
            raise ValueError("Retained survey sheet is unavailable")
        sheet = Image.open(io.BytesIO(read_image_bytes(root, assets[index]))).convert("RGB")
        if sheet.size != (1024, 640):
            raise ValueError("Unsupported retained survey sheet layout")
        cache[index] = sheet
    return cache[index].crop(reference["pixel_bounds"])


def build_context(root, inputs, input_key):
    started = time.monotonic()
    detector = HSVHandleDetector()
    groups = defaultdict(list)
    seen, skipped, frames = set(), [], 0
    time_hashes = defaultdict(set)
    grouped_inputs = []
    for sequence in inputs:
        doc = sequence["document"]
        group_key = digest(
            {
                "source_id": doc["source_id"],
                "source_hash": doc["source_metadata_sha256"],
                "profile": doc["profile_sha256"],
            }
        )
        grouped_inputs.append((group_key, sequence))
        for sample in doc["samples"]:
            time_hashes[(group_key, sample["source_seconds"])].add(sample["decoded_rgb_sha256"])
    for group_key, sequence in grouped_inputs:
        doc = sequence["document"]
        cache = {}
        for index, sample in enumerate(doc["samples"]):
            key = (group_key, sample["source_seconds"], sample["decoded_rgb_sha256"])
            if len(time_hashes[key[:2]]) > 1:
                skipped.append(
                    {
                        "sequence_id": sequence["id"],
                        "frame_index": index,
                        "source_seconds": sample["source_seconds"],
                        "reason": "conflicting_decoded_images_at_source_time",
                    }
                )
                continue
            if key in seen:
                continue
            if frames >= MAX_FRAMES or time.monotonic() - started > MAX_SECONDS:
                raise ValueError("House context exceeds its local frame/time bound; narrow the game coverage")
            seen.add(key)
            try:
                refs = [crop_reference(sequence, index, view) for view in ("house_top", "house_bottom")]
            except ValueError as exc:
                skipped.append({"sequence_id": sequence["id"], "frame_index": index, "reason": str(exc)})
                continue
            crops = [crop_image(root, sequence, ref, cache) for ref in refs]
            for ref, crop in zip(refs, crops, strict=True):
                rgb = np.asarray(crop)
                detected = detector.inspect(rgb)
                groups[group_key].append(
                    {
                        **sample,
                        "view": ref["view"],
                        "sample_index": frames,
                        "source_id": doc["source_id"],
                        "requested_interval_seconds": doc["request"]["interval_seconds"],
                        "crop_width": crop.width,
                        "crop_height": crop.height,
                        **detected,
                        **visibility_observation(rgb),
                        "motion_mean_abs": None,
                        "image_reference": ref,
                    }
                )
            frames += 1
    results = []
    for group_key, rows in groups.items():
        # Overlapping clips can introduce interleaved timestamps. Re-index after sorting.
        indices = {t: i for i, t in enumerate(sorted({r["source_seconds"] for r in rows}))}
        for row in rows:
            row["sample_index"] = indices[row["source_seconds"]]
        rows.sort(key=lambda r: (r["source_seconds"], r["view"]))
        results.append(
            {
                "source_id": rows[0]["source_id"],
                "group_key": group_key,
                **infer_house_context(rows, input_key + group_key),
            }
        )
    return {
        "groups": results,
        "sample_count": frames,
        "skipped_frames": skipped,
        "wall_seconds": round(time.monotonic() - started, 3),
    }


def analyze_house_context(db, game_id):
    with db.snapshot_session() as session:
        revision, inputs, excluded = inputs_in_session(session, game_id)
        manifest = [{"id": s["id"], "sha256": s["sha256"]} for s in inputs]
        context = {
            "game_id": game_id,
            "game_revision": revision,
            "version": VERSION,
            "detector_version": HSVHandleDetector.version,
            "visibility_version": VISIBILITY_VERSION,
            "inputs": manifest,
            "excluded_sequences": excluded,
        }
        key = digest(context)
        existing = session.scalar(select(SkillHouseContext).where(SkillHouseContext.input_key == key))
        if existing:
            if digest(existing.document) != existing.sha256:
                raise ValueError("House context checksum mismatch")
            return {**existing.document, "cached": True}
    if not inputs:
        raise ValueError("No current composite survey images are available")
    result = build_context(db.settings.data_dir, inputs, key)
    doc = {
        **context,
        **result,
        "id": str(uuid5(NAMESPACE_URL, f"{VERSION}:{key}")),
        "input_key": key,
        "created_at": now(),
        "acceptance": "proposed",
    }
    if len(json.dumps(doc).encode()) > MAX_RESULT_BYTES:
        raise ValueError("House context result exceeds its storage bound")
    with db.snapshot_session(write=True) as session:
        game = session.get(GameRow, game_id)
        if game is None or game.revision != revision:
            raise ValueError("Game changed during local analysis; reload")
        _, current, _ = inputs_in_session(session, game_id)
        current_hashes = {s["id"]: s["sha256"] for s in current}
        if any(current_hashes.get(s["id"]) != s["sha256"] for s in manifest):
            raise ValueError("Survey evidence changed during local analysis")
        existing = session.get(SkillHouseContext, doc["id"])
        if existing is None:
            session.add(
                SkillHouseContext(
                    id=doc["id"], game_id=game_id, input_key=key, document=doc, sha256=digest(doc)
                )
            )
        else:
            doc = existing.document
    return {**doc, "cached": False}


def latest_house_context(session, game_id):
    row = session.scalar(
        select(SkillHouseContext)
        .where(SkillHouseContext.game_id == game_id)
        .order_by(SkillHouseContext.created_at.desc())
    )
    if row is None:
        return None
    if digest(row.document) != row.sha256:
        raise ValueError("House context checksum mismatch")
    doc = row.document
    game = session.get(GameRow, game_id)
    _, inputs, _ = inputs_in_session(session, game_id)
    hashes = {s["id"]: s["sha256"] for s in inputs}
    stale = (
        doc["version"] != VERSION
        or doc["detector_version"] != HSVHandleDetector.version
        or doc["visibility_version"] != VISIBILITY_VERSION
        or not compatible_game_scope(session, game, doc["game_revision"])
        or any(hashes.get(s["id"]) != s["sha256"] for s in doc["inputs"])
    )
    return {**doc, "stale": stale, "pending_sequences": len(set(hashes) - {s["id"] for s in doc["inputs"]})}


def house_context_image(db, context_id, group_index, state_index, endpoint):
    if endpoint not in ("first", "last"):
        raise ValueError("Choose first or last context frame")
    with db.snapshot_session() as session:
        row = session.get(SkillHouseContext, context_id)
        if row is None or digest(row.document) != row.sha256:
            raise ValueError("House context evidence missing or changed")
        groups = row.document["groups"]
        if not 0 <= group_index < len(groups) or not 0 <= state_index < len(groups[group_index]["states"]):
            raise ValueError("House context state is unavailable")
        ref = groups[group_index]["states"][state_index][endpoint]["image_reference"]
        sequence = session.get(SkillSequence, ref["sequence_id"])
        if (
            sequence is None
            or sequence.sha256 != ref["sequence_sha256"]
            or digest(sequence.document) != sequence.sha256
        ):
            raise ValueError("Survey evidence changed")
        doc = sequence.document
    image = crop_image(db.settings.data_dir, {"document": doc}, ref)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
