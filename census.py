"""census: scan arXiv PDFs for hidden text and hidden LLM-reviewer prompts.

Data source: arXiv's public bulk mirror on Google Cloud (gs://arxiv-dataset), read over
plain HTTPS with no account and no cost to us. Each PDF is streamed into memory,
scanned with hiddenprompt-scan, and discarded; only the scan result is stored. By default PDFs are
fetched lean (range requests that skip large images, see fetch.py); image bytes are
fetched only when a scan result could depend on them.

    python census.py list 2507 2508                 # cache month listings
    python census.py run 2301-2610                  # scan (resumable, Ctrl+C safe)
    python census.py validate                       # recall on known positives
    python census.py stats                          # counts per month and verdict

Runs at full speed by default. --gentle restores the low-impact mode: background
priority, 4 workers, 5.5 MB/s, 500 GB/day and 250 GB per run. Either way the run
pauses while the laptop is on battery (unless --on-battery).
To stop a long run, create the file data/STOP; run the same command again to resume.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import multiprocessing as mp
import os
import pathlib
import queue
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pymupdf

import fetch
import hiddenprompt_scan

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
DB = DATA / "census.sqlite"
LISTINGS = DATA / "listings"
STOP = DATA / "STOP"
API = "https://storage.googleapis.com/storage/v1/b/arxiv-dataset/o"
PDF_URL = "https://storage.googleapis.com/arxiv-dataset/arxiv/arxiv/pdf/{yymm}/{name}.pdf"
TIMEOUT_S = 600                      # per PDF, download + scan
RECYCLE = 300                        # PDFs per worker process before it is replaced (frees MuPDF memory)
CHUNK = 64 * 1024
STAMP = re.compile(r"arXiv:\d{4}\.\d{4,5}v\d+\s+\[([^\]]+)\]\s+(\d{1,2} [A-Z][a-z]{2} \d{4})")

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    name TEXT PRIMARY KEY,          -- arXiv id + version, e.g. 2507.06185v1
    paper TEXT, version INTEGER, yymm TEXT, bytes INTEGER, posted TEXT,  -- posted = mirror upload time
    status TEXT, pages INTEGER, category TEXT, stamp_date TEXT,
    hidden_chars INTEGER, reasons TEXT, hits TEXT, visible_hits TEXT, meta_hits TEXT,
    verdict TEXT, intent TEXT, runs TEXT, page_errors INTEGER,
    download_ms INTEGER, scan_ms INTEGER, scanner_version TEXT, scanned_at TEXT,
    fetched INTEGER, fetch TEXT     -- bytes actually downloaded; lean, lean+fill or full
);
CREATE INDEX IF NOT EXISTS scans_verdict ON scans(verdict);
CREATE INDEX IF NOT EXISTS scans_yymm ON scans(yymm);
"""
COLS = ["name", "paper", "version", "yymm", "bytes", "posted", "status", "pages", "category", "stamp_date",
        "hidden_chars", "reasons", "hits", "visible_hits", "meta_hits", "verdict", "intent", "runs",
        "page_errors", "download_ms", "scan_ms", "scanner_version", "scanned_at", "fetched", "fetch"]


# ---------- listings ----------

def listing(yymm: str) -> list[tuple[str, int, str]]:
    """All PDF objects for one month as (name, bytes, upload time), cached under data/listings."""
    path = LISTINGS / f"{yymm}.json.gz"
    if path.exists():
        return [tuple(x) for x in json.loads(gzip.decompress(path.read_bytes()))]
    items, token = [], None
    with httpx.Client(timeout=60) as c:
        while True:
            params = {"prefix": f"arxiv/arxiv/pdf/{yymm}/", "fields": "nextPageToken,items(name,size,timeCreated)",
                      "maxResults": 1000, **({"pageToken": token} if token else {})}
            r = c.get(API, params=params)
            r.raise_for_status()
            j = r.json()
            items += [(i["name"].rsplit("/", 1)[1][:-4], int(i["size"]), i["timeCreated"][:19])
                      for i in j.get("items", []) if i["name"].endswith(".pdf")]
            token = j.get("nextPageToken")
            if not token:
                break
    LISTINGS.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(items).encode()))
    return items


def split_name(name: str) -> tuple[str, int]:
    paper, v = name.rsplit("v", 1)
    return paper, int(v)


