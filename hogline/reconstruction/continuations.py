"""Proposed score-history grouping across checked nearby recording gaps."""

from collections import Counter, defaultdict

from hogline.adapters.sources import digest

VERSION = "clock-and-score-prefix-continuations-v2"
RECONCILED_VERSION = "grouped-board-and-reviewed-ice-scores-v2"
LEGACY_RECONCILED_VERSION = "grouped-board-and-reviewed-ice-scores-v1"
COLORS = ("red", "yellow")


def known_points(end):
    points = end["points"]
    return (
        tuple(points[c] for c in COLORS)
        if end.get("status", "proposed") == "proposed" and all(points[c] is not None for c in COLORS)
        else None
    )


def continuation_score_groups(sources, reports, links):
    source_map = {s["id"]: s for s in sources}
    by_source = {r["source_id"]: sorted(r["scorecards"], key=lambda h: h["first_seconds"]) for r in reports}
    books = {h["id"]: (sid, h) for sid, group in by_source.items() for h in group}
    candidates = []
    for link in links:
        a, b = link["left_source_id"], link["right_source_id"]
        lefts, rights = by_source.get(a, []), by_source.get(b, [])
        if not lefts or not rights:
            continue
        left, right = lefts[-1], rights[0]
        if (
            a == b
            or source_map[a].get("sheet") != source_map[b].get("sheet")
            or not source_map[a].get("sheet")
            or not 0 <= link["estimated_recording_gap_seconds"] <= 600
            or any(
                source_map[sid].get("metadata_sha256") != value
                for sid, value in link["source_manifest_hashes"].items()
            )
            or set(link["source_manifest_hashes"]) != {a, b}
            or any(
                h["source_metadata_sha256"] != source_map[sid]["metadata_sha256"]
                for sid, h in ((a, left), (b, right))
            )
            or left.get("card_conflicts")
            or right.get("card_conflicts")
            or not left["right_censored"]
            or not right["left_censored"]
            or source_map[a]["duration_seconds"] - left["last_seconds"] > 180
            or right["first_seconds"] > 120
        ):
            continue
        le = {e["official_number"]: known_points(e) for e in left["ends"]}
        re = {e["official_number"]: known_points(e) for e in right["ends"]}
        shared = {n for n in le.keys() & re.keys() if le[n] is not None and re[n] is not None}
        if any(le[n] != re[n] for n in shared):
            continue
        prefix = []
        for number in range(1, min(max(le, default=0), max(re, default=0)) + 1):
            if number not in shared:
                break
            prefix.append(number)
        if len(prefix) < 3 or not all(any(le[n][c] > 0 for n in prefix) for c in (0, 1)):
            continue
        candidates.append(
            {
                "left_history_id": left["id"],
                "right_history_id": right["id"],
                "boundary_link_id": link["id"],
                "matching_prefix_ends": prefix,
                "estimated_recording_gap_seconds": link["estimated_recording_gap_seconds"],
                "gap_uncertainty_seconds": link["gap_uncertainty_seconds"],
            }
        )
    # Multiple clock hypotheses for the same pair do not strengthen a match.
    pairs = defaultdict(list)
    for candidate in candidates:
        pairs[candidate["left_history_id"], candidate["right_history_id"]].append(candidate)
    unique = [values[0] for values in pairs.values() if len(values) == 1]
    outgoing = Counter(a for a, _ in pairs)
    incoming = Counter(b for _, b in pairs)
    edges = [e for e in unique if outgoing[e["left_history_id"]] == incoming[e["right_history_id"]] == 1]
    next_edge = {e["left_history_id"]: e for e in edges}
    parent = {e["right_history_id"] for e in edges}
    groups, visited = [], set()
    for start in sorted(books):
        if start in parent or start in visited:
            continue
        chain, chain_edges, current = [], [], start
        while current not in chain:
            chain.append(current)
            edge = next_edge.get(current)
            if edge is None:
                break
            chain_edges.append(edge)
            current = edge["right_history_id"]
        if current in chain[:-1]:
            continue
        visited.update(chain)
        groups.append(_group(chain, chain_edges, books))
    # A cycle cannot be resolved by an arbitrary ordering.
    groups.extend(_group([key], [], books) for key in sorted(set(books) - visited))
    result = {
        "version": VERSION,
        "acceptance": "proposed",
        "groups": groups,
        "summary": {
            "source_local_histories": len(books),
            "proposed_groups": len(groups),
            "source_local_known_end_scores": sum(
                known_points(e) is not None for _, h in books.values() for e in h["ends"]
            ),
            "grouped_known_end_scores": sum(g["known_end_scores"] for g in groups),
            "physical_game_count": None,
        },
        "notice": (
            "Clock continuity and matching posted prefixes propose common history. Missing footage, "
            "physical game completion and final results remain unresolved; canonical games are unchanged."
        ),
    }
    result["sha256"] = digest(result)
    return result


