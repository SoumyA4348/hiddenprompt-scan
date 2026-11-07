"""outcomes: did computer-science papers with a hidden review prompt do better after posting?

Each review-prompt paper in computer science is matched with K papers first submitted in the same month with
the same primary category and no AI-directed hidden text. For every paper we record
    published   a journal or conference version, either merged into the paper's OpenAlex record or reported by
                the authors on arXiv (journal reference, or a comment such as "Accepted at ..."), and
    citations   the OpenAlex citation count.
Lookups are cached in data/outcomes.json (holds IDs, private); aggregates go to paper/data/outcomes.csv.

    python outcomes.py
"""
import csv
import hashlib
import json
import math
import random
import re
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import analyze
import final

K = 5
CACHE = "data/outcomes.json"
OUT = "paper/data/outcomes.csv"
UA = {"User-Agent": "hidden-prompt-census"}
ATOM, ARXIV = "{http://www.w3.org/2005/Atom}", "{http://arxiv.org/schemas/atom}"
ACCEPTED = re.compile(r"\b(accepted|to appear|published|camera[- ]ready|proceedings)\b", re.I)
TOPIC = re.compile(r"peer[- ]review|reviewer|hidden prompt|prompt injection|review generation", re.I)


def get(url, tries=8):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return json.load(r)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1) if getattr(e, "code", 0) == 429 else 2 * (i + 1))


def cohort(c):
    """Match sets: (prompt paper, [control papers]) for review-prompt papers with a cs.* stamp."""
    lab = {**final.labels("verified_prompts.tsv"), **final.labels("verified_audit.tsv")}
    info = {r[0]: r[1:] for r in c.execute("SELECT name, paper, yymm, category FROM scans WHERE status='ok'")}
    flagged = {info[n][0] for n, l in lab.items() if l in final.TRUE and n in info}
    review = sorted({info[n][0] for n, l in lab.items() if l == "review" and n in info and info[n][1] >= "2301"})
    sets = []
    for p in review:
        yymm, cat = c.execute("SELECT yymm, category FROM scans WHERE paper=? AND status='ok' ORDER BY version",
                              (p,)).fetchone()
        if not (cat or "").startswith("cs."):
            continue
        pool = [q for (q,) in c.execute("SELECT DISTINCT paper FROM scans WHERE status='ok' AND yymm=? AND category=?",
                                        (yymm, cat)) if q not in flagged]
        pool.sort(key=lambda q: hashlib.sha1((p + q).encode()).hexdigest())
        sets.append((p, pool[:K]))
    return sets


def openalex(ids, cache):
    todo = [i for i in ids if i not in cache["openalex"]]
    for k in range(0, len(todo), 50):
        chunk = todo[k:k + 50]
        f = "|".join(f"10.48550/arXiv.{i}" for i in chunk)
        res = get("https://api.openalex.org/works?per_page=50&select=id,doi,display_name,cited_by_count,locations"
                  "&filter=doi:" + urllib.parse.quote(f, safe=":/|"))
        got = {}
        for w in res["results"]:
            links = [w.get("doi") or ""] + [x for loc in w.get("locations") or []
                                            for x in (loc.get("doi") or "", loc.get("landing_page_url") or "")]
            # a merged record carries the published DOI; the arXiv ID is then only in its locations
            venues = sorted({loc["source"]["display_name"] for loc in w.get("locations") or []
                             if (loc.get("source") or {}).get("type") in ("journal", "conference")})
            for x in links:
                m = re.search(r"arxiv[./](?:org/abs/)?(\d{4}\.\d{4,5})", x.lower())
                if m:
                    got[m.group(1)] = {"id": w["id"].rsplit("/", 1)[1], "title": w["display_name"],
                                       "citations": w["cited_by_count"], "venues": venues}
        for i in chunk:
            cache["openalex"][i] = got.get(i)
        save(cache)


def arxiv_meta(ids, cache):
    """Author-supplied journal reference and comment of the latest version, from the arXiv API."""
    todo = [i for i in ids if i not in cache["arxiv"]]
    for k in range(0, len(todo), 100):
        chunk = todo[k:k + 100]
        url = f"https://export.arxiv.org/api/query?max_results={len(chunk)}&id_list=" + ",".join(chunk)
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
            feed = ET.parse(r).getroot()
        for e in feed.findall(f"{ATOM}entry"):
            m = re.search(r"abs/(\d{4}\.\d{4,5})", e.findtext(f"{ATOM}id") or "")
            if m:
                cache["arxiv"][m.group(1)] = {"journal_ref": e.findtext(f"{ARXIV}journal_ref") or "",
                                              "comment": e.findtext(f"{ARXIV}comment") or ""}
        save(cache)
        time.sleep(3)


