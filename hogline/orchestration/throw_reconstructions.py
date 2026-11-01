"""On-demand, bounded local throw reconstructions with durable jobs and pinned exports."""

import hashlib
import json
import os
import subprocess
import time

from pydantic import Field
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.config import profile
from hogline.db import Job, now
from hogline.domain.catalog import source_or_error
from hogline.domain.models import Model
from hogline.domain.skill_estimates import estimate_report, latest_revision
from hogline.media.frames import resolve_media
from hogline.media.windows import decode_window
from hogline.orchestration.plans import (
    WorkStopped,
    check_execution,
    check_execution_in_session,
    enqueue_in_session,
    finish_execution,
)
from hogline.perception.perspective_rays import register_common_sheet
from hogline.perception.throw_reconstruction import (
    VERSION,
    associate,
    frame_candidates,
    smooth,
    tee_time,
    video_maps,
)

FILES = {"manifest.json", "trajectory.json", "observations.json", "geometry.json", "source-shot.mp4"}
MAX_ARTIFACT_BYTES = 512 * 1024**2


class ReconstructionRequest(Model):
    candidate_ids: list[str] | None = Field(default=None, min_length=1, max_length=256)
    include_clip: bool = False
    retry_failed: bool = False


def artifact_key(report, shot):
    return digest(
        {
            "version": VERSION,
            "review": report["review_sha256"],
            "ray": shot["delivery_ray"],
            "profile": profile()["views"],
        }
    )


def folder(db, game_id, key):
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("Invalid reconstruction key")
    return db.settings.data_dir / "reconstructions" / digest(game_id)[:16] / key


def atomic_json(path, document):
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(document, separators=(",", ":")))
    temporary.replace(path)


def checked_result(db, report, shot):
    if not shot.get("delivery_ray"):
        return None
    key = artifact_key(report, shot)
    root = folder(db, report["game_id"], key)
    path = root / "manifest.json"
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text())
        if manifest["review_sha256"] != report["review_sha256"] or manifest["key"] != key:
            return None
        if manifest["ray_sha256"] != digest(shot["delivery_ray"]):
            return None
        with db.snapshot_session() as session:
            receipts = list(
                session.scalars(
                    select(Job).where(Job.state == "succeeded", Job.document["key"].as_string() == key)
                )
            )
            receipts.sort(key=lambda j: (j.document.get("finished_at", ""), j.id), reverse=True)
            if not receipts or receipts[0].document.get("manifest_sha256") != digest(manifest):
                return None
        if not {"trajectory.json", "observations.json", "geometry.json"} <= manifest["artifacts"].keys():
            return None
        for name, asset in manifest["artifacts"].items():
            target = root / name
            if name not in FILES or target.resolve().parent != root.resolve():
                return None
            # Hash the small measurement files on every read; clip hashes are
            # checked when generated/served, rather than for every animation poll.
            if not target.is_file() or target.stat().st_size != asset["bytes"]:
                return None
            if (
                name != "source-shot.mp4"
                and hashlib.sha256(target.read_bytes()).hexdigest() != asset["sha256"]
            ):
                return None
        return manifest
    except (ValueError, KeyError, OSError):
        return None


