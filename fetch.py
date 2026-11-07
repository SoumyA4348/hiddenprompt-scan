"""fetch: download a PDF over HTTP range requests, skipping the bytes of large images.

Text extraction never needs image data, and images are about two thirds of an arXiv
PDF. We read the classic xref table from the end of the file, fetch every object
except the bodies of large ones, read each large object's header, and skip it only
if it is an image. The result is a full-length buffer with zeros where those image
bytes were; `fill` fetches them later if a scan needs to render the page faithfully.

Files that are not a single classic xref table (xref streams, incremental updates)
raise Fallback, and the caller downloads them whole.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

TAIL = 64 * 1024        # first request: the end of the file, where the xref table lives
LARGE = 64 * 1024       # objects bigger than this are fetched header-first
HEAD = 2048             # enough to hold an object's dictionary
CHUNK = 64 * 1024
IMAGE = re.compile(rb"/Subtype\s*/Image\b")


class Fallback(Exception):
    """This file needs a plain full download."""


class Lean:
    def __init__(self, size: int):
        self.data = bytearray(size)
        self.got = 0            # bytes downloaded
        self.requests = 0
        self.skipped = []       # (start, end) byte ranges left as zeros


def _get(client: httpx.Client, url: str, rng: str, throttle) -> tuple[bytes, int]:
    """One range request. Returns (body, total file size)."""
    for attempt in range(5):
        try:
            with client.stream("GET", url, headers={"Range": f"bytes={rng}"}) as r:
                if r.status_code == 206:
                    total = int(r.headers["Content-Range"].rsplit("/", 1)[1])
                    buf = bytearray()
                    for chunk in r.iter_bytes(CHUNK):
                        throttle(len(chunk))
                        buf += chunk
                    return bytes(buf), total
                if r.status_code != 429 and r.status_code < 500:
                    raise Fallback(f"HTTP {r.status_code} for a range request")
        except httpx.HTTPError:
            pass
        time.sleep(2 ** attempt)          # transient network trouble or server throttling: back off
    raise IOError(url)


def _fetch_ranges(client, url, throttle, pool, lean: Lean, ranges):
    def one(se):
        body, _ = _get(client, url, f"{se[0]}-{se[1] - 1}", throttle)
        if len(body) != se[1] - se[0]:
            raise Fallback("short range read")
        return se, body
    for (s, e), body in pool.map(one, [r for r in ranges if r[1] > r[0]]):
        lean.data[s:e] = body
        lean.got += len(body)
        lean.requests += 1


def _xref_offsets(region: bytes) -> list[int]:
    """Byte offsets of in-use objects from a classic xref table ('xref ... trailer')."""
    if not region.startswith(b"xref"):
        raise Fallback("xref stream, not a table")
    body, sep, trailer = region[4:].partition(b"trailer")
    if not sep or b"/Prev" in trailer[:4096] or b"/XRefStm" in trailer[:4096]:
        raise Fallback("incremental update or hybrid file")
    tok = body.split()
    offsets, i = [], 0
    try:
        while i < len(tok):
            count = int(tok[i + 1])
            i += 2
            for _ in range(count):
                if tok[i + 2] == b"n":
                    offsets.append(int(tok[i]))
                i += 3
    except (IndexError, ValueError):
        raise Fallback("unparsable xref table")
    return offsets


def fetch_lean(client: httpx.Client, url: str, throttle, pool: ThreadPoolExecutor) -> Lean:
    tail, size = _get(client, url, f"-{TAIL}", throttle)
    lean = Lean(size)
    tail_start = size - len(tail)
    lean.data[tail_start:] = tail
    lean.got, lean.requests = len(tail), 1
    if tail_start == 0:
        return lean                                   # small file: already complete
    k = tail.rfind(b"startxref")
    if k < 0:
        raise Fallback("no startxref")
    xref_at = int(tail[k + 9:].split()[0])
    if xref_at < tail_start:                           # big table: fetch the rest of it
        _fetch_ranges(client, url, throttle, pool, lean, [(xref_at, tail_start)])
        tail_start = xref_at
    offsets = sorted(o for o in _xref_offsets(bytes(lean.data[xref_at:])) if 0 < o < xref_at)
    if not offsets:
        raise Fallback("empty xref table")

    # Round 1: everything except the bodies of large objects (their first HEAD bytes are kept).
    ranges, large, cursor = [], [], 0
    for start, end in zip(offsets, offsets[1:] + [xref_at]):
        if end - start > LARGE:
            ranges.append((cursor, min(start + HEAD, tail_start)))
            large.append((start, end))
            cursor = end
    ranges.append((cursor, tail_start))
    _fetch_ranges(client, url, throttle, pool, lean, [(s, min(e, tail_start)) for s, e in ranges if s < tail_start])

    # Round 2: bodies of large objects that are not images (fonts, page content, vector figures).
    rest = []
    for start, end in large:
        head = bytes(lean.data[start:start + HEAD])
        k = head.find(b"stream")
        if k >= 0 and IMAGE.search(head[:k]):
            lean.skipped.append((start + HEAD, min(end, tail_start)))
        else:
            rest.append((start + HEAD, min(end, tail_start)))
    _fetch_ranges(client, url, throttle, pool, lean, rest)
    return lean


def fill(client: httpx.Client, url: str, throttle, pool: ThreadPoolExecutor, lean: Lean):
    """Download the image bytes that fetch_lean skipped, making the buffer the complete file."""
    _fetch_ranges(client, url, throttle, pool, lean, lean.skipped)
    lean.skipped = []
