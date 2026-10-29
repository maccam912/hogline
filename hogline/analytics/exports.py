import csv
import io
import json

from hogline.adapters.sources import digest
from hogline.analytics.actors import actors_in_session
from hogline.analytics.deliveries import deliveries_in_session
from hogline.analytics.end_timeline import house_scores_in_session
from hogline.analytics.house_states import house_states_in_session
from hogline.analytics.ice_reviews import ice_reviews_in_session
from hogline.analytics.lineage import source_lineage, validate_references
from hogline.analytics.play import play_in_windows
from hogline.analytics.scorecards import (
    parent_observations_in_session,
    scorecard_observations_in_session,
    select_inputs,
)
from hogline.analytics.tracks import tracks_in_session
from hogline.db import Release, now
from hogline.domain.catalog import catalog_in_session
from hogline.domain.players import players_in_session
from hogline.perception.board_locator import board_locations_in_session
from hogline.perception.broadcast import broadcast_catalog_in_session
from hogline.perception.pipeline import observations_in_session
from hogline.reconstruction.boundaries import boundary_links
from hogline.reconstruction.continuations import continuation_score_groups, reconcile_continuation_groups
from hogline.reconstruction.ends import reconstruct_ends
from hogline.reconstruction.scorecards import reconstruct_scorecards
from hogline.reconstruction.scoring import reconcile_scores
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
        "window_house_states": house_states_in_session(session, full=True),
        "window_actors": actors_in_session(session, full=True),
        "window_deliveries": deliveries_in_session(session, full=True),
        "players": players_in_session(session, history=True),
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
    # Validate existing interpretation/observation lineage before opening optional dense inputs.
    validate_references(payload)
    payload["house_score_observations"] = house_scores_in_session(session)
    play = {"results": [], "exclusions": []}
    for source in data["sources"]:
        scoped = play_in_windows(
            root, [w for w in data["analysis_windows"] if w["source_id"] == source["id"]]
        )
        play["results"].extend(scoped["results"])
        play["exclusions"].extend(scoped["exclusions"])
    payload["window_play_evidence"] = play["results"]
    payload["play_input_exclusions"] = play["exclusions"]
    payload["end_timelines"] = [
        reconstruct_ends(
            source["id"],
            broadcast["observations"],
            payload["window_house_states"] + payload["window_play_evidence"],
            payload["house_score_observations"],
        )
        for source in data["sources"]
    ]
    payload["scorecard_observations"] = scorecard_observations_in_session(session)
    payload["scorecard_parent_observations"] = parent_observations_in_session(
        session, payload["scorecard_observations"]
    )
    payload["scorecard_timelines"] = [
        reconstruct_scorecards(
            source["id"],
            select_inputs(source["id"], broadcast["observations"], payload["scorecard_observations"]),
        )
        for source in data["sources"]
    ]
    payload["ice_end_reviews"] = ice_reviews_in_session(session)
    payload["reconciled_score_histories"] = [
        reconcile_scores(report, payload["ice_end_reviews"]) for report in payload["scorecard_timelines"]
    ]
    payload["continuation_score_groups"] = continuation_score_groups(
        payload["sources"], payload["scorecard_timelines"], payload["source_boundary_links"]
    )
    payload["grouped_score_histories"] = reconcile_continuation_groups(
        payload["continuation_score_groups"], payload["reconciled_score_histories"]
    )
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
        "score_history_id",
        "source_id",
        "cumulative_red_points",
        "cumulative_yellow_points",
        "board_red_points",
        "board_yellow_points",
        "ice_red_points",
        "ice_yellow_points",
        "score_comparison",
        "ice_review_ids",
        "lying_red_points",
        "lying_yellow_points",
        "end_completion_verified",
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
    if release["mode"] == "exploratory":
        sources = {s["id"]: s for s in release["sources"]}
        for report in release.get("reconciled_score_histories", []):
            source = sources[report["source_id"]]
            for history in report["histories"]:
                base = {
                    "score_history_id": history["id"],
                    "source_id": source["id"],
                    "sheet": source.get("sheet"),
                    "label": source["title"],
                    "acceptance": "proposed",
                    "game_acceptance": "proposed",
                    "coverage": history["coverage"],
                    "completion": history["game_completion"],
                }
                final = history["final_result"] or {}
                rows = [
                    {
                        **base,
                        "row_type": "score_history",
                        "red_points": final.get("red"),
                        "yellow_points": final.get("yellow"),
                    }
                ]
                for end in history["ends"]:
                    ids = sorted(
                        set(
                            end["ice_evidence_ids"]
                            + end["phase_evidence_ids"]
                            + (end["board_evidence"] or {}).get("evidence_ids", [])
                        )
                    )
                    rows.append(
                        {
                            **base,
                            "row_type": "score_history_end",
                            "acceptance": end["status"],
                            "observed_index": end["observed_index"],
                            "official_number": end["official_number"],
                            "end_state": end["state"],
                            "red_points": end["points"]["red"],
                            "yellow_points": end["points"]["yellow"],
                            "cumulative_red_points": end["score_after_end"]["red"],
                            "cumulative_yellow_points": end["score_after_end"]["yellow"],
                            **{
                                f"{basis}_{c}_points": (end[f"{basis}_points"] or {}).get(c)
                                for basis in ("board", "ice")
                                for c in ("red", "yellow")
                            },
                            "score_comparison": end["comparison"],
                            "ice_review_ids": "|".join(end["ice_review_ids"]),
                            "lying_red_points": (end["lying_ice_points"] or {}).get("red"),
                            "lying_yellow_points": (end["lying_ice_points"] or {}).get("yellow"),
                            "end_completion_verified": end["end_completion_verified"],
                            "evidence_ids": "|".join(ids),
                            "youtube_links": "|".join(evidence[i].get("youtube_url") or "" for i in ids),
                        }
                    )
                for row in rows:
                    writer.writerow(
                        {
                            k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v
                            for k, v in row.items()
                        }
                    )
    return out.getvalue()


