"""Release input manifests and reference integrity, independent of score acceptance."""

from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.db import MetadataSnapshot
from hogline.domain.provenance import acquisition_references, observation_manifest, resolve_manifest


def source_lineage(session, data):
    links = acquisition_references(data["evidence"], data["scans"], data["analysis_windows"])
    refs = {
        frame["id"]: {(r["source_id"], r["metadata_sha256"]) for r in links.get(frame["id"], [])}
        for frame in data["evidence"]
    }
    required = {(s["id"], s["metadata_sha256"]) for s in data["sources"]}
    required.update((w["source_id"], w["source_metadata_sha256"]) for w in data["analysis_windows"])
    required.update((s["source_id"], s["source_metadata_sha256"]) for s in data["scans"])
    for versions in refs.values():
        required.update(versions)
    rows = list(
        session.scalars(
            select(MetadataSnapshot).where(MetadataSnapshot.source_id.in_([s["id"] for s in data["sources"]]))
        )
    )
    manifests = []
    present = set()
    for row in rows:
        if digest(row.document) != row.sha256:
            raise ValueError("Source metadata snapshot checksum mismatch; repair it before exporting")
        present.add((row.source_id, row.sha256))
        manifests.append(
            {
                "id": row.id,
                "source_id": row.source_id,
                "sha256": row.sha256,
                "created_at": row.created_at,
                "document": row.document,
            }
        )
    return {
        "source_metadata_snapshots": manifests,
        "evidence_manifest_refs": {
            f["id"]: [
                {"source_id": source_id, "metadata_sha256": sha} for source_id, sha in sorted(refs[f["id"]])
            ]
            for f in data["evidence"]
        },
        "lineage_gaps": {
            "unpinned_evidence_ids": [f["id"] for f in data["evidence"] if not refs[f["id"]]],
            "missing_metadata_snapshots": [
                {"source_id": source_id, "metadata_sha256": sha}
                for source_id, sha in sorted(required - present)
            ],
        },
    }


