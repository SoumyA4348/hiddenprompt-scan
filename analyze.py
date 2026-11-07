"""analyze: interim census numbers from data/census.sqlite (or another DB given as argument).

Verdicts are re-derived from the stored lexicon hits with the current hiddenprompt-scan rules, so rows
scanned by older detector versions are scored the same way as new ones.

    python analyze.py [path/to/census.sqlite] [--show]     # --show prints every prompt's hidden text

Prompts with one lexicon hit are listed separately: all 18 of Lin's known positives have two or
more distinct hits, and the one false positive seen so far (dialogue quoted inside a figure) had one.
Single-hit prompts are checked by hand before they are counted.
"""
import json
import math
import sqlite3
import sys

import hiddenprompt_scan


def verdict(hits: list[str], hidden_chars: int) -> str:
    strong = [h for h in hits if h not in hiddenprompt_scan.WEAK]
    asks = [h for h in hits if hiddenprompt_scan.INTENT.get(h) != "address"]
    if strong and asks:
        return "prompt"
    return "suspect" if hits else "hidden_text" if (hidden_chars or 0) >= hiddenprompt_scan.MIN_CHARS else "clean"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), c + h


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    db = next((a for a in argv if not a.startswith("--")), "data/census.sqlite")
    con = sqlite3.connect(db)
    rows = con.execute("SELECT name, yymm, category, hits, hidden_chars, reasons, runs FROM scans WHERE status='ok'").fetchall()
    papers, prompts, by_year, by_field = set(), [], {}, {}
    for name, yymm, cat, hits, chars, reasons, runs in rows:
        v = verdict(hits.split(",") if hits else [], chars)
        papers.add(name.rsplit("v", 1)[0])
        y = "20" + yymm[:2]
        field = (cat or "unknown").split(".")[0]
        for key, d in ((y, by_year), (field, by_field)):
            n, k = d.get(key, (0, 0))
            d[key] = (n + 1, k + (v == "prompt"))
        if v == "prompt":
            prompts.append((name, cat, hits, reasons, runs))
    n, k = len(rows), len(prompts)
    lo, hi = wilson(k, n)
    print(f"{n} PDFs ({len(papers)} papers) scanned OK; prompts: {k} PDFs = {k / max(n, 1):.4%} (95% CI {lo:.4%}-{hi:.4%})")
    multi = sum(len(p[2].split(",")) > 1 for p in prompts)
    print(f"   multi-hit: {multi}   single-hit (check by hand): {k - multi}")
    print("by year:  " + "  ".join(f"{y} {k_}/{n_}" for y, (n_, k_) in sorted(by_year.items())))
    top = sorted(by_field.items(), key=lambda x: -x[1][0])[:12]
    print("by field: " + "  ".join(f"{f} {k_}/{n_}" for f, (n_, k_) in top))
    if "--show" in argv:
        for name, cat, hits, reasons, runs in prompts:
            print(f"\n{name} [{cat}] hits={hits} reasons={reasons}")
            for r in json.loads(runs or "[]")[:4]:
                print(f"   p{r['page']} {r['reasons']} {r['text'][:300]!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