def grouped_scores_csv(release):
    """One row per proposed grouped end; source-local rows remain in the ordinary CSV."""
    if release["mode"] != "exploratory":
        raise ValueError("Grouped proposed scores require exploratory export mode")
    fields = [
        "score_history_id",
        "continuation_group_id",
        "source_ids",
        "source_history_ids",
        "sheet",
        "official_number",
        "acceptance",
        "comparison",
        "red_points",
        "yellow_points",
        "cumulative_red_points",
        "cumulative_yellow_points",
        "board_red_points",
        "board_yellow_points",
        "ice_red_points",
        "ice_yellow_points",
        "lying_red_points",
        "lying_yellow_points",
        "end_completion_verified",
        "game_completion",
        "ice_review_ids",
        "evidence_ids",
        "youtube_links",
    ]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    sources = {s["id"]: s for s in release["sources"]}
    evidence = {e["id"]: e for e in release["evidence"]}
    for history in release["grouped_score_histories"]["histories"]:
        sheets = {sources[sid].get("sheet") for sid in history["source_ids"]}
        for end in history["ends"]:
            ids = sorted(
                set(
                    end["ice_evidence_ids"]
                    + end["phase_evidence_ids"]
                    + (end["board_evidence"] or {}).get("evidence_ids", [])
                )
            )
            row = {
                "score_history_id": history["id"],
                "continuation_group_id": history["continuation_group_id"],
                "source_ids": "|".join(history["source_ids"]),
                "source_history_ids": "|".join(history["source_history_ids"]),
                "sheet": next(iter(sheets)) if len(sheets) == 1 else None,
                "official_number": end["official_number"],
                "acceptance": end["status"],
                "comparison": end["comparison"],
                "red_points": end["points"]["red"],
                "yellow_points": end["points"]["yellow"],
                "cumulative_red_points": end["score_after_end"]["red"],
                "cumulative_yellow_points": end["score_after_end"]["yellow"],
                **{
                    f"{basis}_{c}_points": (end[field] or {}).get(c)
                    for basis, field in (
                        ("board", "board_points"),
                        ("ice", "ice_points"),
                        ("lying", "lying_ice_points"),
                    )
                    for c in ("red", "yellow")
                },
                "end_completion_verified": end["end_completion_verified"],
                "game_completion": history["game_completion"],
                "ice_review_ids": "|".join(end["ice_review_ids"]),
                "evidence_ids": "|".join(ids),
                "youtube_links": "|".join(evidence[i].get("youtube_url") or "" for i in ids),
            }
            writer.writerow(
                {
                    k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v
                    for k, v in row.items()
                }
            )
    return out.getvalue()