def catalog(db, game_id):
    report = estimate_report(db, game_id)
    with db.snapshot_session() as session:
        jobs = list(session.scalars(select(Job).where(Job.document["game_id"].as_string() == game_id)))
        states = {}
        for job in sorted(jobs, key=lambda j: (j.document.get("created_at", ""), j.id)):
            if job.document.get("kind") == "throw_reconstruction":
                key = (job.document["key"], job.document["include_clip"])
                states[key] = {
                    "job_id": job.id,
                    "state": job.state,
                    "error": job.document.get("error"),
                    "progress": job.document.get("progress", 0),
                }
    rows = []
    for player in report["players"]:
        for index, shot in enumerate(player["deliveries"]):
            ray = shot.get("delivery_ray")
            row = {
                "candidate_id": shot["candidate_id"],
                "person_id": player["person_id"],
                "player": player["name"],
                "shot_number": index + 1,
                "end": shot["end"],
                "source_seconds": shot["seconds"],
                "finish": shot["finish"],
                "state": "not_generated",
            }
            if ray and ray.get("perspectives") and not report["stale"]:
                key = artifact_key(report, shot)
                row.update(
                    key=key,
                    direction=ray["origin_view"],
                    **states.get((key, False), {}),
                    clip_job=states.get((key, True)),
                )
                result = checked_result(db, report, shot)
                if result:
                    row.update(
                        state="ready",
                        result=result,
                        trajectory=json.loads((folder(db, game_id, key) / "trajectory.json").read_text()),
                    )
                elif row["state"] == "succeeded":
                    row.update(
                        state="artifact_unavailable", error="Saved reconstruction changed; regenerate it"
                    )
            else:
                row.update(state="unavailable", error="Native ray and reviewed perspective geometry required")
            rows.append(row)
    return {
        "game_id": game_id,
        "label": report["label"],
        "players": [{k: p[k] for k in ("person_id", "name", "position")} for p in report["players"]],
        "throws": rows,
        "stale": report["stale"],
        "review_sha256": report["review_sha256"],
        "alignment": "delivery_tee_crossing",
        "directions": "rotate opposite deliveries 180 degrees",
        "notice": "All throws start at their observed delivery-tee crossing. Real elapsed time is preserved. "
        "Dashed paths are estimates; partial tracks fade without inventing a finish.",
    }


def enqueue_reconstructions(db, game_id, request):
    report = estimate_report(db, game_id)
    if report["stale"]:
        raise ValueError("Refresh the player review before reconstructing throws")
    shots = {s["candidate_id"]: (p, s) for p in report["players"] for s in p["deliveries"]}
    requested = list(dict.fromkeys(request.candidate_ids or shots))
    if any(cid not in shots for cid in requested):
        raise ValueError("A requested throw no longer belongs to this review")
    plans = []
    for cid in requested:
        player, shot = shots[cid]
        ray = shot.get("delivery_ray")
        if not ray or not ray.get("perspectives"):
            raise ValueError("Construct native/perspective rays for each selected throw first")
        tee_time(ray)
        key = artifact_key(report, shot)
        result = checked_result(db, report, shot)
        if result and request.include_clip and "source-shot.mp4" in result["artifacts"]:
            clip = folder(db, game_id, key) / "source-shot.mp4"
            if export_asset(clip) != result["artifacts"]["source-shot.mp4"]:
                result = None
        if result and (not request.include_clip or "source-shot.mp4" in result["artifacts"]):
            plans.append({"candidate_id": cid, "cached": True, "key": key})
            continue
        plans.append(
            {
                "kind": "throw_reconstruction",
                "stage_version": VERSION,
                "game_id": game_id,
                "candidate_id": cid,
                "person_id": player["person_id"],
                "player": player["name"],
                "review_sha256": report["review_sha256"],
                "ray_sha256": digest(ray),
                "key": key,
                "include_clip": request.include_clip,
                "request": {
                    "source_id": ray["source_id"],
                    "start_seconds": max(0, ray["first_seconds"] - 2),
                    "end_seconds": ray["first_seconds"] + 48,
                    "max_worker_seconds": 240,
                },
            }
        )
        if not result and (folder(db, game_id, key) / "manifest.json").is_file():
            plans[-1]["repair_manifest_sha256"] = hashlib.sha256(
                (folder(db, game_id, key) / "manifest.json").read_bytes()
            ).hexdigest()
        if not result:
            with db.snapshot_session() as session:
                receipts = list(
                    session.scalars(
                        select(Job).where(Job.state == "succeeded", Job.document["key"].as_string() == key)
                    )
                )
                if receipts:
                    latest = max(receipts, key=lambda j: (j.document.get("finished_at", ""), j.id))
                    plans[-1]["repair_after_job_id"] = latest.id
    result = []
    with db.snapshot_session(write=True) as session:
        if latest_revision(session, game_id).sha256 != report["review_sha256"]:
            raise ValueError("Player review changed before queueing reconstructions")
        for plan in plans:
            if plan.get("cached"):
                result.append(plan)
            else:
                row = enqueue_in_session(session, plan)
                if row.state == "failed" and request.retry_failed:
                    if row.attempts >= 3:
                        raise ValueError("Reconstruction retry limit reached; inspect the retained error")
                    row.state = "queued"
                    row.document = {**row.document, "error": None, "progress": 0}
                result.append(
                    {
                        "candidate_id": plan["candidate_id"],
                        "job_id": row.id,
                        "state": row.state,
                        "key": plan["key"],
                    }
                )
    return {"throws": result, "paid_requests": 0}


