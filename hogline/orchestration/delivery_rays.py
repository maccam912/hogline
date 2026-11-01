"""Bounded local native-frame analysis; no LLM calls or strict shot acceptance."""

import hashlib
import json
import math
import subprocess
from fractions import Fraction
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from pydantic import Field
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.config import profile
from hogline.db import Evidence, SkillDeliveryRay
from hogline.domain.catalog import source_or_error
from hogline.domain.models import Model
from hogline.domain.skill_estimates import estimate_report, latest_revision
from hogline.media.frames import EvidenceBudget, capture, observe, resolve_media
from hogline.media.windows import decode_native_crop
from hogline.perception.delivery_ray import (
    DIMENSION_REFERENCE,
    VERSION,
    directional_tracks,
    fit_ray,
    house_geometry,
    yellow_leading_edges,
)
from hogline.perception.stones import RawHSVHandleDetector


class DeliveryRayRequest(Model):
    candidate_id: str
    # Camera geometry/direction are explicit operator choices, not inferred from names or end parity.
    origin_view: Literal["house_top", "house_bottom"]
    forward_sign: Literal[-1, 1]
    reference_seconds: float = Field(ge=0, allow_inf_nan=False)
    detector: Literal["yellow_handle", "yellow_leading_edge"] = "yellow_handle"


def probe_native_media(media):
    raw = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", media],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    )
    stream = raw["streams"][0]
    if abs(float(stream.get("start_time", 0))) > 0.001:
        raise ValueError("This native method requires a recording timeline starting at zero")
    return {
        "fps": float(Fraction(stream["avg_frame_rate"])),
        "width": stream["width"],
        "height": stream["height"],
    }


def decoded_rows(media, lo, hi, bounds, sign, detector, fps):
    rows = []
    for im, tm in decode_native_crop(media, lo, hi, bounds, fps=fps):
        rgb = np.asarray(im)
        detections = (
            yellow_leading_edges(rgb, sign)
            if detector == "yellow_leading_edge"
            else [
                d
                for d in RawHSVHandleDetector().detect(rgb)
                if d["color"] == "yellow" and 40 < d["center_px"][0] < rgb.shape[1] - 40
            ]
        )
        rows.append(
            {
                "t": tm["pts"] * tm["time_base_num"] / tm["time_base_den"],
                **tm,
                "crop_sha256": hashlib.sha256(im.tobytes()).hexdigest(),
                "detections": detections,
            }
        )
    return rows


def analyze_delivery_ray(db, game_id, request):
    report = estimate_report(db, game_id)
    if report["stale"]:
        raise ValueError("Refresh the player report before measuring its deliveries")
    shot = next(
        (d for p in report["players"] for d in p["deliveries"] if d["candidate_id"] == request.candidate_id),
        None,
    )
    if shot is None or shot["stone_color"] != "yellow":
        raise ValueError("Select an attributed yellow delivery in this report")
    previous = shot.get("delivery_ray")
    if previous and previous.get("version") == VERSION and previous.get("request") == request.model_dump():
        return {
            "id": previous["id"],
            "cached": True,
            "document": {k: v for k, v in previous.items() if k != "id"},
        }
    with db.snapshot_session() as session:
        revision = latest_revision(session, game_id)
        candidate = revision.document.get("independent_candidates", {}).get(request.candidate_id)
        if candidate is None:
            from hogline.domain.skill_estimates import inputs

            candidate = inputs(session, game_id)[3][request.candidate_id]
        source = source_or_error(session, candidate["source_id"])
    handle = resolve_media(source)
    if handle["format_id"] not in ("verified-post-live", "local-original"):
        raise ValueError("Native rays require checksum-verified local footage")
    # This native path currently supports the 60 Hz Wausau composite only.
    info = probe_native_media(handle["uri"])
    fps = info["fps"]
    if not math.isclose(fps, 60, abs_tol=0.001):
        raise ValueError("This native ray method currently requires a verified 60 Hz source")
    layout = profile()
    bounds = [
        round(v * size)
        for v, size in zip(
            layout["views"][request.origin_view], [info["width"], info["height"]] * 2, strict=True
        )
    ]
    if info["width"] != 1280 or info["height"] != 720 or bounds[2] - bounds[0] != 320:
        raise ValueError("Verify the overhead camera crops for this recording first")
    lo, hi = max(0, shot["seconds"] - 3), shot["seconds"] + 21
    coarse = decoded_rows(handle["uri"], lo, hi, bounds, request.forward_sign, request.detector, 5)
    tracks = directional_tracks(coarse, request.forward_sign, max_gap=0.45)
    if not tracks:
        raise ValueError("No continuous house pass found; review the native frames or another detector")
    lo, hi = max(0, tracks[0][0]["t"] - 0.7), tracks[0][-1]["t"] + 0.7
    if hi - lo > 12:
        raise ValueError("Ambiguous pass exceeds the native frame budget")
    rows = decoded_rows(handle["uri"], lo, hi, bounds, request.forward_sign, request.detector, 60)
    return publish_native_ray(db, report, shot, source, handle, request, rows, bounds, source_fps=fps)