def _group(chain, edges, books):
    by_end = defaultdict(list)
    for identifier in chain:
        source_id, history = books[identifier]
        for end in history["ends"]:
            by_end[end["official_number"]].append(
                {"source_id": source_id, "history_id": identifier, "end": end}
            )
    ends, totals = [], dict.fromkeys(COLORS, 0)
    for number in range(1, max(by_end, default=0) + 1):
        variants = by_end[number]
        claims = {known_points(v["end"]) for v in variants} - {None}
        points = (
            dict(zip(COLORS, next(iter(claims)), strict=True))
            if len(claims) == 1
            else dict.fromkeys(COLORS, None)
        )
        totals = {
            c: totals[c] + points[c] if totals[c] is not None and points[c] is not None else None
            for c in COLORS
        }
        ends.append(
            {
                "official_number": number,
                "points": points,
                "score_after_end": dict(totals),
                "status": "proposed" if len(claims) == 1 else "disputed" if claims else "unresolved",
                "source_claims": variants,
            }
        )
    body = {
        "history_ids": chain,
        "source_ids": [books[i][0] for i in chain],
        "continuation_links": edges,
        "ends": ends,
        "known_end_scores": sum(e["status"] == "proposed" for e in ends),
        "acceptance": "proposed",
        "final_result": None,
        "ends_played": None,
    }
    return {"id": digest(body), **body}