def select(items, versions: str, sample: float, seed: str):
    """Pick which PDFs to scan: every version, first, latest, or first and latest ("ends"); optional paper sample."""
    latest = {}
    for name, *_ in items:
        paper, v = split_name(name)
        if v > latest.get(paper, 0):
            latest[paper] = v
    out = []
    for item in items:
        paper, v = split_name(item[0])
        if (versions == "v1" and v != 1 or versions == "latest" and v != latest[paper]
                or versions == "ends" and v not in (1, latest[paper])):
            continue
        if sample < 1 and int(hashlib.sha1(f"{seed}:{paper}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF >= sample:
            continue
        out.append(item)
    return out


# ---------- machine etiquette ----------

def background_priority():
    """Lowest CPU, disk and memory priority (--gentle), so the laptop stays responsive."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.SetPriorityClass(k.GetCurrentProcess(), 0x00100000)   # PROCESS_MODE_BACKGROUND_BEGIN: low disk/memory priority
        k.SetPriorityClass(k.GetCurrentProcess(), 0x00000040)   # then IDLE_PRIORITY_CLASS: CPU only when free
    else:
        os.nice(10)


def on_battery() -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    class Power(ctypes.Structure):
        _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("percent", ctypes.c_ubyte),
                    ("saver", ctypes.c_ubyte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]
    p = Power()
    return bool(ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(p))) and p.ac == 0


class Throttle:
    """Caps total download speed across all worker processes (shared virtual clock, 1 s burst). rate <= 0: no cap."""

    def __init__(self, clock, rate: float):
        self.clock, self.rate = clock, rate

    def __call__(self, n: int):
        if self.rate <= 0:
            return
        with self.clock.get_lock():
            now = time.time()
            t = max(self.clock.value, now - 1.0) + n / self.rate
            self.clock.value = t
        if t > now:
            time.sleep(t - now)


def download(client: httpx.Client, url: str, throttle) -> bytes | None:
    """GET url with every chunk paced by the throttle. None if the mirror has no such file."""
    for attempt in range(6):
        try:
            with client.stream("GET", url) as r:
                if r.status_code == 404:
                    return None
                if r.status_code == 200:
                    buf = bytearray()
                    for chunk in r.iter_bytes(CHUNK):
                        throttle(len(chunk))
                        buf += chunk
                    return bytes(buf)
        except httpx.HTTPError:
            pass
        time.sleep(2 ** attempt)
    raise IOError(url)


# ---------- one PDF ----------

def scan_bytes(data: bytes):
    """hiddenprompt-scan report, plus (category, stamp date) from the page-1 arXiv stamp and whether MuPDF had to repair the file."""
    rep = hiddenprompt_scan.scan(data)
    stamp, repaired = None, False
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            repaired = doc.is_repaired
            m = STAMP.search(doc[0].get_text()) if doc.page_count else None
            stamp = m.groups() if m else None
    except Exception:
        pass
    return rep, stamp, repaired


def needs_images(rep: dict, repaired: bool) -> bool:
    """Could this lean-fetch result change once the skipped image bytes are present?
    Only the render-based checks (light and covered text) and damaged files depend on images."""
    return bool(repaired or rep["status"] != "ok" or rep.get("page_errors") or rep.get("visible_hits")
                or any({"light", "covered"} & set(r["reasons"]) for r in rep.get("runs", [])))


def process(client: httpx.Client, throttle, pool, lean_ok: bool, name: str, size: int, posted: str) -> dict:
    paper, version = split_name(name)
    yymm = paper[:4]
    url = PDF_URL.format(yymm=yymm, name=name)
    row = {"name": name, "paper": paper, "version": version, "yymm": yymm, "bytes": size, "posted": posted,
           "scanner_version": hiddenprompt_scan.__version__, "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    t0 = time.time()
    lean = None
    try:
        try:
            if not lean_ok:
                raise fetch.Fallback("lean fetch disabled")
            lean = fetch.fetch_lean(client, url, throttle, pool)
            data, mode = bytes(lean.data), "lean"
        except fetch.Fallback:
            lean = None
            data, mode = download(client, url, throttle), "full"
    except IOError:
        return {**row, "status": "download_error"}
    if data is None:
        return {**row, "status": "missing"}
    t1 = time.time()
    rep, stamp, repaired = scan_bytes(data)
    if lean and lean.skipped and needs_images(rep, repaired):
        t = time.time()
        try:
            fetch.fill(client, url, throttle, pool, lean)
        except (IOError, fetch.Fallback):
            return {**row, "status": "download_error"}
        t1 += time.time() - t
        data, mode = bytes(lean.data), "lean+fill"
        rep, stamp, repaired = scan_bytes(data)
    row.update(download_ms=int((t1 - t0) * 1000), scan_ms=int((time.time() - t1) * 1000), status=rep["status"],
               fetched=lean.got if lean else len(data), fetch=mode)
    if stamp:
        row.update(category=stamp[0], stamp_date=stamp[1])
    if rep["status"] != "ok":
        return row
    keep_runs = rep["runs"] != []   # any hidden text at all, so new lexicon rules can be re-applied offline
    row.update(
        pages=rep["pages"], hidden_chars=rep["hidden_chars"], verdict=rep["verdict"], intent=rep["intent"],
        page_errors=rep["page_errors"], reasons=json.dumps(rep["reasons"]) if rep["reasons"] else None,
        hits=",".join(rep["hits"]) or None, visible_hits=",".join(rep["visible_hits"]) or None,
        meta_hits=",".join(rep["meta_hits"]) or None,
        runs=json.dumps([{**r, "text": r["text"][:500]} for r in rep["runs"][:20]], ensure_ascii=False)
        if keep_runs else None,
    )
    return row


def worker(wid: int, tasks, results, clock, rate, gentle, lean_ok):
    if gentle:
        background_priority()
    client = httpx.Client(timeout=120, follow_redirects=True,
                          limits=httpx.Limits(max_connections=32, max_keepalive_connections=16))
    pool = ThreadPoolExecutor(16)            # parallel range requests within one PDF
    throttle = Throttle(clock, rate)
    parent = mp.parent_process()
    for _ in range(RECYCLE):
        while True:
            try:
                item = tasks.get(timeout=10)
                break
            except queue.Empty:
                if parent is not None and not parent.is_alive():
                    return              # main process is gone; don't linger
        if item is None:
            return
        results.put(("start", wid, item[0], time.time()))
        try:
            row = process(client, throttle, pool, lean_ok, *item)
        except Exception as e:  # never let one PDF kill the worker
            paper, v = split_name(item[0])
            row = {"name": item[0], "paper": paper, "version": v, "yymm": paper[:4], "bytes": item[1],
                   "posted": item[2], "status": "error", "runs": json.dumps({"error": str(e)[:300]})}
        results.put(("done", wid, row))
    results.put(("exit", wid))               # recycle: the main process starts a fresh worker


# ---------- runner ----------

def db():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(scans)")}
    for col, typ in (("fetched", "INTEGER"), ("fetch", "TEXT")):   # databases from before lean fetching
        if col not in have:
            con.execute(f"ALTER TABLE scans ADD COLUMN {col} {typ}")
    return con


def save(con, rows):
    con.executemany(f"INSERT OR REPLACE INTO scans ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
                    [[r.get(c) for c in COLS] for r in rows])
    con.commit()


def run(months, versions="all", sample=1.0, seed="census", workers=None, limit=None,
        rate_mb=0.0, daily_gb=0.0, allow_battery=False, session_gb=0.0, gentle=False, lean_ok=True, shard="0/1"):
    """rate_mb, daily_gb and session_gb of 0 mean no limit. shard "k/n" scans only the k-th of n disjoint slices."""
    if gentle:
        background_priority()
        workers = workers or 4
        rate_mb, daily_gb, session_gb = rate_mb or 5.5, daily_gb or 500.0, session_gb or 250.0
    workers = workers or max(4, (os.cpu_count() or 8) - 2)
    day_cap, run_cap = daily_gb * 1e9 or float("inf"), session_gb * 1e9 or float("inf")
    con = db()
    done = {n for (n,) in con.execute("SELECT name FROM scans WHERE status NOT IN ('download_error', 'crash', 'timeout')")}
    todo = []
    for m in months:
        todo += [it for it in select(listing(m), versions, sample, seed) if it[0] not in done]
    todo.sort(key=lambda it: hashlib.sha1(it[0].encode()).hexdigest())   # stable random order: interim counts stay unbiased
    k, n = map(int, shard.split("/"))
    if n > 1:
        todo = [it for it in todo if int(hashlib.sha1(it[0].encode()).hexdigest()[:8], 16) % n == k]
    if limit:
        todo = todo[:limit]
    total_bytes = sum(it[1] for it in todo)
    print(f"{len(todo)} PDFs to scan ({total_bytes / 1e9:.1f} GB) across {months[0]}..{months[-1]}; "
          f"{len(done)} already in the database; {workers} workers, "
          f"{'lean' if lean_ok else 'full'} fetch, pace {rate_mb or 'unlimited'} MB/s, "
          f"caps {daily_gb or 'none'} GB/day and {session_gb or 'none'} GB/run", flush=True)
    if not todo:
        return
    STOP.unlink(missing_ok=True)

    tasks, results = mp.Queue(maxsize=workers * 4), mp.Queue()
    clock = mp.Value("d", 0.0)
    procs, inflight = {}, {}

    def spawn(wid):
        p = mp.Process(target=worker, args=(wid, tasks, results, clock, rate_mb * 1e6, gentle, lean_ok), daemon=True)
        p.start()
        procs[wid] = p

    for wid in range(workers):
        spawn(wid)
    fed, got, buf = 0, 0, []
    info = {it[0]: it for it in todo}
    t_start = last_print = last_save = last_power = time.time()
    done_bytes, fetched, verdicts, modes, stopping, pause = 0, 0, {}, {}, False, None
    day = time.strftime("%Y-%m-%d")
    day_bytes = con.execute("SELECT COALESCE(SUM(COALESCE(fetched, bytes)), 0) FROM scans WHERE scanned_at >= ?",
                            (day,)).fetchone()[0]
    battery = not allow_battery and on_battery()
    try:
        while got < fed or not stopping and got < len(todo):
            if not stopping and STOP.exists():
                stopping = True
                print("STOP file found: finishing PDFs in flight, then exiting", flush=True)
            if not stopping and fetched >= run_cap:
                stopping = True
                print(f"[{time.strftime('%m-%d %H:%M')}] run cap of {session_gb:.0f} GB reached: "
                      "finishing PDFs in flight, then exiting", flush=True)
            if time.time() - last_power > 30:
                battery, last_power = not allow_battery and on_battery(), time.time()
            if time.strftime("%Y-%m-%d") != day:
                day, day_bytes = time.strftime("%Y-%m-%d"), 0
            why = "on battery" if battery else "daily cap reached" if day_bytes >= day_cap else None
            if why != pause:
                print(f"[{time.strftime('%m-%d %H:%M')}] " + (f"paused: {why}" if why else "resumed"), flush=True)
                pause = why
            while not stopping and not pause and fed < len(todo):
                try:
                    tasks.put_nowait(todo[fed])
                except queue.Full:
                    break
                fed += 1
            try:
                msg = results.get(timeout=2)
            except queue.Empty:
                msg = None
            if msg and msg[0] == "start":
                inflight[msg[1]] = (msg[2], msg[3])
            elif msg and msg[0] == "exit":
                procs.pop(msg[1]).join()
                spawn(msg[1])
            elif msg:
                inflight.pop(msg[1], None)
                row = msg[2]
                buf.append(row)
                got += 1
                done_bytes += info[row["name"]][1]
                n = row.get("fetched") or 0
                fetched += n
                day_bytes += n
                key = row.get("verdict") or row["status"]
                verdicts[key] = verdicts.get(key, 0) + 1
                modes[row.get("fetch")] = modes.get(row.get("fetch"), 0) + 1
            now = time.time()
            for wid, p in list(procs.items()):           # replace crashed or stuck workers
                name_t = inflight.get(wid)
                stuck = name_t and now - name_t[1] > TIMEOUT_S
                if p.is_alive() and not stuck:
                    continue
                if stuck:
                    p.kill()
                    p.join()
                if name_t:
                    paper, v = split_name(name_t[0])
                    _, size, posted = info[name_t[0]]
                    buf.append({"name": name_t[0], "paper": paper, "version": v, "yymm": paper[:4], "bytes": size,
                                "posted": posted, "status": "timeout" if stuck else "crash"})
                    got += 1
                    inflight.pop(wid, None)
                spawn(wid)
            if buf and (len(buf) >= 200 or now - last_save > 15):
                save(con, buf)
                buf, last_save = [], now
            if now - last_print > 300:
                pdf_rate = got / (now - t_start)
                print(f"[{time.strftime('%m-%d %H:%M')}] {got}/{len(todo)} PDFs  {pdf_rate:.1f}/s  "
                      f"downloaded {fetched / 1e9:.1f} GB for {done_bytes / 1e9:.1f} GB of PDFs  "
                      f"{fetched / (now - t_start) / 1e6:.1f} MB/s  today {day_bytes / 1e9:.0f} GB  "
                      f"~{(len(todo) - got) / max(pdf_rate, 1e-9) / 86400:.1f} days left  {verdicts}  {modes}", flush=True)
                last_print = now
    except KeyboardInterrupt:
        print("interrupted; saving progress", flush=True)
    finally:
        if buf:
            save(con, buf)
        for p in procs.values():
            p.kill()
        print(f"done: {got} PDFs in {(time.time() - t_start) / 60:.1f} min, downloaded {fetched / 1e9:.1f} GB  "
              f"{verdicts}  {modes}", flush=True)


# ---------- validation and stats ----------

def validate():
    pos = [l.split() for l in (DATA / "validation" / "known_positives.txt").read_text().splitlines()
           if l.strip() and not l.startswith("#")]
    pdfs = DATA / "validation" / "pdfs"
    pdfs.mkdir(parents=True, exist_ok=True)
    found = 0
    with httpx.Client(timeout=120) as c:
        for name, kind in pos:
            f = pdfs / f"{name}.pdf"
            if not f.exists():
                r = c.get(PDF_URL.format(yymm=name[:4], name=name))
                r.raise_for_status()
                f.write_bytes(r.content)
            rep = hiddenprompt_scan.scan(str(f))
            found += rep.get("verdict") == "prompt"
            print(f"{name:14} {kind:17} {rep.get('verdict', rep['status']):11} {rep.get('intent') or '':10} "
                  f"{','.join(rep.get('reasons', {}))}  hits={','.join(rep.get('hits', []))}")
    print(f"recall: {found}/{len(pos)}")


def stats():
    con = db()
    print("month  pdfs    prompt suspect hidden_text other")
    for yymm, n, p, s, h, o in con.execute("""
        SELECT yymm, COUNT(*), SUM(verdict='prompt'), SUM(verdict='suspect'), SUM(verdict='hidden_text'),
               SUM(status!='ok') FROM scans GROUP BY yymm ORDER BY yymm"""):
        print(f"{yymm}  {n:6}  {p:6} {s:7} {h:11} {o:5}")


def expand_months(args) -> list[str]:
    """'2301-2610' style ranges (or single YYMM values) to a list of YYMM months."""
    out = []
    for a in args:
        lo, _, hi = a.partition("-")
        y, m = int(lo[:2]), int(lo[2:])
        end = (int((hi or lo)[:2]), int((hi or lo)[2:]))
        while (y, m) <= end:
            out.append(f"{y:02d}{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("months", nargs="+")
    p = sub.add_parser("run")
    p.add_argument("months", nargs="+")
    p.add_argument("--versions", choices=["all", "v1", "latest", "ends"], default="all")
    p.add_argument("--sample", type=float, default=1.0, help="fraction of papers to scan (stable hash sample)")
    p.add_argument("--seed", default="census")
    p.add_argument("--workers", type=int, help="worker processes (default: CPU count - 2)")
    p.add_argument("--limit", type=int)
    p.add_argument("--rate", type=float, default=0.0, help="download pace in MB/s shared by all workers (0 = no cap)")
    p.add_argument("--daily-gb", type=float, default=0.0, help="GB per calendar day (0 = no cap)")
    p.add_argument("--session-gb", type=float, default=0.0, help="stop this run after this many GB (0 = no cap)")
    p.add_argument("--on-battery", action="store_true", help="keep running when the laptop is unplugged")
    p.add_argument("--gentle", action="store_true", help="low-impact mode: background priority, 4 workers, "
                                                         "5.5 MB/s, 500 GB/day, 250 GB per run")
    p.add_argument("--full-fetch", action="store_true", help="download whole PDFs instead of lean range fetches")
    p.add_argument("--shard", default="0/1", help="k/n: scan only the k-th of n disjoint slices (for parallel machines)")
    sub.add_parser("validate")
    sub.add_parser("stats")
    a = ap.parse_args()
    if a.cmd in ("list", "run"):
        a.months = expand_months(a.months)
    if a.cmd == "list":
        for m in a.months:
            items = listing(m)
            print(f"{m}: {len(items)} PDFs, {sum(it[1] for it in items) / 1e9:.1f} GB")
    elif a.cmd == "run":
        run(a.months, a.versions, a.sample, a.seed, a.workers, a.limit, a.rate, a.daily_gb, a.on_battery,
            a.session_gb, a.gentle, not a.full_fetch, a.shard)
    elif a.cmd == "validate":
        validate()
    else:
        stats()


if __name__ == "__main__":
    main()
