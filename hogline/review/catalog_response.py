"""Coalesce archive polling and reuse unchanged serialized catalog bytes briefly."""

import hashlib
import threading
import time
from collections import OrderedDict

from fastapi.responses import Response


class CatalogResponseCache:
    def __init__(self, *, ttl=2, clock=time.monotonic, max_entries=1):
        self.ttl, self.clock = ttl, clock
        self.max_entries = max_entries
        self.lock = threading.Lock()
        self.generation = 0
        self.entries = OrderedDict()

    def invalidate(self):
        # Never block the event loop behind an archive build in a worker thread.
        self.generation += 1

    def response(self, loader, if_none_match=None, *, key=None):
        with self.lock:
            generation = self.generation
            entry = self.entries.get(key)
            if entry is None or entry[0] != generation or self.clock() >= entry[1]:
                body = loader().body
                etag = '"' + hashlib.sha256(body).hexdigest() + '"'
                entry = (generation, self.clock() + self.ttl, body, etag)
                self.entries[key] = entry
            self.entries.move_to_end(key)
            while len(self.entries) > self.max_entries:
                self.entries.popitem(last=False)
            _, _, body, etag = entry
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if if_none_match and etag in [v.strip() for v in if_none_match.split(",")]:
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/json", headers=headers)