def publish_native_ray(db, report, shot, source, handle, request, rows, bounds, *, source_fps=60):
    """Shared publication path; cached decoded rows are always checked against native parents.

    Native per-point pixel hashes, PTS, source hash and selected exact-frame parents
    remain retained; all other native crops can be regenerated from the original.
    """
    sign = request.forward_sign
    tracks = directional_tracks(rows, sign, max_gap=0.12)
    if not tracks:
        raise ValueError("No native continuous yellow track; no ray was inferred")
    # Broadcast borders and partly clipped handles can turn a stone track into a stationary sleeve track.
    margin = 25 if request.detector == "yellow_leading_edge" else 18
    track = [p for p in tracks[0] if margin <= p["pixel"][1] <= bounds[3] - bounds[1] - margin]
    other = "house_top" if request.origin_view == "house_bottom" else "house_bottom"
    if shot.get("receiving_view") != other:
        raise ValueError("Origin/receiving camera pair disagrees with the reviewed delivery")
    layout = profile()
    reference, rt = capture(handle["uri"], request.reference_seconds)
    if reference.size != (1280, 720):
        raise ValueError("Camera layout requires the verified 1280 by 720 composite")

    def crop(im, view):
        return im.crop(
            tuple(round(v * s) for v, s in zip(layout["views"][view], [1280, 720] * 2, strict=True))
        )

    geometry = house_geometry(np.asarray(crop(reference, request.origin_view)))
    receiving_geometry = house_geometry(np.asarray(crop(reference, other)))
    result = fit_ray(
        track,
        geometry,
        sign,
        source_fps=source_fps,
        pixel_sensitivity=3 if request.detector == "yellow_leading_edge" else 1.5,
    )
    lookup = {v["pts"]: v for v in rows}
    selected = [track[0], track[len(track) // 2], track[-1]]
    split = result["house_split"]
    for name in ("entry", "exit"):
        if split[name] and not split[name]["extrapolated"]:
            selected.append(next(p for p in track if p["pts"] == split[name]["frame_after_pts"]))
    budget = EvidenceBudget(db.settings.data_dir, 24, db.settings.max_cache_mb)
    captured = {}

    def retain(image, timing):
        doc, _ = observe(image, timing, source, db.settings.data_dir, budget=budget)
        doc["id"] = str(uuid5(NAMESPACE_URL, f"delivery-ray-frame:{doc['evidence_key']}"))
        captured[doc["id"]] = doc
        return doc["id"]

    reference_id = retain(reference, rt)
    ids = {}
    for p in {v["pts"]: v for v in selected}.values():
        im, tm = capture(handle["uri"], p["t"] - 0.00001)
        row = lookup[p["pts"]]
        if (
            tm["pts"] != p["pts"]
            or hashlib.sha256(im.crop(bounds).tobytes()).hexdigest() != row["crop_sha256"]
        ):
            raise ValueError("Selected native crop disagrees with its exact source frame")
        ids[p["pts"]] = retain(im, tm)
    for p in result["points"]:
        r = lookup[p["pts"]]
        p.update({k: r[k] for k in ("time_base_num", "time_base_den", "crop_sha256")})
    # Receiving background is synchronous with the first native observation: broom remains inspectable.
    document = {
        **result,
        "game_id": report["game_id"],
        "candidate_id": request.candidate_id,
        "review_sha256": report["review_sha256"],
        "source_id": source["id"],
        "source_metadata_sha256": source["metadata_sha256"],
        "media_sha256": handle["sha256"],
        "request": request.model_dump(),
        "origin_view": request.origin_view,
        "receiving_view": other,
        "geometry": geometry,
        "receiving_geometry": receiving_geometry,
        "geometry_reference_id": reference_id,
        "camera_transfer": "same_image_x_fixed_wausau_composite_assumption",
        "camera_notice": "Nominal rings and a fixed camera reference; lens distortion, "
        "handle height and camera drift are not calibrated.",
        "origin_frame_id": ids[track[len(track) // 2]["pts"]],
        "receiving_frame_id": ids[track[0]["pts"]],
        "native_frame_ids": [{"pts": p, "id": i} for p, i in ids.items()],
        "frame_pins": {i: digest(d) for i, d in captured.items()},
        "dimension_reference": DIMENSION_REFERENCE,
        "weight_prediction": {
            "status": "unavailable",
            "reason": "House passage may still be hand-controlled. "
            "No validated house-split-to-resting-distance model exists.",
        },
        "observed_weight": {
            "status": "light_of_tee" if shot["finish"] == "in_front_of_rings" else "unresolved",
            "basis": "Reviewed front-of-rings placement; this is observed, not predicted, "
            "and may be an intended guard.",
        },
        "acceptance": "assistant_estimate",
    }
    plan_key = digest(
        {
            "version": VERSION,
            "report": report["review_sha256"],
            "request": request.model_dump(),
            "source": source["metadata_sha256"],
            "media": handle["sha256"],
        }
    )
    with db.snapshot_session(write=True) as session:
        current = latest_revision(session, report["game_id"])
        if (
            current.sha256 != report["review_sha256"]
            or source_or_error(session, source["id"])["metadata_sha256"] != source["metadata_sha256"]
        ):
            raise ValueError("Report or source changed during native analysis")
        existing = session.scalar(select(SkillDeliveryRay).where(SkillDeliveryRay.plan_key == plan_key))
        if existing:
            if digest(existing.document) != existing.sha256:
                raise ValueError("Delivery-ray checksum mismatch")
            return {"id": existing.id, "cached": True, "document": existing.document}
        # Resolve deduplicated evidence IDs before making immutable document pins.
        for i, d in list(captured.items()):
            old = session.scalar(select(Evidence).where(Evidence.evidence_key == d["evidence_key"]))
            if old:
                for key in ("origin_frame_id", "receiving_frame_id", "geometry_reference_id"):
                    if document[key] == i:
                        document[key] = old.id
                for f in document["native_frame_ids"]:
                    if f["id"] == i:
                        f["id"] = old.id
                document["frame_pins"].pop(i)
                document["frame_pins"][old.id] = digest(old.document)
            else:
                session.add(
                    Evidence(id=i, source_id=source["id"], evidence_key=d["evidence_key"], document=d)
                )
        row = SkillDeliveryRay(
            game_id=report["game_id"],
            candidate_id=request.candidate_id,
            plan_key=plan_key,
            sha256=digest(document),
            document=document,
        )
        session.add(row)
        session.flush()
        return {"id": row.id, "cached": False, "document": document}
