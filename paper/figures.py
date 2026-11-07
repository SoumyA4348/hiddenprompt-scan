"""figures: the paper's two figures and the aggregate CSVs behind them (no paper IDs).

    python paper/figures.py      # run from the repo root; needs data/ from a census run
"""
import csv
import math
import os
import sqlite3
import sys
from collections import Counter

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, ".")
import final  # noqa: E402

OUT = "paper/figures"
DATA = "results"
BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#d9d8d4"

plt.rcParams.update({
    "font.family": "serif", "font.size": 8.5, "axes.edgecolor": MUTED, "axes.labelcolor": INK,
    "xtick.color": MUTED, "ytick.color": MUTED, "axes.spines.top": False, "axes.spines.right": False,
    "pdf.fonttype": 42, "hatch.linewidth": 0.6,
})


def review_papers():
    c = sqlite3.connect("data/census.sqlite")
    lab = {**final.labels("verified_prompts.tsv"), **final.labels("verified_audit.tsv")}
    info = {r[0]: r[1:] for r in c.execute("SELECT name, paper, yymm, category FROM scans WHERE status='ok'")}
    win = {n: l for n, l in lab.items() if l in final.TRUE and n in info and info[n][1] >= "2301"}
    papers = {info[n][0]: info[n] for n, l in win.items() if l == "review"}
    months = [f"{y:02d}{m:02d}" for y in range(23, 27) for m in range(1, 13) if f"{y:02d}{m:02d}" <= "2610"]
    vol = dict(c.execute("SELECT yymm, COUNT(DISTINCT paper) FROM scans WHERE status='ok' AND yymm >= '2301' GROUP BY yymm"))
    return papers, months, vol


