"""final: the paper's numbers, from the census DB plus the hand-verified label files.

Sources of candidates, each verified by hand (labels in data/validation/, private because they hold IDs):
    verified_prompts.tsv   rule-based `prompt` verdicts (arXiv)
    verified_audit.tsv     LLM screen (Nova Micro) -> judge (DeepSeek V3.2) positives (arXiv)
    verified_rxiv.tsv      rule-based + judge positives (bioRxiv / medRxiv, keyed by S3 key)
Labels: review (asks an AI reviewer for a better evaluation), canary (detects AI review), citation,
agent_instruction (other AI-directed instruction), false_positive, unclear.

    python final.py            # numbers; also lists judge positives that still need a hand label
"""
import json
import sqlite3
import sys
from collections import Counter

import analyze

TRUE = {"review", "canary", "citation", "agent_instruction"}
V = "data/validation/"


def labels(name):
    try:
        lines = open(V + name, encoding="utf-8").read().splitlines()[1:]
    except FileNotFoundError:
        return {}
    return dict(l.split("\t") for l in lines if "\t" in l)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    c = sqlite3.connect("data/census.sqlite")
    lab = {**labels("verified_prompts.tsv"), **labels("verified_audit.tsv")}
    rlab = labels("verified_rxiv.tsv")

    # audit coverage and unlabeled judge positives
    try:
        a = sqlite3.connect("data/audit.sqlite")
        j = sqlite3.connect("data/judge.sqlite")
        screened = dict(a.execute("SELECT name, flagged FROM audit"))
        judged = dict(j.execute("SELECT name, flagged FROM audit"))
    except sqlite3.OperationalError:
        screened, judged = {}, {}
    todo_scans = c.execute("SELECT COUNT(*) FROM scans WHERE status='ok' AND verdict IN ('hidden_text','suspect')").fetchone()[0]
    todo_rxiv = c.execute("SELECT COUNT(*) FROM rxiv WHERE status='ok' AND verdict IN ('hidden_text','suspect')").fetchone()[0]
    print(f"audit coverage: screened {len(screened)} of {todo_scans + todo_rxiv} hidden-text rows "
          f"(screen flags {sum(1 for v in screened.values() if v)}, judged {len(judged)}, "
          f"judge positives {sum(1 for v in judged.values() if v)})")
    pending = [n for n, v in judged.items() if v and n not in lab and n not in rlab]
    if pending:
        print(f"!! {len(pending)} judge positives need a hand label (add them to verified_audit.tsv / verified_rxiv.tsv)")

    # arXiv, main window 2301-2610 (v1 + latest), per paper
    info = {r[0]: r[1:] for r in c.execute(
        "SELECT name, paper, version, yymm, category, reasons FROM scans WHERE status='ok'")}
    true = {n: l for n, l in lab.items() if l in TRUE and n in info}
    window = {n: l for n, l in true.items() if info[n][2] >= "2301"}
    base = {n: l for n, l in true.items() if info[n][2] < "2301"}
    papers_all = c.execute("SELECT COUNT(DISTINCT paper) FROM scans WHERE status='ok' AND yymm >= '2301'").fetchone()[0]
    review_papers = {info[n][0]: n for n, l in window.items() if l == "review"}
    any_papers = {info[n][0]: n for n in window}
    lo, hi = analyze.wilson(len(review_papers), papers_all)
    print(f"\narXiv 2301-2610: {papers_all} papers; review prompts in {len(review_papers)} papers = "
          f"{len(review_papers) / papers_all:.4%} (95% CI {lo:.4%}-{hi:.4%}); any AI-directed hidden instruction: "
          f"{len(any_papers)} papers; by label {Counter(window.values())}")
    print("  by year (papers / per 100k):", end="")
    for y in ("23", "24", "25", "26"):
        d = c.execute("SELECT COUNT(DISTINCT paper) FROM scans WHERE status='ok' AND yymm LIKE ?", (y + "%",)).fetchone()[0]
        k = sum(1 for p, n in review_papers.items() if info[n][2].startswith(y))
        print(f"  20{y} {k} ({k / d * 1e5:.1f})", end="")
    print()
    fields = Counter((info[n][3] or "unknown").split(".")[0] for n in review_papers.values())
    print("  by field:", fields.most_common())
    print("  cs subfields:", Counter(info[n][3] for n in review_papers.values() if (info[n][3] or "").startswith("cs.")).most_common())
    tech = Counter()
    for n in review_papers.values():
        r = json.loads(info[n][4]) if (info[n][4] or "").startswith("{") else {}
        tech[" + ".join(sorted(k for k in r if k in ("light", "tiny", "transparent", "invisible", "covered", "offpage", "layer")))] += 1
    print("  technique:", tech.most_common())
    removed = kept = added = single = 0
    for p in review_papers:
        vs = [(v, n in window) for n, v in c.execute("SELECT name, version FROM scans WHERE paper=? AND status='ok' ORDER BY version", (p,))]
        if len(vs) < 2:
            single += 1
        elif vs[0][1] and not vs[-1][1]:
            removed += 1
        elif vs[0][1] and vs[-1][1]:
            kept += 1
        elif not vs[0][1] and vs[-1][1]:
            added += 1
    print(f"  persistence: removed in latest {removed}, kept {kept}, added later {added}, single version {single}")
    b = c.execute("SELECT COUNT(*) FROM scans WHERE status='ok' AND yymm < '2301'").fetchone()[0]
    print(f"\narXiv 2022 baseline (v1, pre-ChatGPT): {len(base)} AI-directed hidden instructions in {b} papers")

    # bioRxiv / medRxiv
    print()
    for server in ("biorxiv", "medrxiv"):
        n = c.execute("SELECT COUNT(*) FROM rxiv WHERE status='ok' AND server=?", (server,)).fetchone()[0]
        keys = {k for (k,) in c.execute("SELECT key FROM rxiv WHERE server=?", (server,))}
        t = Counter(l for k, l in rlab.items() if k in keys and l in TRUE)
        print(f"{server}: {n} preprint versions; AI-directed hidden instructions {sum(t.values())} {dict(t)}")


if __name__ == "__main__":
    main()