def export_asset(path):
    with path.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"bytes": path.stat().st_size, "sha256": sha}


def example_seed(db, report, shot, ray):
    root = db.settings.data_dir / "shot-reconstruction" / "example"
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        if (
            manifest["candidate_id"] != shot["candidate_id"]
            or manifest["media_sha256"] != ray["media_sha256"]
            or manifest["review_sha256"] != report["review_sha256"]
            or manifest["ray_id"] != ray["id"]
        ):
            return None
        for name in ("trajectory.json", "observations.json"):
            if export_asset(root / name) != manifest["artifacts"][name]:
                return None
        trajectory = json.loads((root / "trajectory.json").read_text())
        zero = tee_time(ray)
        trajectory["grid"] = [[t - zero, x, s] for t, x, s in trajectory["grid"]]
        trajectory["observation_times"] = [t - zero for t in trajectory["observation_times"]]
        trajectory.update(
            status="observed_rest",
            alignment_source_seconds=zero,
            duration_seconds=trajectory["grid"][-1][0],
            observations=271,
            longest_gap_seconds=manifest["largest_unmeasured_gap_seconds"],
            method="reused_source_pinned_assistant_reviewed_example",
        )
        return trajectory, json.loads((root / "observations.json").read_text())
    except (ValueError, KeyError, OSError):
        return None


