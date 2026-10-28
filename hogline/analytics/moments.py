"""Search existing evidence-backed observations without acquiring more media."""

from collections import Counter, defaultdict
from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from hogline.adapters.sources import digest, video_link
from hogline.analytics.tracks import tracks_in_session
from hogline.config import profile
from hogline.domain.catalog import Conflict, catalog_in_session
from hogline.domain.models import Acceptance, Model
from hogline.media.windows import VERSION as DECODER_VERSION
from hogline.perception.broadcast import broadcast_catalog_in_session
from hogline.perception.stones import VERSION as DETECTOR_VERSION
from hogline.perception.temporal import VERSION as WINDOW_VERSION
from hogline.perception.tracking import VERSION as TRACKING_VERSION
from hogline.perception.visibility import VERSION as VISIBILITY_VERSION

VERSION = "evidence-moment-search-v1"
Kind = Literal["board_change", "score_proposal", "handle_motion", "house_configuration", "reviewed_end"]


class MomentQuery(Model):
    text: str = Field(default="", max_length=200)
    sheet: int | None = Field(default=None, ge=1, le=8)
    source_id: str | None = None
    title_date_from: date | None = None
    title_date_to: date | None = None
    kinds: list[Kind] = Field(default_factory=list, max_length=5)
    acceptance: Acceptance | None = None
    include_earlier_inputs: bool = False
    offset: int = Field(default=0, ge=0, le=1000000)
    limit: int = Field(default=25, ge=1, le=100)
    expected_index_sha256: str | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def dates_ordered(self):
        if self.title_date_from and self.title_date_to and self.title_date_from > self.title_date_to:
            raise ValueError("The title-date range must be ordered")
        return self


def build_moments(data, broadcast, track_results):
    profile_sha256 = digest(profile())
    sources = {s["id"]: s for s in data["sources"]}
    evidence = {e["id"]: e for e in data["evidence"]}
    moments = []

    def add(
        kind,
        item_id,
        sid,
        start,
        end,
        title,
        note,
        evidence_ids,
        *,
        manifest=None,
        model=None,
        current_method=True,
        acceptance="proposed",
        view="composite",
        reference=None,
    ):
        src = sources.get(sid)
        if src is None:
            return
        frames = [
            evidence[i]
            for i in dict.fromkeys(evidence_ids)
            if i in evidence and evidence[i]["source_id"] == sid
        ]
        moments.append(
            {
                "id": f"{kind}:{item_id}",
                "kind": kind,
                "source_id": sid,
                "source_title": src["title"],
                "sheet": src.get("sheet"),
                "title_date": src.get("title_date"),
                "start_seconds": start,
                "end_seconds": end,
                "title": title,
                "notice": note,
                "acceptance": acceptance,
                "view": view,
                "reference": reference or {},
                "source_metadata_sha256": manifest,
                "model_version": model,
                "earlier_inputs": not current_method or bool(manifest and manifest != src["metadata_sha256"]),
                "start_youtube_url": video_link(src, start),
                "end_youtube_url": video_link(src, end),
                "evidence": [
                    {
                        "id": f["id"],
                        "source_seconds": f["source_seconds"],
                        "youtube_url": video_link(src, f["source_seconds"]),
                    }
                    for f in sorted(frames, key=lambda f: f["source_seconds"])
                ],
            }
        )

    for c in broadcast.get("board_timeline", {}).get("changes", []):
        add(
            "board_change",
            c["id"],
            c["source_id"],
            c["before"]["source_seconds"],
            c["after"]["source_seconds"],
            f"{c['color'].title()} board: {c['change_type'].replace('_', ' ')}",
            "A bracket between repeated posted-card readings, not the time an end finished.",
            c["evidence_ids"],
            manifest=c["source_metadata_sha256"],
            model=c["model_version"],
            reference={"board_change_id": c["id"]},
        )
    for p in broadcast.get("score_proposals", []):
        add(
            "score_proposal",
            p["id"],
            p["source_id"],
            p["source_start_seconds"],
            p["source_end_seconds"],
            f"End {p['official_number']}: {p['color']} +{p['points']} proposed",
            "Difference of repeated cumulative board columns. Game, end, and card completeness need review.",
            p["evidence_ids"],
            manifest=p["source_metadata_sha256"],
            model=p["model_version"],
            reference={"score_proposal_id": p["id"]},
        )
    for w in data["analysis_windows"]:
        current = (
            w["version"] == WINDOW_VERSION
            and w["model_version"] == DETECTOR_VERSION
            and w.get("visibility_version") == VISIBILITY_VERSION
            and w.get("decoder_version") == DECODER_VERSION
            and w.get("profile_sha256") == profile_sha256
        )
        for c in w["candidates"]:
            add(
                "house_configuration",
                digest({"window_id": w["id"], **c}),
                w["source_id"],
                c["start_seconds"],
                c["end_seconds"],
                f"{c['candidate_count']} persistent handle candidates",
                "Repeated image-space configuration. Hidden stones, end phase, and score remain unknown.",
                c["evidence_ids"],
                manifest=w["source_metadata_sha256"],
                model=w["model_version"],
                current_method=current,
                view=c["view"],
                reference={"window_id": w["id"]},
            )
    for result in track_results:
        for t in result["tracks"]:
            if t["motion_class"] != "position_change_observed":
                continue
            add(
                "handle_motion",
                t["id"],
                result["source_id"],
                t["start_seconds"],
                t["end_seconds"],
                f"{t['color'].title()} handle position change · {t['net_displacement_px']:.1f} pixels",
                (
                    "Proposed sampled association, not a verified delivery or physical stone identity. "
                    "Retained images are window context, not every path point."
                ),
                result["context_anchor_evidence_ids"],
                manifest=result["source_metadata_sha256"],
                model=result["version"],
                current_method=result["version"] == TRACKING_VERSION
                and result["detector_version"] == DETECTOR_VERSION
                and result["visibility_version"] == VISIBILITY_VERSION
                and result.get("profile_sha256") == profile_sha256,
                view=t["view"],
                reference={
                    "window_id": result["window_id"],
                    "track_result_id": result["id"],
                    "track_id": t["id"],
                },
            )
    for game in data["games"]:
        if game.get("retired_into"):
            continue
        for end in game["ends"]:
            if end["state"] in ("unknown", "unplayed"):
                continue
            by_source = defaultdict(list)
            for eid in end["evidence_ids"]:
                if eid in evidence:
                    by_source[evidence[eid]["source_id"]].append(evidence[eid])
            for sid, frames in by_source.items():
                times = [f["source_seconds"] for f in frames]
                add(
                    "reviewed_end",
                    f"{game['id']}:{end['id']}:{sid}",
                    sid,
                    min(times),
                    max(times),
                    (
                        f"{game['label']} · end {end['official_number'] or end['observed_index']} "
                        f"· {end['red_points']}–{end['yellow_points']}"
                    ),
                    (
                        "Saved end interpretation. Times locate evidence frames, not the start or completion "
                        "of play. Game boundary acceptance is separate."
                    ),
                    [f["id"] for f in frames],
                    acceptance=end["acceptance"],
                    reference={"game_id": game["id"], "game_revision": game["revision"], "end_id": end["id"]},
                )
    return sorted(
        moments,
        key=lambda m: (m["title_date"] or "", m["sheet"] or 0, m["source_id"], m["start_seconds"], m["id"]),
    )


