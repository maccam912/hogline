"""Bounded recording workspaces, ordered admission and fair sharing of workers."""

from sqlalchemy import select

from hogline.db import ProcessingBatch, queue_field


def recording_turns(session, slots=1):
    return list(
        session.scalars(
            select(ProcessingBatch.id)
            .where(
                ProcessingBatch.state.in_(["active", "paused"]),
                queue_field(ProcessingBatch, "campaign_purpose") == "recording_analysis",
            )
            .order_by(queue_field(ProcessingBatch, "created_at"), ProcessingBatch.id)
            .limit(slots)
        )
    )
