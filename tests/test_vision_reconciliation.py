import json
from uuid import uuid4

import pytest

from hogline.adapters.sources import digest
from hogline.perception.vision_budget import budget_status, ledger, reconcile_completed_reservations


def legacy(root, state="completed", usage=None):
    identifier = str(uuid4())
    document = {
        "id": identifier,
        "model": "gpt-6.1-terra",
        "response_id": "response-test",
        "usage": usage or {"input_tokens": 1000, "output_tokens": 200},
    }
    with ledger(root) as connection:
        connection.execute(
            "INSERT INTO requests(id,reserved_cents,state,created_at) VALUES (?,100,?,'test')",
            (identifier, state),
        )
    (root / f"{identifier}.json").write_text(json.dumps(document))
    return identifier, document


def test_reviewed_completed_usage_reconciles_once_with_audit_and_retains_uncertain(tmp_path):
    identifier, document = legacy(tmp_path)
    uncertain, unknown = legacy(tmp_path, state="reserved")
    reviewed = {identifier: digest(document), uncertain: digest(unknown)}
    preview = reconcile_completed_reservations(tmp_path, reviewed, reason="Reviewed retained usage")
    assert len(preview["changes"]) == 1 and len(preview["retained"]) == 1
    assert budget_status(tmp_path)["committed_cents"] == 200
    result = reconcile_completed_reservations(
        tmp_path, reviewed, reason="Reviewed retained usage", apply=True
    )
    assert result["changes"][0]["after_cents"] == 1
    assert result["changes"][0]["legacy_checksum_absent"]
    assert budget_status(tmp_path)["committed_cents"] == 101
    assert reconcile_completed_reservations(tmp_path, reviewed, reason="Again", apply=True)["changes"] == []
    with ledger(tmp_path) as connection:
        assert connection.execute("SELECT count(*) FROM accounting_adjustments").fetchone()[0] == 1


def test_changed_or_incomplete_usage_cannot_release_reservation(tmp_path):
    identifier, document = legacy(tmp_path)
    with pytest.raises(ValueError, match="changed"):
        reconcile_completed_reservations(tmp_path, {identifier: "wrong"}, reason="Reviewed", apply=True)
    document["usage"] = {"input_tokens": 100}
    (tmp_path / f"{identifier}.json").write_text(json.dumps(document))
    result = reconcile_completed_reservations(
        tmp_path, {identifier: digest(document)}, reason="Reviewed", apply=True
    )
    assert result["changes"] == [] and result["retained"]
    assert budget_status(tmp_path)["committed_cents"] == 100
