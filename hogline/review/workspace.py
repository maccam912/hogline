"""Small archive summaries; load immutable evidence only for the selected recording."""

from sqlalchemy import select

from hogline.adapters.sources import source_view
from hogline.db import GameRow, Job, JobExecution, ProcessingBatch, ReviewTask, Source, rows_by_ids
from hogline.domain.catalog import present_game
from hogline.domain.reconciliation import reconciliations_in_session
from hogline.orchestration.plans import batch_view
from hogline.orchestration.worker import job_view

DETAIL_ARRAYS = (
    "scans",
    "analysis_windows",
    "evidence",
    "timelines",
    "observations",
    "board_locations",
    "window_tracks",
    "window_house_states",
    "window_actors",
    "window_deliveries",
    "source_boundary_links",
)
BATCH_FIELDS = (
    "request",
    "kind",
    "campaign_purpose",
    "created_at",
    "finished_at",
    "phase",
    "limits",
    "spent_worker_seconds",
    "reserved_image_bytes",
    "pause_reason",
    "analysis_outcome",
)


def present_jobs(session, jobs):
    executions = {
        e.id: e
        for e in rows_by_ids(
            session,
            JobExecution,
            JobExecution.id,
            [j.document["lease_token"] for j in jobs if j.document.get("lease_token")],
        )
    }
    return [
        {
            **job_view(j),
            "execution": {"state": e.state, **e.document}
            if (e := executions.get(j.document.get("lease_token")))
            else None,
        }
        for j in jobs
    ]


def overview_in_session(session):
    sources = {r.id: {**source_view(r.document), "id": r.id} for r in session.scalars(select(Source))}
    games = [present_game(r, sources) for r in session.scalars(select(GameRow))]
    # Selecting fixed JSON fields avoids deserializing saved reconstruction reports,
    # phase payloads and execution histories (hundreds of MB in the live archive).
    batches = []
    query = select(
        ProcessingBatch.id,
        ProcessingBatch.revision,
        ProcessingBatch.state,
        *(ProcessingBatch.document[k].label(k) for k in BATCH_FIELDS),
    )
    for row in session.execute(query).mappings():
        doc = {k: row[k] for k in BATCH_FIELDS if row[k] is not None}
        request = doc.get("request", {})
        batches.append(
            {
                **doc,
                "id": row["id"],
                "revision": row["revision"],
                "state": row["state"],
                "summary_only": True,
                "units": [],
                "executions": [],
                "phase_history": [],
                "succeeded_units": None,
                "deferred_units": 0,
                "limits": doc.get("limits")
                or {k: request.get(k, 0) for k in ("max_image_growth_mb", "max_worker_seconds")},
            }
        )
    live = [
        j
        for j in session.scalars(select(Job).where(Job.state.in_(["queued", "running"])))
        if j.state == "running" or not j.document.get("batch_id") or j.document.get("retry_pending")
    ]
    return {
        **{key: [] for key in DETAIL_ARRAYS},
        "sources": list(sources.values()),
        "games": [g for g in games if not g.get("retired_into")],
        "retired_games": [g for g in games if g.get("retired_into")],
        "review_tasks": [
            {"id": r.id, "game_id": r.game_id, "reason": r.reason, "state": r.state}
            for r in session.scalars(select(ReviewTask))
        ],
        "reconciliations": reconciliations_in_session(session),
        "batches": batches,
        "jobs": present_jobs(session, live),
    }


def source_processing_in_session(session, source_id):
    ids = [
        identifier
        for identifier, sources in session.execute(
            select(ProcessingBatch.id, ProcessingBatch.document["request"]["source_ids"])
        )
        if source_id in sources
    ]
    batches = list(rows_by_ids(session, ProcessingBatch, ProcessingBatch.id, ids))
    job_ids = {u["job_id"] for b in batches for u in b.document["units"]}
    jobs = {j.id: j for j in rows_by_ids(session, Job, Job.id, job_ids)}
    executions = list(rows_by_ids(session, JobExecution, JobExecution.batch_id, ids))
    return {
        "batches": [
            batch_view(session, b, jobs=jobs, executions=[e for e in executions if e.batch_id == b.id])
            for b in batches
        ],
        "jobs": present_jobs(session, list(jobs.values())),
    }
