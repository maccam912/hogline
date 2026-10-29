from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.responses import JSONResponse

from hogline.review.catalog_response import CatalogResponseCache


def test_concurrent_catalog_polls_share_one_build_and_unchanged_body():
    cache = CatalogResponseCache()
    entered, release = Event(), Event()
    calls = []

    def load():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return JSONResponse({"scores": [1, 2, None]})

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(cache.response, load) for _ in range(6)]
        assert entered.wait(2)
        release.set()
        responses = [f.result() for f in futures]
    assert len(calls) == 1 and len({r.body for r in responses}) == 1
    tag = responses[0].headers["etag"]
    unchanged = cache.response(load, tag)
    assert unchanged.status_code == 304 and unchanged.body == b""
    cache.invalidate()
    assert cache.response(load, tag).status_code == 304
    assert len(calls) == 2  # The rebuild may legitimately produce identical bytes.


def test_mutation_during_build_invalidates_that_snapshot_and_failures_are_not_cached():
    moment = [0]
    cache = CatalogResponseCache(clock=lambda: moment[0])

    def old():
        cache.invalidate()
        return JSONResponse({"revision": 1})

    first = cache.response(old)
    following = cache.response(lambda: JSONResponse({"revision": 2}), first.headers["etag"])
    assert following.status_code == 200 and following.body != first.body
    moment[0] = 3

    def fail():
        raise ValueError("Unavailable")

    with pytest.raises(ValueError, match="Unavailable"):
        cache.response(fail)
    assert cache.response(lambda: JSONResponse({"revision": 3})).body == b'{"revision":3}'