def execute_reconstruction(db, job):
    if job["stage_version"] != VERSION:
        raise ValueError("Reconstruction method changed while queued; request a current reconstruction")
    started = time.monotonic()
    token = job["lease_token"]
    report = estimate_report(db, job["game_id"])
    shot = next(
        (s for p in report["players"] for s in p["deliveries"] if s["candidate_id"] == job["candidate_id"]),
        None,
    )
    if (
        report["stale"]
        or report["review_sha256"] != job["review_sha256"]
        or shot is None
        or digest(shot.get("delivery_ray")) != job["ray_sha256"]
    ):
        raise ValueError("Throw evidence changed while queued; generate a new reconstruction")
    ray = shot["delivery_ray"]
    root = folder(db, job["game_id"], job["key"])
    root.mkdir(parents=True, exist_ok=True)

    def tick():
        elapsed = time.monotonic() - started
        if elapsed > 240:
            raise ValueError("Reconstruction exceeded its 240-second worker bound")
        check_execution(db, token, elapsed)

    with db.snapshot_session() as session:
        source = source_or_error(session, ray["source_id"])
    handle = resolve_media(source)
    if handle["sha256"] != ray["media_sha256"]:
        raise ValueError("Original media no longer matches the native ray")
    tick()
    manifest = checked_result(db, report, shot)
    if not manifest:
        cameras = register_common_sheet(ray)
        seed = example_seed(db, report, shot, ray)
        lo = max(0, ray["first_seconds"] - 2)
        hi = min(source.get("duration_seconds") or lo + 50, lo + 50)
        if seed:
            trajectory, observations = seed
        else:
            rows = []
            for image, tm in decode_window(handle["uri"], lo, hi, interval=0.25, max_frames=204, tick=tick):
                if image.size != (1280, 720):
                    raise ValueError("Review camera layout before reconstructing this recording")
                rows.append(
                    {
                        "t": tm["pts"] * tm["time_base_num"] / tm["time_base_den"],
                        "timing": tm,
                        "frame_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
                        "detections": frame_candidates(image, ray, cameras),
                    }
                )
            observations = associate(rows, ray, finish=shot["finish"])
            trajectory = smooth(observations, ray, contact=shot["finish"] == "contact_observed")
        trajectory.update(
            tee_distance_m=ray["nominal_tee_distance_m"],
            normalized_direction=True,
            attribution="assistant_estimate",
        )
        atomic_json(root / "trajectory.json", trajectory)
        atomic_json(root / "observations.json", observations)
        atomic_json(
            root / "geometry.json",
            {"cameras": video_maps(ray, cameras), "tee_distance_m": ray["nominal_tee_distance_m"]},
        )
        end = min(hi, trajectory["alignment_source_seconds"] + trajectory["duration_seconds"] + 3)
        manifest = {
            "version": VERSION,
            "key": job["key"],
            "game_id": job["game_id"],
            "candidate_id": shot["candidate_id"],
            "player": job["player"],
            "review_sha256": report["review_sha256"],
            "ray_sha256": digest(ray),
            "media_sha256": handle["sha256"],
            "source_id": source["id"],
            "source_metadata_sha256": source["metadata_sha256"],
            "created_at": now(),
            "clip_start_seconds": lo,
            "clip_end_seconds": max(lo + 3, end),
            "artifacts": {
                name: export_asset(root / name)
                for name in ("trajectory.json", "observations.json", "geometry.json")
            },
            "notice": "Approximate reconstruction; unknown tails remain unknown. "
            "Nominal 114 ft sheet; camera timing, lens distortion and physical accuracy uncalibrated.",
        }
    if job["include_clip"] and "source-shot.mp4" not in manifest["artifacts"]:
        trajectory = json.loads((root / "trajectory.json").read_text())
        if trajectory["status"] not in ("observed_rest", "apparent_rest"):
            manifest["clip_end_seconds"] = min(
                source.get("duration_seconds") or float("inf"), job["request"]["end_seconds"]
            )
        tick()
        usage = sum(
            p.stat().st_size for p in (db.settings.data_dir / "reconstructions").rglob("*") if p.is_file()
        )
        cap = min(MAX_ARTIFACT_BYTES, max(1, db.settings.working_video_mb) * 1024**2)
        available = min(32 * 1024**2, cap - usage)
        if available < 4 * 1024**2:
            raise ValueError("Reconstruction export storage limit reached; tracks remain available")
        temporary = root / f"source-{os.getpid()}.mp4"
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    str(manifest["clip_start_seconds"]),
                    "-i",
                    handle["uri"],
                    "-t",
                    str(manifest["clip_end_seconds"] - manifest["clip_start_seconds"]),
                    "-map",
                    "0:v:0",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "22",
                    "-pix_fmt",
                    "yuv420p",
                    "-fs",
                    str(available),
                    "-movflags",
                    "+faststart",
                    str(temporary),
                ],
                check=True,
                timeout=90,
            )
            probe = json.loads(
                subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "json",
                        str(temporary),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=15,
                ).stdout
            )
            expected = manifest["clip_end_seconds"] - manifest["clip_start_seconds"]
            if abs(float(probe["format"]["duration"]) - expected) > 0.1:
                raise ValueError("Export storage bound truncated the clip; no incomplete clip published")
            temporary.replace(root / "source-shot.mp4")
            manifest["artifacts"]["source-shot.mp4"] = export_asset(root / "source-shot.mp4")
        finally:
            temporary.unlink(missing_ok=True)
    # Recheck source/review and lease before publishing an immutable input-key result.
    with db.snapshot_session(write=True) as session:
        stopped = check_execution_in_session(session, token, time.monotonic() - started)
        if stopped:
            raise WorkStopped(stopped)
        if (
            latest_revision(session, job["game_id"]).sha256 != job["review_sha256"]
            or source_or_error(session, source["id"])["metadata_sha256"] != source["metadata_sha256"]
        ):
            raise ValueError("Review/source changed during reconstruction")
        from hogline.domain.delivery_rays import current_rays

        current_ray = current_rays(session, db.settings.data_dir, job["game_id"], job["review_sha256"]).get(
            job["candidate_id"]
        )
        if current_ray is None or digest(current_ray) != job["ray_sha256"]:
            raise ValueError("Native ray changed during reconstruction")
        atomic_json(root / "manifest.json", manifest)
        row = session.get(Job, job["id"])
        row.state = "succeeded"
        row.lease_until = None
        row.document = {
            **row.document,
            "progress": 100,
            "finished_at": now(),
            "error": None,
            "manifest_sha256": digest(manifest),
        }
        finish_execution(session, token, "succeeded", time.monotonic() - started)
    return manifest
