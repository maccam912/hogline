from fastapi.testclient import TestClient

from hogline.review.app import create_app


def test_empty_workbench_and_origin_guard(db):
    app = create_app(db.settings)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-cache"
        assert "/static/discovery.js?v=" in page.text
        assert client.get("/static/discovery.js").headers["cache-control"] == "no-cache"
        assert client.get("/api/catalog").json()["sources"] == []
        assert client.get("/api/health").json()["status"] == "ok"
        assert (
            client.post("/api/releases", json={}, headers={"Origin": "https://evil.invalid"}).status_code
            == 403
        )
        response = client.post("/api/releases", json={"mode": "confirmed"})
        assert response.status_code == 200
        release_id = response.json()["id"]
        exported = client.get(f"/api/releases/{release_id}.json")
        assert exported.json()["games"] == []
        assert client.get(f"/api/releases/{release_id}.csv").text.startswith("row_type,game_id")
        assert client.get(f"/api/releases/{release_id}.grouped-csv").status_code == 409
        proposed = client.post("/api/releases", json={"mode": "exploratory"}).json()
        grouped = client.get(f"/api/releases/{proposed['id']}.grouped-csv")
        assert grouped.status_code == 200 and grouped.headers["content-type"].startswith("text/csv")
        assert grouped.text.startswith("score_history_id,continuation_group_id,source_ids")
        assert "-grouped.csv" in grouped.headers["content-disposition"]
        assert client.get("/api/evidence/missing/composite").status_code == 404


def test_invalid_url_and_scan_fail_cleanly(db):
    with TestClient(create_app(db.settings)) as client:
        assert (
            client.post("/api/sources", json={"url": "https://example.com/watch?v=AAAAAAAAAAA"}).status_code
            == 422
        )
        assert client.post("/api/scans", json={"source_id": "missing", "end_seconds": 50}).status_code == 422