def validate_references(payload):
    sources = {s["id"] for s in payload["sources"]}
    evidence = {e["id"]: e for e in payload["evidence"]}

    def require(ids, scope, kind):
        if any(i not in scope for i in ids):
            raise ValueError(f"Release {kind} references missing data; publication was rolled back")

    require([e["source_id"] for e in evidence.values()], sources, "evidence")
    for window in payload["analysis_windows"]:
        require([window["source_id"]], sources, "analysis window")
        require(window["anchor_evidence_ids"], evidence, "window anchor")
        require(
            window.get("temporal_anchor_evidence_ids", []),
            set(window["anchor_evidence_ids"]),
            "temporal anchor",
        )
        require(
            [e for c in window["candidates"] for e in c["evidence_ids"]],
            set(window["anchor_evidence_ids"]),
            "window candidate",
        )
        if any(evidence[e]["source_id"] != window["source_id"] for e in window["anchor_evidence_ids"]):
            raise ValueError("Release window anchor/source mismatch; publication was rolled back")
    windows = {w["id"]: w for w in payload["analysis_windows"]}
    for result in payload.get("window_tracks", []):
        require([result["window_id"]], windows, "handle fragments")
        window = windows[result["window_id"]]
        if (
            result["source_id"] != window["source_id"]
            or result["source_metadata_sha256"] != window["source_metadata_sha256"]
            or result["input_parquet"] != window["parquet"]
        ):
            raise ValueError("Release handle fragments disagree with their source window")
        require(result["context_anchor_evidence_ids"], window["anchor_evidence_ids"], "track context anchor")
        for track in result["tracks"]:
            points = track["points"]
            if (
                len(points) < result["minimum_samples"]
                or track["start_seconds"] != points[0]["source_seconds"]
                or track["end_seconds"] != points[-1]["source_seconds"]
                or not window["observed_first_seconds"]
                <= track["start_seconds"]
                < track["end_seconds"]
                <= window["observed_last_seconds"]
                or any(
                    b["source_seconds"] <= a["source_seconds"]
                    for a, b in zip(points, points[1:], strict=False)
                )
            ):
                raise ValueError("Release handle fragment timestamps disagree with their window")
    for result in payload.get("window_actors", []):
        require([result["window_id"]], windows, "actor fragments")
        window = windows[result["window_id"]]
        if result["input"] != window.get("actor_input") or any(
            result[key] != window[key]
            for key in (
                "source_id",
                "source_metadata_sha256",
                "profile_sha256",
            )
        ):
            raise ValueError("Release actor fragments disagree with their source window")
        require(result["context_anchor_evidence_ids"], window["anchor_evidence_ids"], "actor context")
        for track in result["tracks"]:
            points = track["points"]
            if (
                len(points) < 3
                or track["view"] not in ("perspective_left", "perspective_right")
                or track["start_seconds"] != points[0]["source_seconds"]
                or track["end_seconds"] != points[-1]["source_seconds"]
                or not window["observed_first_seconds"]
                <= track["start_seconds"]
                < track["end_seconds"]
                <= window["observed_last_seconds"]
                or any(
                    b["source_seconds"] <= a["source_seconds"]
                    for a, b in zip(points, points[1:], strict=False)
                )
            ):
                raise ValueError("Release actor fragment timestamps disagree with their source window")
    actor_results = {r["id"]: r for r in payload.get("window_actors", [])}
    memberships = set()
    for group in payload.get("players", {}).get("groups", []):
        for member in group["members"]:
            require([member["actor_result_id"]], actor_results, "player fragment")
            parent = actor_results[member["actor_result_id"]]
            track = next((t for t in parent["tracks"] if t["id"] == member["track_id"]), None)
            if (
                member["id"] in memberships
                or member["group_id"] != group["id"]
                or track is None
                or member["actor_result_sha256"] != parent["sha256"]
                or member["source_id"] != parent["source_id"]
                or any(member[k] != track[k] for k in ("view", "start_seconds", "end_seconds"))
            ):
                raise ValueError("Release player grouping disagrees with its original body evidence")
            memberships.add(member["id"])
    for result in payload.get("window_deliveries", []):
        require([result["window_id"]], windows, "release associations")
        require([result["actor_result_id"]], actor_results, "release actor analysis")
        window, actors = windows[result["window_id"]], actor_results[result["actor_result_id"]]
        if (
            result["input_perspective"] != window.get("actor_input")
            or result["actor_result_sha256"] != actors["sha256"]
            or actors["window_id"] != window["id"]
            or any(
                result[key] != window[key]
                for key in ("source_id", "source_metadata_sha256", "profile_sha256")
            )
        ):
            raise ValueError(
                "Release delivery hypotheses disagree with their source window or actor analysis"
            )
        require(result["context_anchor_evidence_ids"], window["anchor_evidence_ids"], "release context")
        handle_tracks = {t["id"]: t for t in result["perspective_handle_tracks"]}
        actor_tracks = {t["id"]: t for t in actors["tracks"]}
        for candidate in result["candidates"]:
            require([candidate["handle_track_id"]], handle_tracks, "release handle track")
            require([candidate["actor_track_id"]], actor_tracks, "release body track")
            handle, actor = (
                handle_tracks[candidate["handle_track_id"]],
                actor_tracks[candidate["actor_track_id"]],
            )
            if (
                candidate["view"] != handle["view"]
                or candidate["view"] != actor["view"]
                or candidate["before"] not in handle["points"]
                or candidate["after"] not in handle["points"]
                or candidate["earliest_release_seconds"] != candidate["before"]["source_seconds"]
                or candidate["latest_release_seconds"] != candidate["after"]["source_seconds"]
                or not window["observed_first_seconds"]
                <= candidate["earliest_release_seconds"]
                < candidate["latest_release_seconds"]
                <= window["observed_last_seconds"]
            ):
                raise ValueError(
                    "Release delivery timing disagrees with supporting person/object observations"
                )
    for result in [*payload.get("window_house_states", []), *payload.get("window_play_evidence", [])]:
        if result.get("version") == "house-focused-play-cues-v1" and (
            digest({k: v for k, v in result.items() if k != "sha256"}) != result["sha256"]
        ):
            raise ValueError("Release house-focused play checksum mismatch")
        require([result["window_id"]], windows, "house states")
        window = windows[result["window_id"]]
        if any(
            result[key] != window[other]
            for key, other in (
                ("source_id", "source_id"),
                ("source_metadata_sha256", "source_metadata_sha256"),
                ("input_parquet", "parquet"),
                ("profile_sha256", "profile_sha256"),
                ("detector_version", "model_version"),
                ("visibility_version", "visibility_version"),
            )
        ):
            raise ValueError("Release house states disagree with their source window")
        require(result["context_anchor_evidence_ids"], window["anchor_evidence_ids"], "house-state context")
        states = {s["id"]: s for s in result["states"]}
        for state in states.values():
            if (
                len(state["sample_indices"]) < result["minimum_samples"]
                or state["first"]["sample_index"] != state["sample_indices"][0]
                or state["last"]["sample_index"] != state["sample_indices"][-1]
                or state["first"]["source_seconds"] != state["start_seconds"]
                or state["last"]["source_seconds"] != state["end_seconds"]
                or not window["observed_first_seconds"]
                <= state["start_seconds"]
                < state["end_seconds"]
                <= window["observed_last_seconds"]
            ):
                raise ValueError("Release house-state timing disagrees with its source window")
        for change in result["transitions"]:
            require([change["before_state_id"], change["after_state_id"]], states, "house-state change")
            before, after = states[change["before_state_id"]], states[change["after_state_id"]]
            if (
                change["before"] != before["last"]
                or change["after"] != after["first"]
                or change["view"] != before["view"]
                or change["view"] != after["view"]
                or change["earliest_seconds"] != before["end_seconds"]
                or change["latest_seconds"] != after["start_seconds"]
            ):
                raise ValueError("Release house-state change disagrees with its supporting states")
    for scan in payload["processing_scans"]:
        require([scan["source_id"]], sources, "processing scan")
        require([s["evidence_id"] for s in scan.get("samples", [])], evidence, "processing sample")
        if any(evidence[s["evidence_id"]]["source_id"] != scan["source_id"] for s in scan.get("samples", [])):
            raise ValueError("Release processing sample/source mismatch; publication was rolled back")
    lineage_games = [
        *payload["games"],
        *payload.get("retired_games", []),
        *[p["game"] for r in payload.get("reconciliations", []) for p in r["parents"]],
        *[g for r in payload.get("reconciliations", []) for g in r["successors"]],
    ]
    known_games = {g["id"] for g in lineage_games}
    for game in lineage_games:
        require(game.get("retired_into", []), known_games, "game retirement")
        require([s["source_id"] for s in game["spans"]], sources, "game span")
        require(
            [i for e in game["ends"] for i in e["evidence_ids"]] + game["result"]["evidence_ids"],
            evidence,
            "score",
        )
    for observation in [
        *payload["observations"],
        *payload["broadcast"]["observations"],
        *payload.get("board_locations", []),
    ]:
        require([observation["evidence_id"]], evidence, "observation")
        reference = observation.get("header_reference") or {}
        if reference.get("evidence_id"):
            require([reference["evidence_id"]], evidence, "board header reference")
            if reference.get("image_sha256") != evidence[reference["evidence_id"]]["image"]["sha256"]:
                raise ValueError("Board header reference image checksum disagrees with its evidence")
        if (
            observation.get("source_id")
            and observation["source_id"] != evidence[observation["evidence_id"]]["source_id"]
        ):
            raise ValueError("Release observation/source mismatch; publication was rolled back")
    acquisition_refs = acquisition_references(
        evidence.values(), payload["processing_scans"], payload["analysis_windows"]
    )
    manifests = {
        (m["source_id"], m["sha256"]): digest(m["document"]) == m["sha256"]
        for m in payload.get("source_metadata_snapshots", [])
    }
    for observation in payload["broadcast"]["observations"]:
        if "source_manifest_resolution" in observation:
            eid = observation["evidence_id"]
            expected = resolve_manifest(
                observation, evidence.get(eid), acquisition_refs.get(eid, []), manifests
            )
            if observation["source_manifest_resolution"] != expected:
                raise ValueError("Release observation manifest resolution disagrees with acquisition records")
    timeline = payload["broadcast"].get("board_timeline", {})
    epochs = {e["id"]: e for e in timeline.get("epochs", [])}
    broadcast_observations = {o["id"]: o for o in payload["broadcast"]["observations"]}
    for epoch in epochs.values():
        require([epoch["source_id"]], sources, "board epoch")
        require(epoch["observation_ids"], broadcast_observations, "board epoch observation")
        for oid in epoch["observation_ids"]:
            observation = broadcast_observations[oid]
            if (
                observation["source_id"] != epoch["source_id"]
                or observation_manifest(observation) != epoch["source_metadata_sha256"]
                or observation["model_version"] != epoch["model_version"]
                or observation["evidence_id"] not in epoch["evidence_ids"]
            ):
                raise ValueError("Release board epoch disagrees with observation provenance")
        for key in ("first", "last"):
            anchor = epoch[key]
            require([anchor["observation_id"]], epoch["observation_ids"], "board epoch anchor")
            observation = broadcast_observations[anchor["observation_id"]]
            if (
                anchor["evidence_id"] != observation["evidence_id"]
                or anchor["source_seconds"] != observation["source_seconds"]
            ):
                raise ValueError("Release board anchor disagrees with its observation")
        require(epoch["evidence_ids"], evidence, "board epoch evidence")
        if any(evidence[i]["source_id"] != epoch["source_id"] for i in epoch["evidence_ids"]):
            raise ValueError("Release board epoch/source mismatch")
    for change in timeline.get("changes", []):
        require([change["before_epoch_id"], change["after_epoch_id"]], epochs, "board change")
        require(change["evidence_ids"], evidence, "board change evidence")
        if (
            change["before"] != epochs[change["before_epoch_id"]]["last"]
            or change["after"] != epochs[change["after_epoch_id"]]["first"]
        ):
            raise ValueError("Release board change anchors disagree with their epochs")
        if any(
            epochs[i]["source_id"] != change["source_id"]
            for i in (change["before_epoch_id"], change["after_epoch_id"])
        ):
            raise ValueError("Release board change/source mismatch")
    if "score_proposals" in payload["broadcast"]:
        from hogline.perception.scoreboard import persistent_board_claims

        expected_scores = persistent_board_claims(payload["broadcast"]["observations"])
        if payload["broadcast"]["score_proposals"] != expected_scores:
            raise ValueError("Release score proposals disagree with pinned repeated board readings")
    if "score_disagreements" in payload:
        from hogline.reconstruction.score_conflicts import validate_review_export

        validate_review_export(payload["score_disagreements"], payload, payload["broadcast"])
    for proposal in payload["source_boundary_links"]:
        require([proposal["left_source_id"], proposal["right_source_id"]], sources, "recording link")
        require(
            proposal["left"]["evidence_ids"] + proposal["right"]["evidence_ids"], evidence, "recording link"
        )

    for score in payload.get("house_score_observations", []):
        require([score["evidence_id"]], evidence, "house-score observation")
        frame = evidence[score["evidence_id"]]
        detector = next(
            (o for o in payload["observations"] if o["id"] == score["detector_observation_id"]), None
        )
        if (
            digest({k: v for k, v in score.items() if k != "sha256"}) != score["sha256"]
            or frame["source_id"] != score["source_id"]
            or frame.get("profile_sha256") != score["profile_sha256"]
            or score["source_metadata_sha256"]
            not in {r["metadata_sha256"] for r in acquisition_refs.get(frame["id"], [])}
            or any(score[k] != frame[k] for k in ("source_seconds", "pts", "time_base_num", "time_base_den"))
            or score["input_crops"] != {v: frame["crops"][v] for v in score["views"]}
            or detector is None
            or digest(detector) != score["detector_observation_sha256"]
        ):
            raise ValueError("Release house-score provenance disagrees with source evidence")
    if "end_timelines" in payload:
        from hogline.reconstruction.ends import reconstruct_ends

        expected = [
            reconstruct_ends(
                source["id"],
                payload["broadcast"]["observations"],
                payload.get("window_house_states", []) + payload.get("window_play_evidence", []),
                payload.get("house_score_observations", []),
            )
            for source in payload["sources"]
        ]
        if payload["end_timelines"] != expected:
            raise ValueError("Release end timelines disagree with their pinned observations")
    if "scorecard_timelines" in payload:
        from hogline.analytics.scorecards import check_observation, select_inputs
        from hogline.reconstruction.scorecards import reconstruct_scorecards

        inspected = payload.get("scorecard_observations", [])
        parents = {o["id"]: o for o in payload.get("scorecard_parent_observations", [])}
        for observation in inspected:
            check_observation(observation, evidence.get(observation["evidence_id"]), parents)
            for key in ("reference", "fixed_header_reference"):
                reference = observation.get(key)
                if (
                    reference
                    and evidence.get(reference["evidence_id"], {}).get("image") != reference["image"]
                ):
                    raise ValueError("Scorecard header reference differs from original evidence")
        expected = [
            reconstruct_scorecards(
                source["id"], select_inputs(source["id"], payload["broadcast"]["observations"], inspected)
            )
            for source in payload["sources"]
        ]
        if payload["scorecard_timelines"] != expected:
            raise ValueError("Release scorecard timelines disagree with their pinned observations")
    if "reconciled_score_histories" in payload:
        from hogline.analytics.ice_reviews import check_review
        from hogline.reconstruction.scoring import reconcile_scores

        reviews = payload.get("ice_end_reviews", [])
        for review in reviews:
            check_review(review, evidence)
        expected = [reconcile_scores(report, reviews) for report in payload["scorecard_timelines"]]
        if payload["reconciled_score_histories"] != expected:
            raise ValueError("Release combined scores disagree with their independent ice reviews")