def citing(ids, cache):
    """Titles of the works citing these papers, to see whether the citations come from coverage of hidden prompts."""
    wid = {cache["openalex"][i]["id"]: i for i in ids if cache["openalex"].get(i)}
    titles, cursor = [], "*"
    while cursor:
        res = get("https://api.openalex.org/works?per_page=200&select=id,display_name&cursor=" + cursor
                  + "&filter=cites:" + "|".join(wid))
        titles += [w["display_name"] or "" for w in res["results"]]
        cursor = res["meta"].get("next_cursor")
    return titles


def published(i, cache):
    """A journal or conference version: merged into the OpenAlex record, or reported by the authors on arXiv."""
    oa, ax = cache["openalex"].get(i) or {}, cache["arxiv"].get(i) or {}
    return bool(oa.get("venues") or ax.get("journal_ref") or ACCEPTED.search(ax.get("comment", "")))


def sign_p(up, down):
    """Two-sided exact sign test."""
    n, k = up + down, min(up, down)
    return 1.0 if n == 0 else min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def save(cache):
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    c = sqlite3.connect("data/census.sqlite")
    sets = cohort(c)
    try:
        cache = json.load(open(CACHE, encoding="utf-8"))
    except FileNotFoundError:
        cache = {}
    cache.setdefault("openalex", {})
    cache.setdefault("arxiv", {})
    ids = [p for p, ctl in sets for p in [p, *ctl]]
    openalex(ids, cache)
    arxiv_meta(ids, cache)

    found = lambda i: cache["openalex"].get(i) is not None  # noqa: E731
    pub = lambda i: published(i, cache)  # noqa: E731
    cit = lambda i: cache["openalex"][i]["citations"]  # noqa: E731
    prompts = [p for p, _ in sets]
    controls = [q for _, ctl in sets for q in ctl]

    k_p, k_c = sum(map(pub, prompts)), sum(map(pub, controls))
    # permutation test within match sets: under the null the prompt paper is any member of its own set
    rng = random.Random(0)
    groups = [[pub(p)] + [pub(q) for q in ctl] for p, ctl in sets]
    expected = sum(sum(g) / len(g) for g in groups)
    n_perm = 20000
    hits = sum(abs(sum(rng.choice(g) for g in groups) - expected) >= abs(k_p - expected) - 1e-9 for _ in range(n_perm))
    p_pub = hits / n_perm

    # citations: only papers OpenAlex knows; each prompt paper against the median of its found controls
    csets = [(p, [q for q in ctl if found(q)]) for p, ctl in sets if found(p)]
    csets = [(p, ctl) for p, ctl in csets if ctl]
    up = down = tie = 0
    for p, ctl in csets:
        m = statistics.median(cit(q) for q in ctl)
        up += cit(p) > m
        down += cit(p) < m
        tie += cit(p) == m
    rows = [
        ("match sets (cs papers with a review prompt)", len(sets)),
        ("control papers", len(controls)),
        ("prompt papers published", k_p),
        ("control papers published", k_c),
        ("prompt published %", round(100 * k_p / len(prompts), 1)),
        ("control published %", round(100 * k_c / len(controls), 1)),
        ("published permutation p", round(p_pub, 3)),
        ("match sets with citation data", len(csets)),
        ("prompt median citations", statistics.median(cit(p) for p, _ in csets)),
        ("control median citations", statistics.median(cit(q) for _, ctl in csets for q in ctl)),
        ("prompt above control median", up),
        ("prompt below control median", down),
        ("prompt tied with control median", tie),
        ("citations sign-test p", round(sign_p(up, down), 3)),
    ]
    titles = citing([p for p, _ in csets], cache)
    about = [t for t in titles if TOPIC.search(t)]
    rows += [("works citing prompt papers", len(titles)),
             ("of which about peer review or prompt injection", len(about))]
    print("  citing works on the topic:", about)
    lo_p, hi_p = analyze.wilson(k_p, len(prompts))
    lo_c, hi_c = analyze.wilson(k_c, len(controls))
    rows += [("prompt published 95% CI", f"{100 * lo_p:.1f}-{100 * hi_p:.1f}"),
             ("control published 95% CI", f"{100 * lo_c:.1f}-{100 * hi_c:.1f}")]
    missing = sum(1 for i in ids if not found(i))
    print(f"OpenAlex coverage: {len(ids) - missing} of {len(ids)} papers")
    for k, v in rows:
        print(f"  {k}: {v}")
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["measure", "value"])
        w.writerows(rows)


if __name__ == "__main__":
    main()