def reconcile_continuation_groups(grouping, reports, *, version=RECONCILED_VERSION):
    """Combine checked source projections while retaining every source's claim."""
    if version not in (RECONCILED_VERSION, LEGACY_RECONCILED_VERSION):
        raise ValueError("Unsupported grouped score history version")
    by_board = {
        h["board_history_id"]: h for report in reports for h in report["histories"] if h["board_history_id"]
    }
    inputs = [(g, [by_board[key] for key in g["history_ids"]]) for g in grouping["groups"]]
    # Independent ice-only histories must not disappear just because no board was readable.
    inputs.extend((None, [h]) for report in reports for h in report["histories"] if not h["board_history_id"])
    histories = []
    for group, members in inputs:
        by_end = defaultdict(list)
        for member in members:
            for end in member["ends"]:
                by_end[end["official_number"] or end["observed_index"]].append(
                    {"source_id": member["source_id"], "history_id": member["id"], "end": end}
                )
        ends, totals, prefix = [], dict.fromkeys(COLORS, 0), 0
        for number in range(1, max(by_end, default=0) + 1):
            originals = by_end[number]

            def claims(field, originals=originals):
                return {
                    tuple(v["end"][field][c] for c in COLORS)
                    for v in originals
                    if v["end"].get(field) and all(v["end"][field][c] is not None for c in COLORS)
                }

            scores = claims("points")
            board, ice, lying = (
                claims(field) for field in ("board_points", "ice_points", "lying_ice_points")
            )
            disputed = (
                any(v["end"]["status"] == "disputed" for v in originals)
                or len(scores) > 1
                or len(board | ice) > 1
            )
            unscored = version == RECONCILED_VERSION and any(
                v["end"]["status"] == "not_scored" for v in originals
            )
            disputed = disputed or (unscored and bool(scores | board | ice))

            def single(values):
                return dict(zip(COLORS, next(iter(values)), strict=True)) if len(values) == 1 else None

            points = None if disputed else single(scores)
            totals = {
                c: totals[c]
                if unscored and not disputed
                else totals[c] + points[c]
                if totals[c] is not None and points is not None
                else None
                for c in COLORS
            }
            status = (
                "disputed"
                if disputed
                else "not_scored"
                if unscored
                else "proposed"
                if points is not None
                else "unresolved"
            )
            if prefix == number - 1 and status == "proposed":
                prefix = number
            board_ids = sorted(
                {i for v in originals for i in (v["end"]["board_evidence"] or {}).get("evidence_ids", [])}
            )
            end = {
                "official_number": number
                if group or any(v["end"]["official_number"] for v in originals)
                else None,
                "observed_index": number,
                "points": points or dict.fromkeys(COLORS, None),
                "score_after_end": dict(totals),
                "board_points": single(board),
                "ice_points": single(ice),
                "lying_ice_points": single(lying),
                "end_completion_verified": any(v["end"]["end_completion_verified"] for v in originals),
                "comparison": "disagreement"
                if disputed
                else "concession_not_scored"
                if unscored
                else "agreement"
                if board and ice
                else "scoreboard_only"
                if board
                else "ice_only"
                if ice
                else "unresolved",
                "status": status,
                "state": "not_scored_concession"
                if unscored and not disputed
                else "unknown"
                if points is None
                else "played"
                if any(points.values())
                else "blank",
                "board_evidence": {"evidence_ids": board_ids} if board_ids else None,
                "source_claims": originals,
                **{
                    field: sorted({i for v in originals for i in v["end"][field]})
                    for field in ("ice_review_ids", "ice_evidence_ids", "phase_evidence_ids")
                },
            }
            ends.append(end)
        complete = bool(
            ends
            and all(
                e["status"] == "proposed" or (i == len(ends) - 1 and e["status"] == "not_scored")
                for i, e in enumerate(ends)
            )
            and any(h["game_completion"] == "complete" and len(h["ends"]) == len(ends) for h in members)
        )
        body = {
            "continuation_group_id": group["id"] if group else None,
            "source_ids": [h["source_id"] for h in members],
            "source_history_ids": [h["id"] for h in members],
            "continuation_links": group["continuation_links"] if group else [],
            "ends": ends,
            "score_history_complete_through": prefix or None,
            "known_end_scores": sum(e["status"] == "proposed" for e in ends),
            "game_completion": "complete" if complete else "unknown",
            "final_result": dict(totals) if complete else None,
            "ends_played": len(ends)
            if complete
            and (
                version == LEGACY_RECONCILED_VERSION
                or any(h["ends_played"] == len(ends) for h in members if h["game_completion"] == "complete")
            )
            else None,
            "acceptance": "proposed",
        }
        histories.append({"id": digest(body), **body})
    result = {
        "version": version,
        "grouping_sha256": grouping["sha256"],
        "source_report_hashes": {r["source_id"]: r["sha256"] for r in reports},
        "histories": histories,
        "summary": {
            "known_end_scores": sum(h["known_end_scores"] for h in histories),
            "score_disagreements": sum(e["status"] == "disputed" for h in histories for e in h["ends"]),
            "physical_game_count": None,
        },
        "notice": "Proposed continuation groups combine source-local board and independent ice evidence. "
        "Every original claim and missing recording gap is retained; "
        "lying stones alone do not prove completion.",
    }
    return {**result, "sha256": digest(result)}
