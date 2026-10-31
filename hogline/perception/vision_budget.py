"""Atomic reservations and conservative token accounting for the local vision pilot."""

import json
import math
import sqlite3
from contextlib import contextmanager
from uuid import UUID

from hogline.adapters.sources import digest
from hogline.db import now

RESERVATION_CENTS = 100


@contextmanager
def ledger(root):
    root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(root / "budget.sqlite3", timeout=10) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS requests "
            "(id TEXT PRIMARY KEY, reserved_cents INTEGER, state TEXT, created_at TEXT)"
        )
        columns = {r[1] for r in connection.execute("PRAGMA table_info(requests)")}
        for name, kind in (("request_key", "TEXT"), ("charged_cents", "INTEGER"), ("result_sha256", "TEXT")):
            if name not in columns:
                connection.execute(f"ALTER TABLE requests ADD COLUMN {name} {kind}")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS vision_request_key ON requests(request_key)")
        yield connection


def reserve(root, identifier, request_key, budget):
    """Return a checked cached result or reserve once. Uncertain attempts never auto-retry."""
    with ledger(root) as connection:
        old = connection.execute(
            "SELECT id,state,result_sha256 FROM requests WHERE request_key=?", (request_key,)
        ).fetchone()
        if old:
            path = root / f"{old[0]}.json"
            if old[1] == "completed" and path.is_file():
                result = json.loads(path.read_text())
                if digest(result) != old[2]:
                    raise ValueError("Cached vision result checksum mismatch")
                return result
            raise ValueError(
                "This vision request has an unresolved prior attempt; inspect it before retrying"
            )
        committed = connection.execute(
            "SELECT coalesce(sum(coalesce(charged_cents,reserved_cents)),0) FROM requests"
        ).fetchone()[0]
        if committed + RESERVATION_CENTS > int(budget * 100):
            raise ValueError("Vision pilot reservation budget reached")
        connection.execute(
            "INSERT INTO requests(id,reserved_cents,state,created_at,request_key) "
            "VALUES (?,?,'reserved',?,?)",
            (identifier, RESERVATION_CENTS, now(), request_key),
        )
    return None


def conservative_cents(usage):
    """Upper pricing estimate, not billed spend. Unknown usage retains the full reservation.

    Terra pricing checked 2026-10-30: standard $1/$5 per million input/output;
    use $3/$9 to cover cache writes, long context and regional premiums. No tools/fast service.
    """
    if not isinstance(usage, dict):
        return None
    counts = [usage.get(k) for k in ("input_tokens", "output_tokens")]
    if any(type(n) is not int or n < 0 for n in counts):
        return None
    return max(1, math.ceil((counts[0] * 3 + counts[1] * 9) / 10000))


def complete(root, identifier, document):
    with ledger(root) as connection:
        connection.execute(
            "UPDATE requests SET state='completed',charged_cents=?,result_sha256=? WHERE id=?",
            (conservative_cents(document.get("usage")), digest(document), identifier),
        )


def budget_status(root):
    with ledger(root) as connection:
        rows = connection.execute(
            "SELECT state,count(*),sum(coalesce(charged_cents,reserved_cents)) FROM requests GROUP BY state"
        ).fetchall()
    return {
        "committed_cents": sum(r[2] for r in rows),
        "requests": {r[0]: r[1] for r in rows},
        "accounting": "conservative_estimate_plus_reservations",
    }


def reconcile_completed_reservations(root, reviewed_checksums, *, reason, apply=False):
    """Explicitly reconcile reviewed legacy successes, retaining uncertain attempts.

    Caller supplies the checksums inspected before applying. This seals legacy artifacts
    without claiming historical cryptographic verification or access to billed spend.
    """
    if not reason.strip():
        raise ValueError("Accounting reconciliation needs a review reason")
    changes, retained = [], []
    with ledger(root) as connection:
        for identifier, reviewed_checksum in reviewed_checksums.items():
            UUID(identifier)  # Legacy result paths must be plain identifiers.
            row = connection.execute(
                "SELECT state,reserved_cents,charged_cents,result_sha256 FROM requests WHERE id=?",
                (identifier,),
            ).fetchone()
            if row is None or row[0] != "completed" or row[2] is not None:
                retained.append({"id": identifier, "reason": "not_an_unreconciled_completed_request"})
                continue
            path = root / f"{identifier}.json"
            document = json.loads(path.read_text())
            checksum = digest(document)
            if checksum != reviewed_checksum or (row[3] is not None and row[3] != checksum):
                raise ValueError("Reviewed vision result changed or disagrees with the ledger")
            cost = conservative_cents(document.get("usage"))
            if (
                document.get("id") != identifier
                or document.get("model") != "gpt-6.1-terra"
                or not document.get("response_id")
                or cost is None
            ):
                retained.append({"id": identifier, "reason": "completed_usage_not_verifiable"})
                continue
            changes.append(
                {
                    "id": identifier,
                    "before_cents": row[1],
                    "after_cents": cost,
                    "result_sha256": checksum,
                    "reason": reason,
                    "legacy_checksum_absent": row[3] is None,
                }
            )
        if apply and changes:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS accounting_adjustments "
                "(request_id TEXT, created_at TEXT, document TEXT)"
            )
            for change in changes:
                connection.execute(
                    "UPDATE requests SET charged_cents=?,result_sha256=? WHERE id=?",
                    (change["after_cents"], change["result_sha256"], change["id"]),
                )
                connection.execute(
                    "INSERT INTO accounting_adjustments VALUES (?,?,?)",
                    (change["id"], now(), json.dumps(change)),
                )
    return {
        "applied": apply,
        "changes": changes,
        "retained": retained,
        "notice": "Conservative local estimates, not OpenAI billed spend",
    }
