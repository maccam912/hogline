"""Coalesce archive polling and reuse unchanged serialized catalog bytes briefly."""

import hashlib
import threading
import time

from fastapi.responses import Response


class CatalogResponseCache:
    def __init__(self, *, ttl=2, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self.lock = threading.Lock()
        self.generation = 0
        self.entry = None

    def invalidate(self):
        # Never block the event loop behind an archive build in a worker thread.
        self.generation += 1

    def response(self, loader, if_none_match=None):
        with self.lock:
            generation = self.generation
            entry = self.entry
            if entry is None or entry[0] != generation or self.clock() >= entry[1]:
                body = loader().body
                etag = '"' + hashlib.sha256(body).hexdigest() + '"'
                entry = (generation, self.clock() + self.ttl, body, etag)
                self.entry = entry
            _, _, body, etag = entry
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if if_none_match and etag in [v.strip() for v in if_none_match.split(",")]:
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/json", headers=headers)
