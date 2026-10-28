import csv
import io
import json

from hogline.adapters.sources import digest
from hogline.analytics.lineage import source_lineage, validate_references
from hogline.analytics.tracks import tracks_in_session
from hogline.db import Release, now
from hogline.domain.catalog import catalog_in_session
from hogline.perception.board_locator import board_locations_in_session
from hogline.perception.broadcast import broadcast_catalog_in_session
from hogline.perception.pipeline import observations_in_session
from hogline.reconstruction.boundaries import boundary_links
from hogline.review.score_conflicts import conflicts_in_session


def snapshot(db, mode="confirmed"):
    if mode not in ("confirmed", "exploratory"):
        raise ValueError("Export mode must be confirmed or exploratory")
    with db.snapshot_session(write=True) as session:
        return snapshot_in_session(session, mode, db.settings.data_dir)


def snapshot_in_session(session, mode, root):
    data = catalog_in_session(session)
    broadcast = broadcast_catalog_in_session(session, root)
    games = data["games"]
    if mode == "confirmed":
        games = [g for g in games if g["acceptance"] == "human_confirmed"]
        for game in games:
            game["ends"] = [e for e in game["ends"] if e["acceptance"] == "human_confirmed"]
            if game["result"]["acceptance"] != "human_confirmed":
                game["result"] = {
                    "outcome": "unknown",
                    "red_points": None,
                    "yellow_points": None,
                    "acceptance": "unknown",
                    "evidence_ids": [],
                }
    payload = {
        "schema_version": "hogline-archive-v2",
        "mode": mode,
        "created_at": now(),
        "games": games,
        "retired_games": data["retired_games"],
        "reconciliations": data["reconciliations"],
        "sources": data["sources"],
        **source_lineage(session, data),
        "processing_scans": data["scans"],
        "analysis_windows": data["analysis_windows"],
        "window_tracks": tracks_in_session(session, full=True),
        "evidence": data["evidence"],
        "observations": observations_in_session(session),
        "broadcast": broadcast,
        "score_disagreements": conflicts_in_session(session, {**data, "games": games}, broadcast),
        "board_locations": board_locations_in_session(session),
        "source_boundary_links": boundary_links(
            data["sources"],
            broadcast["observations"],
            broadcast["version"],
            {e["id"] for e in data["evidence"]},
        ),
        "activity_timelines": data["timelines"],
        "coverage": {
            "inventoried_recordings": len(data["sources"]),
            "canonical_games": len(data["games"]),
            "exported_games": len(games),
            "confirmed_results": sum(g["result"]["acceptance"] == "human_confirmed" for g in games),
            "scan_intervals": [
                {"source_id": s["source_id"], **s["request"], "kind": "sparse_sampled"} for s in data["scans"]
            ],
        },
        "metric_definition": {
            "id": "confirmed-results-v1",
            "grain": "canonical_game",
            "eligibility": "human_confirmed final result; never inferred from partial end totals",
            "unknown": "excluded from decided-game counts; null is not zero",
        },
    }
    validate_references(payload)
    # Use the same rows that were serialized, even if a review is saved during export.
    payload["interpretation_revisions"] = {
        g["id"]: g["revision"] for g in [*data["games"], *data["retired_games"]]
    }
    release = Release(sha256=digest(payload), document=payload)
    session.add(release)
    session.flush()
    return {"id": release.id, "sha256": release.sha256, **payload}


def csv_export(release):
    out = io.StringIO()
    fields = [
        "row_type",
        "game_id",
        "game_revision",
        "sheet",
        "label",
        "red_team",
        "yellow_team",
        "league",
        "game_acceptance",
        "source_spans_json",
        "coverage",
        "completion",
        "acceptance",
        "observed_index",
        "official_number",
        "end_state",
        "red_points",
        "yellow_points",
        "outcome",
        "evidence_ids",
        "youtube_links",
    ]
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    evidence = {e["id"]: e for e in release["evidence"]}
    for game in release["games"]:
        base = {
            "game_id": game["id"],
            "game_revision": game["revision"],
            "sheet": game["sheet"],
            "label": game["label"],
            "red_team": game["red_team"],
            "yellow_team": game["yellow_team"],
            "league": game["league"],
            "game_acceptance": game["acceptance"],
            "source_spans_json": json.dumps(game["spans"], separators=(",", ":")),
            "coverage": game["coverage"],
            "completion": game["completion"],
        }
        result = game["result"]
        ids = result["evidence_ids"]
        rows = [
            {
                **base,
                "row_type": "game",
                "acceptance": result["acceptance"],
                "red_points": result["red_points"],
                "yellow_points": result["yellow_points"],
                "outcome": result["outcome"],
                "evidence_ids": "|".join(ids),
                "youtube_links": "|".join(evidence[i].get("youtube_url") or "" for i in ids)
                or "|".join(s.get("youtube_url") or "" for s in game["spans"]),
            }
        ]
        for end in game["ends"]:
            ids = end["evidence_ids"]
            rows.append(
                {
                    **base,
                    "row_type": "end",
                    "acceptance": end["acceptance"],
                    "observed_index": end["observed_index"],
                    "official_number": end["official_number"],
                    "end_state": end["state"],
                    "red_points": end["red_points"],
                    "yellow_points": end["yellow_points"],
                    "evidence_ids": "|".join(ids),
                    "youtube_links": "|".join(evidence[i].get("youtube_url") or "" for i in ids),
                }
            )
        for row in rows:
            # Text from source metadata/reviewers must not become spreadsheet formulas.
            writer.writerow(
                {
                    k: "'" + str(v) if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v
                    for k, v in row.items()
                }
            )
    return out.getvalue()