def fig_monthly(papers, months, vol):
    n = Counter(p[1] for p in papers.values())
    with open(f"{DATA}/review_prompts_by_month.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["month", "papers_scanned", "papers_with_review_prompt"])
        for m in months:
            w.writerow([f"20{m[:2]}-{m[2:]}", vol.get(m, 0), n.get(m, 0)])
    fig, ax = plt.subplots(figsize=(6.3, 2.2))
    x = range(len(months))
    ax.bar(x, [n.get(m, 0) for m in months], width=0.8, color=BLUE, edgecolor="white", linewidth=0.6)
    press = months.index("2507")
    ax.axvline(press, color=MUTED, linestyle="--", linewidth=0.8)
    ax.text(press + 0.6, max(n.values()) * 0.93, "first press reports\n(July 2025)", color=MUTED, fontsize=7.5, va="top")
    ticks = [i for i, m in enumerate(months) if m[2:] in ("01", "07")]
    ax.set_xticks(ticks, [f"{'Jan' if months[i][2:] == '01' else 'Jul'} 20{months[i][:2]}" for i in ticks])
    ax.set_ylabel("papers")
    ax.set_xlim(-0.8, len(months) - 0.2)
    ax.yaxis.grid(True, color=GRID, linewidth=0.5)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(f"{OUT}/fig_monthly.pdf")
    fig.savefig(f"{OUT}/fig_monthly.png", dpi=200)


def sign_p(up, down):
    """Two-sided exact sign test."""
    n, k = up + down, min(up, down)
    return 1.0 if n == 0 else min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def obedience_csv(db, conds, out):
    """Per-model paired comparison of each condition against the control review of the same paper."""
    by = {}
    for n, m, c, s, d, can in sqlite3.connect(db).execute(
            "SELECT name, model, condition, score, decision, canary FROM reviews"):
        by.setdefault(m, {}).setdefault(n, {})[c] = (s, d, can)
    rows = []
    for m, papers in sorted(by.items()):
        ctrl = [p for p in papers.values() if "control" in p and p["control"][0] is not None]
        if not ctrl:
            continue
        row = {"model": m, "n_papers": len(ctrl), "control_mean": round(sum(p["control"][0] for p in ctrl) / len(ctrl), 2),
               "control_accept": sum(p["control"][1] == "accept" for p in ctrl)}
        for c in conds:
            pairs = [(p["control"], p[c]) for p in papers.values()
                     if "control" in p and c in p and p["control"][0] is not None and p[c][0] is not None]
            diff = [b[0] - a[0] for a, b in pairs]
            up, down = sum(x > 0 for x in diff), sum(x < 0 for x in diff)
            row.update({f"{c}_shift": round(sum(diff) / len(diff), 2), f"{c}_up": up, f"{c}_down": down,
                        f"{c}_flips": sum(a[1] != "accept" and b[1] == "accept" for a, b in pairs),
                        f"{c}_p": f"{sign_p(up, down):.1e}"})
        can = [p["canary"][2] for p in papers.values() if "canary" in p]
        row["canary"] = f"{sum(can)}/{len(can)}"
        rows.append(row)
    keys = list(rows[0])
    for r in rows:
        keys += [k for k in r if k not in keys]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def read(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


NAMES = {"amazon.nova-lite-v1:0": "Nova Lite", "amazon.nova-pro-v1:0": "Nova Pro", "deepseek.v3.2": "DeepSeek V3.2",
         "mistral.mistral-large-3-675b-instruct": "Mistral Large 3", "openai.gpt-oss-120b-1:0": "gpt-oss-120b",
         "qwen.qwen3-next-80b-a3b": "Qwen3-Next 80B", "us.meta.llama3-3-70b-instruct-v1:0": "Llama 3.3 70B",
         "us.meta.llama4-maverick-17b-instruct-v1:0": "Llama 4 Maverick"}


def bars(ax, rows, series, title):
    rows = sorted(rows, key=lambda r: float(r[series[0][0] + "_shift"]))
    k = len(series)
    h = 0.8 / k
    for j, (cond, label, color, hatch) in enumerate(series):
        ys = [i + (k - 1 - j) * h - 0.4 + h / 2 for i in range(len(rows))]
        vals = [float(r[cond + "_shift"]) for r in rows]
        ax.barh(ys, vals, height=h * 0.92, color=color, hatch=hatch, edgecolor="white", linewidth=0.5, label=label)
        for y, v in zip(ys, vals):
            ax.text(v + 0.03 if v >= 0 else 0.03, y, f"{v:+.2f}", va="center", fontsize=6.5, color=INK)
    ax.set_yticks(range(len(rows)), [NAMES[r["model"]] for r in rows])
    ax.axvline(0, color=MUTED, linewidth=0.6)
    ax.set_xlim(-0.2, 2.6)
    ax.xaxis.grid(True, color=GRID, linewidth=0.5)
    ax.set_axisbelow(True)
    ax.set_title(title, fontsize=8.5, color=INK, loc="left")
    ax.set_xlabel("mean score change vs. clean paper (1-10 scale)")
    ax.legend(frameon=False, fontsize=7, loc="lower right")


def fig_obedience():
    text = read(f"{DATA}/obedience_text.csv")
    pdf = read(f"{DATA}/obedience_pdf.csv")
    fig, (a, b) = plt.subplots(2, 1, figsize=(6.3, 5.2), gridspec_kw={"height_ratios": [1.6, 1]})
    bars(a, text, [("positive", "hidden prompt", BLUE, None), ("defended", "prompt + reviewer warning", ORANGE, "////")],
         "(a) paper given as extracted text, 60 papers")
    bars(b, pdf, [("positive", "hidden prompt", BLUE, None), ("wild2", "second common wording", AQUA, "...."),
                  ("stripped", "prompt removed by hiddenprompt-scan", YELLOW, "\\\\\\\\")],
         "(b) paper given as a PDF file, 43 papers")
    fig.tight_layout()
    fig.savefig(f"{OUT}/fig_obedience.pdf")
    fig.savefig(f"{OUT}/fig_obedience.png", dpi=200)


def aggregates(papers):
    c = sqlite3.connect("data/census.sqlite")
    fields = Counter((p[2] or "unknown").split(".")[0] for p in papers.values())
    with open(f"{DATA}/review_prompts_by_field.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["field", "papers_with_review_prompt"])
        w.writerows(fields.most_common())
    with open(f"{DATA}/scan_totals.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["corpus", "units", "count"])
        w.writerow(["arXiv 2023-01..2026-10", "papers", c.execute(
            "SELECT COUNT(DISTINCT paper) FROM scans WHERE status='ok' AND yymm >= '2301'").fetchone()[0]])
        w.writerow(["arXiv 2023-01..2026-10", "PDFs", c.execute(
            "SELECT COUNT(*) FROM scans WHERE status='ok' AND yymm >= '2301'").fetchone()[0]])
        w.writerow(["arXiv 2022 control (v1)", "papers", c.execute(
            "SELECT COUNT(*) FROM scans WHERE status='ok' AND yymm < '2301'").fetchone()[0]])
        for server in ("biorxiv", "medrxiv"):
            w.writerow([server, "preprint versions", c.execute(
                "SELECT COUNT(*) FROM rxiv WHERE status='ok' AND server=?", (server,)).fetchone()[0]])


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)
    obedience_csv("data/phase2.sqlite", ["positive", "defended"], f"{DATA}/obedience_text.csv")
    obedience_csv("data/phase2c.sqlite", ["positive", "wild2", "stripped"], f"{DATA}/obedience_pdf.csv")
    papers, months, vol = review_papers()
    fig_monthly(papers, months, vol)
    fig_obedience()
    aggregates(papers)
    print(f"{len(papers)} review-prompt papers; figures in {OUT}, CSVs in {DATA}")
