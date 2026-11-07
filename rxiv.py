"""rxiv: the same hidden-prompt census for bioRxiv and medRxiv, read from their requester-pays S3 buckets.

Each preprint version is a .meca bundle (a zip of the PDF, JATS XML, figures and supplements). Only the
zip directory, the main PDF and the XML are read, with ranged GETs, so supplements are never downloaded.
Run it on EC2 in us-east-1 (same region as the buckets, so transfer is free); results go to the
`rxiv` table of data/census.sqlite.

    python rxiv.py run biorxiv:2021-01..2026-10 medrxiv:2021-01..2026-10 [--workers 3] [--shard k/n] [--limit N]
    python rxiv.py stats
"""
import hashlib
import io
import json
import multiprocessing as mp
import os
import pathlib
import re
import signal
import sqlite3
import sys
import time
import zipfile

import boto3

import hiddenprompt_scan

BUCKETS = {"biorxiv": "biorxiv-src-monthly", "medrxiv": "medrxiv-src-monthly"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
DB = pathlib.Path(os.environ.get("RXIV_DB", "data/census.sqlite"))
BLOCK = 512 * 1024
TIMEOUT = 300


class RangeFile(io.RawIOBase):
    """Read-only file over an S3 object, fetched in cached blocks with ranged GETs."""

    def __init__(self, s3, bucket, key, size):
        self.s3, self.bucket, self.key, self.size, self.pos = s3, bucket, key, size, 0
        self.cache, self.fetched = {}, 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def _block(self, i):
        if i not in self.cache:
            start, end = i * BLOCK, min(self.size, (i + 1) * BLOCK) - 1
            r = self.s3.get_object(Bucket=self.bucket, Key=self.key, Range=f"bytes={start}-{end}",
                                   RequestPayer="requester")
            self.cache[i] = r["Body"].read()
            self.fetched += len(self.cache[i])
        return self.cache[i]

    def _fetch(self, start, end):  # one GET for a long span instead of many blocks
        r = self.s3.get_object(Bucket=self.bucket, Key=self.key, Range=f"bytes={start}-{end - 1}",
                               RequestPayer="requester")
        data = r["Body"].read()
        self.fetched += len(data)
        return data

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        if n == 0:
            return b""
        start, end = self.pos, self.pos + n
        if n > 4 * BLOCK:
            data = self._fetch(start, end)
        else:
            data = b"".join(self._block(i) for i in range(start // BLOCK, (end - 1) // BLOCK + 1))
            data = data[start - (start // BLOCK) * BLOCK:][:n]
        self.pos = end
        return data


def months(spec):
    a, b = spec.split("..")
    (y1, m1), (y2, m2) = map(int, a.split("-")), map(int, b.split("-"))
    out = []
    while (y1, m1) <= (y2, m2):
        out.append(f"{MONTHS[m1 - 1]}_{y1}")
        y1, m1 = (y1 + 1, 1) if m1 == 12 else (y1, m1 + 1)
    return out


def db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS rxiv (key TEXT, server TEXT, month TEXT, doi TEXT, "
                "category TEXT, modified TEXT, pdf_bytes INTEGER, fetched INTEGER, status TEXT, pages INTEGER, "
                "hidden_chars INTEGER, reasons TEXT, hits TEXT, visible_hits TEXT, verdict TEXT, runs TEXT, "
                "scanner_version TEXT, error TEXT, scan_ms INTEGER, PRIMARY KEY (server, key))")
    return con


def _alarm(*_):
    raise TimeoutError("scan timeout")


def work(task):
    server, month, key, size, modified = task
    alarm = hasattr(signal, "SIGALRM")  # Linux (EC2); a local Windows test simply has no timeout
    if alarm:
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(TIMEOUT)
    row = {"key": key, "server": server, "month": month, "modified": modified, "status": "error"}
    t0 = time.time()
    try:
        s3 = work.s3
        f = RangeFile(s3, BUCKETS[server], key, size)
        with zipfile.ZipFile(f) as z:
            infos = z.infolist()
            pdfs = [i for i in infos if i.filename.lower().endswith(".pdf") and i.filename.startswith("content/")
                    and "supplement" not in i.filename.lower()]
            xmls = [i for i in infos if i.filename.lower().endswith(".xml") and i.filename.startswith("content/")]
            if xmls:
                x = z.read(min(xmls, key=lambda i: i.file_size)).decode("utf-8", "replace")
                m = re.search(r'pub-id-type="doi"[^>]*>([^<]+)<', x)
                row["doi"] = m.group(1).strip() if m else None
                m = re.search(r'subj-group-type="hwp-journal-coll"[^>]*>\s*<subject>([^<]+)<', x)
                row["category"] = m.group(1).strip() if m else None
            if not pdfs:
                row["status"] = "no_pdf"
            else:
                pdf = max(pdfs, key=lambda i: i.file_size)
                data = z.read(pdf)
                row["pdf_bytes"] = len(data)
                rep = hiddenprompt_scan.scan(data)
                row.update(status=rep["status"], pages=rep.get("pages"), hidden_chars=rep.get("hidden_chars"),
                           reasons=json.dumps(rep.get("reasons", {})), hits=",".join(rep.get("hits", [])),
                           visible_hits=",".join(rep.get("visible_hits", [])), verdict=rep.get("verdict"),
                           runs=json.dumps(rep.get("runs", []), ensure_ascii=False), scanner_version=rep["version"])
        row["fetched"] = f.fetched
    except Exception as e:
        row["error"] = f"{e.__class__.__name__}: {e}"[:300]
    finally:
        if alarm:
            signal.alarm(0)
    row["scan_ms"] = int((time.time() - t0) * 1000)
    return row


def init_worker():
    work.s3 = boto3.client("s3", region_name="us-east-1")


def run(argv):
    opt = lambda k, d: argv[argv.index(k) + 1] if k in argv else d
    targets = [a.split(":") for a in argv if ":" in a and not a.startswith("--")]
    workers, limit = int(opt("--workers", 3)), int(opt("--limit", 0))
    k, n = map(int, opt("--shard", "0/1").split("/"))
    s3 = boto3.client("s3", region_name="us-east-1")
    con = db()
    done = {(r[0], r[1]) for r in con.execute("SELECT server, key FROM rxiv WHERE status != 'error'")}
    tasks = []
    for server, spec in targets:
        for month in months(spec):
            for page in s3.get_paginator("list_objects_v2").paginate(
                    Bucket=BUCKETS[server], Prefix=f"Current_Content/{month}/", RequestPayer="requester"):
                for it in page.get("Contents", []):
                    key = it["Key"]
                    if not key.endswith(".meca") or (server, key) in done:
                        continue
                    if int(hashlib.sha1(key.encode()).hexdigest(), 16) % n != k:
                        continue
                    tasks.append((server, month, key, it["Size"], it["LastModified"].isoformat()))
    if limit:
        tasks = tasks[:limit]
    print(f"{len(tasks)} preprints to scan in {targets} (shard {k}/{n}), {len(done)} done", flush=True)
    cols = ["key", "server", "month", "doi", "category", "modified", "pdf_bytes", "fetched", "status", "pages",
            "hidden_chars", "reasons", "hits", "visible_hits", "verdict", "runs", "scanner_version", "error", "scan_ms"]
    t0, count, verdicts = time.time(), 0, {}
    with mp.Pool(workers, initializer=init_worker, maxtasksperchild=200) as pool:
        for row in pool.imap_unordered(work, tasks, chunksize=4):
            con.execute(f"INSERT OR REPLACE INTO rxiv ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [row.get(c) for c in cols])
            count += 1
            v = row.get("verdict") or row["status"]
            verdicts[v] = verdicts.get(v, 0) + 1
            if count % 100 == 0:
                con.commit()
                rate = count / (time.time() - t0)
                print(f"[{time.strftime('%m-%d %H:%M')}] {count}/{len(tasks)} {rate:.1f}/s {verdicts}", flush=True)
                if pathlib.Path("data/STOP").exists():
                    pool.terminate()
                    break
    con.commit()
    print(f"done: {count} preprints in {(time.time() - t0) / 60:.1f} min {verdicts}", flush=True)


def stats():
    con = db()
    for r in con.execute("SELECT server, verdict, status, COUNT(*) FROM rxiv GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC"):
        print(*r)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.argv[1] == "run":
        run(sys.argv[2:])
    else:
        stats()
