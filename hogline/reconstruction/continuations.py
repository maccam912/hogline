"""Proposed score-history grouping across checked nearby recording gaps."""

from collections import Counter, defaultdict

from hogline.adapters.sources import digest

VERSION = "clock-and-score-prefix-continuations-v1"
COLORS = ("red", "yellow")


def known_points(end):
    points = end["points"]
    return tuple(points[c] for c in COLORS) if all(points[c] is not None for c in COLORS) else None


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