def search_index(data, moments, query):
    known_sources = {s["id"] for s in data["sources"]}
    if query.source_id and query.source_id not in known_sources:
        raise ValueError("Recording not found")
    fingerprint = digest({"version": VERSION, "moments": moments})
    if query.expected_index_sha256 and query.expected_index_sha256 != fingerprint:
        raise Conflict("The evidence index changed. Run the search again before paging.")
    matches = []
    terms = query.text.casefold().split()
    for m in moments:
        if (
            query.source_id
            and m["source_id"] != query.source_id
            or query.sheet
            and m["sheet"] != query.sheet
            or query.kinds
            and m["kind"] not in query.kinds
            or query.acceptance
            and m["acceptance"] != query.acceptance
            or not query.include_earlier_inputs
            and m["earlier_inputs"]
        ):
            continue
        title_date = m["title_date"]
        if query.title_date_from and (not title_date or title_date < query.title_date_from.isoformat()):
            continue
        if query.title_date_to and (not title_date or title_date > query.title_date_to.isoformat()):
            continue
        haystack = f"{m['title']} {m['source_title']} {m['kind']}".casefold()
        if not all(term in haystack for term in terms):
            continue
        matches.append(m)
    page = matches[query.offset : query.offset + query.limit]
    return {
        "version": VERSION,
        "index_sha256": fingerprint,
        "query": query.model_dump(mode="json"),
        "total": len(matches),
        "by_kind": dict(Counter(m["kind"] for m in matches)),
        "moments": page,
        "next_offset": query.offset + query.limit if query.offset + query.limit < len(matches) else None,
        "coverage": {
            "inventoried_recordings": len(known_sources),
            "indexed_recordings": len({m["source_id"] for m in moments}),
            "indexed_moments": len(moments),
            "earlier_input_moments": sum(m["earlier_inputs"] for m in moments),
        },
        "notice": (
            "Search covers retained observations and saved ends, not every event in the archive. "
            "Title dates are recording labels, not established wall-clock dates. "
            "No matches does not mean no play occurred."
        ),
    }


def search_moments(db, query):
    with db.snapshot_session() as session:
        data = catalog_in_session(session)
        broadcast = broadcast_catalog_in_session(session, db.settings.data_dir)
        tracks = tracks_in_session(session, full=True)
        return search_index(data, build_moments(data, broadcast, tracks), query)
