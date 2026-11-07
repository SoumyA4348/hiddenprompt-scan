"""hiddenprompt-scan: find hidden text and hidden LLM-reviewer prompts in PDF files.

Hidden text is text that a PDF text extractor (and so any LLM reading the
file) receives, but that a person reading the rendered page cannot see:
white or background-coloured text, microscopic fonts, invisible render
mode, zero opacity, text placed off the page, text in switched-off layers,
and text covered by other content.

    python hiddenprompt_scan.py paper.pdf [more.pdf ...]          # one summary per file
    python hiddenprompt_scan.py --json paper.pdf                  # JSON lines
    python hiddenprompt_scan.py --strip paper.pdf clean.pdf       # copy with the hidden text redacted
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata

import pymupdf

__version__ = "0.1.1"   # 0.1.1: a prompt must ask for something, not only address an AI
pymupdf.TOOLS.mupdf_display_errors(False)

TINY_PT = 1.0       # glyphs smaller than this cannot be read on screen or paper
LIGHT = 0.9         # text this close to white may vanish on a white page
INK_MIN = 24        # grey-level spread (0-255) needed to call a span visible
INK_ZOOM = 2        # render at 144 dpi for the visibility check
MIN_CHARS = 20      # hidden runs shorter than this are layout noise
EXTRACT = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES  # no clipping to the page box
FILLED, STROKED = 16, 32

# Prompt lexicon, matched on normalised lower-case text: (id, intent, regex).
LEXICON = [
    ("ignore_instructions", "override",
     r"\b(?:ignore|disregard|forget|override)\b(?: all| any)?(?: of)?(?: the| your)?"
     r"(?: previous| prior| above| preceding| earlier| other| original| system)? "
     r"(?:instructions?|prompts?|directions?|commands?|guidelines?|rules?)\b"),
    ("positive_review", "manipulate",
     r"\b(?:give|write|provide|generate|produce|output|return|offer)\b(?: only)?(?: a| an)?"
     r"(?: very| highly| extremely| overwhelmingly| strongly| exclusively)? positive "
     r"(?:review|feedback|evaluation|assessment|comments?|score|rating)"),
    ("positive_only", "manipulate", r"\bpositive (?:review|feedback|evaluation)s? only\b"),
    ("hide_weaknesses", "manipulate",
     r"\b(?:do not|don't|never|avoid|without)\b (?:highlight|mention|point(?:ing)? out|discuss|list|includ|emphasi[sz]|rais)\w*"
     r"(?: any)?(?: of)?(?: the)? (?:negatives?|weakness(?:es)?|limitations?|flaws?|criticisms?|shortcomings?|drawbacks?|concerns?)"),
    ("downplay_weaknesses", "manipulate", r"\bweakness(?:es)?\b.{0,80}\b(?:very minor|easily fixable|downplay)"),
    ("recommend_accept", "manipulate",
     r"\b(?:recommend|suggest|advocate|argue|vote)\w*(?: for)?(?: the| a)?(?: strong)? (?:accept|acceptance|accepting)\b"),
    ("accept_paper", "manipulate",
     r"\b(?:strong(?:ly)? )?accept(?:ing)? (?:this|the) (?:paper|manuscript|submission|work|article)\b"),
    ("score_request", "manipulate",
     r"\b(?:highest|maximum|perfect|full|top) (?:possible )?(?:scores?|ratings?|marks|grades?)\b"),
    ("praise_frame", "manipulate",
     r"\b(?:groundbreaking|transformative|exceptional novelty|outstanding strengths|remarkable contributions|highly impactful)\b"),
    ("review_requirements", "manipulate", r"\breview requirements\b"),
    ("ai_reviewer", "address",
     r"\b(?:llm|ai|gpt|language[- ]model|automated|machine|bot)[- ](?:based |generated )?(?:reviewers?|reviewing|review(?:er)? note)\b"),
    ("if_you_are_ai", "address",
     r"\bif you are (?:an? |the )?(?:llm|ai\b|large language model|language model|chatbot|ai assistant|assistant|ai model|ai system|automated|bot)"),
    ("as_a_model", "address", r"\bas an? (?:llm|ai|large language model|language model|ai assistant|ai model)\b"),
    ("note_to_ai", "address",
     r"\b(?:note|instructions?|message|notice|attention|important)\b (?:to|for) (?:the |any |all )?"
     r"(?:llms?|ai|ais|reviewers?|language models?|assistants?|models?|automated)\b"),
    ("canary_word", "canary",
     r"\b(?:include|use|mention|insert|add|incorporate|put|start|end|begin)\w*\b(?: your| the)?(?: review| response| answer)?"
     r"(?: with)?(?: the)? (?:word|words|phrase|term|token|sentence|string|keyword)s?\b"),
    ("ai_ban", "deter",
     r"\b(?:do not|don't|must not|should not|never)\b (?:use|employ|rely on|consult|feed|upload|input|submit|paste|share)\b"
     r".{0,30}\b(?:ai|llms?|chatgpt|large language models?|language models?|generative ai|ai tools?)\b"
     r"|\b(?:ai|llms?|chatgpt|language models?|generative ai)\b.{0,30}\b(?:is|are) (?:strictly )?"
     r"(?:not allowed|prohibited|forbidden|not permitted)\b"),
    ("model_name", "address",
     r"\b(?:chatgpt|gpt-?[345](?:\.\d)?o?|claude|gemini|llama|copilot|deepseek|qwen|grok|mistral)\b"),
    ("cjk_override", "override", r"(?:忽略|无视|無視|忘记|忘記).{0,8}(?:指令|指示|提示|命令)"),
    ("cjk_positive", "manipulate", r"(?:正面|积极|積極|肯定|好意)的?(?:评价|評価|评审|審査|レビュー)|好评|高く評価|좋은 평가|긍정적"),
]
LEXICON = [(i, intent, re.compile(p)) for i, intent, p in LEXICON]
INTENT = {i: intent for i, intent, _ in LEXICON}
WEAK = {"model_name", "score_request", "praise_frame", "canary_word", "accept_paper"}  # need a second, stronger hit
PROBE = {"ignore_instructions", "positive_review", "positive_only", "hide_weaknesses", "recommend_accept",
         "review_requirements", "ai_reviewer", "if_you_are_ai", "note_to_ai", "cjk_override", "cjk_positive"}
_DROP = dict.fromkeys(map(ord, "­​‌‍⁠﻿"), None)
_QUOTES = str.maketrans("‘’“”", "''\"\"")


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_DROP).translate(_QUOTES)
    return re.sub(r"\s+", " ", text).strip().lower()


def lexicon_hits(text: str, only: set | None = None) -> list[str]:
    text = re.sub(r"(\w)- (\w)", r"\1\2", text)  # undo line-end hyphenation
    return [i for i, _, rx in LEXICON if (only is None or i in only) and rx.search(text)]


def _lum(srgb: int) -> float:
    return (0.299 * (srgb >> 16 & 255) + 0.587 * (srgb >> 8 & 255) + 0.114 * (srgb & 255)) / 255


def _spans(page):
    d = page.get_text("dict", flags=EXTRACT, clip=pymupdf.INFINITE_RECT())
    for block in d["blocks"]:
        for line in block.get("lines", ()):
            for s in line["spans"]:
                if s["text"].strip():
                    s["dir"] = line["dir"]
                    yield s


def _attr_reasons(s, page_rect) -> set:
    """Reasons a span is hidden that follow from its own attributes."""
    r = set()
    x0, y0, x1, y1 = s["bbox"]
    cos, sin = s["dir"]
    height = abs(cos) * (y1 - y0) + abs(sin) * (x1 - x0)  # extent across the text direction
    if min(s["size"], height) < TINY_PT:
        r.add("tiny")
    if not s["char_flags"] & (FILLED | STROKED):
        r.add("invisible")          # render mode 3: neither filled nor stroked
    elif s["alpha"] < 13:
        r.add("transparent")        # under 5% opacity
    box = pymupdf.Rect(s["bbox"])
    area = max(box.width * box.height, 1e-9)
    if (box & page_rect).get_area() < 0.2 * area:
        r.add("offpage")
    return r


class _Ink:
    """Renders a page once and answers 'does this box show any visible ink?'."""

    def __init__(self, page):
        self.page, self.pix = page, None

    def visible(self, bbox) -> bool:
        if self.pix is None:
            self.pix = self.page.get_pixmap(matrix=pymupdf.Matrix(INK_ZOOM, INK_ZOOM),
                                            colorspace=pymupdf.csGRAY, alpha=False)
        r = pymupdf.Rect(bbox)
        dx, dy = r.width * 0.2, r.height * 0.2   # central 60% of the box: the glyph bodies
        r = pymupdf.Rect(r.x0 + dx, r.y0 + dy, r.x1 - dx, r.y1 - dy) * INK_ZOOM
        r = pymupdf.Rect(r.x0 - self.pix.x, r.y0 - self.pix.y, r.x1 - self.pix.x, r.y1 - self.pix.y)
        ir = r.irect & pymupdf.IRect(0, 0, self.pix.width, self.pix.height)
        if ir.width < 1 or ir.height < 1:
            return False
        s, st = self.pix.samples, self.pix.stride
        px = bytearray()
        for y in range(ir.y0, ir.y1):
            px += s[y * st + ir.x0: y * st + ir.x1]
        px = sorted(px)
        return px[int(0.98 * (len(px) - 1))] - px[int(0.02 * (len(px) - 1))] >= INK_MIN


def _page_findings(page, extra_hidden=()):
    """Return (hidden runs, visible probe hits) for one page."""
    spans = list(_spans(page))
    rect = page.rect
    ink = _Ink(page)
    reasons = [_attr_reasons(s, rect) for s in spans]
    for s, r in zip(spans, reasons):
        if _lum(s["color"]) >= LIGHT and s["char_flags"] & FILLED and (r or not ink.visible(s["bbox"])):
            r.add("light")          # white / near-background text with no visible ink

    # Lexicon matches in text that looks visible: render-check those spans (covered or clipped text).
    parts, owners, pos = [], [], 0
    for k, s in enumerate(spans):
        t = norm(s["text"])
        parts.append(t)
        owners.append((pos, pos + len(t), k))
        pos += len(t) + 1
    page_text = " ".join(parts)
    probe = set()
    for i, _, rx in LEXICON:
        if i not in PROBE:
            continue
        for m in rx.finditer(page_text):
            probe.add(i)
            for a, b, k in owners:
                if a < m.end() and b > m.start() and not reasons[k] and not ink.visible(spans[k]["bbox"]):
                    reasons[k].add("covered")

    runs, last = [], -2
    for k, (s, r) in enumerate(zip(spans, reasons)):
        if not r:
            continue
        if k == last + 1:
            runs[-1]["reasons"] |= r
            runs[-1]["text"] += " " + s["text"]
        else:
            runs.append({"page": page.number + 1, "reasons": set(r), "text": s["text"]})
        last = k
    for text in extra_hidden:
        runs.append({"page": page.number + 1, "reasons": {"layer"}, "text": text})
    return runs, probe


def _layer_hidden(doc):
    """Text that only appears when every optional-content layer is switched on: {page_no: [text]}."""
    ocgs = doc.get_ocgs()
    if not ocgs or all(o["on"] for o in ocgs.values()):
        return {}
    before = {p.number: {norm(s["text"]) for s in _spans(p)} for p in doc}
    doc.set_layer(-1, on=list(ocgs), off=[], basestate="ON")
    extra = {}
    with pymupdf.open(stream=doc.tobytes(), filetype="pdf") as all_on:
        for p in all_on:
            new = [s["text"] for s in _spans(p) if norm(s["text"]) not in before[p.number]]
            if new:
                extra[p.number] = new
    return extra


def strip(source) -> bytes:
    """Return a copy of the PDF with hidden text spans redacted (white/invisible, tiny, transparent, off-page).

    A defence for AI-assisted review: what is left is what a human reader can see."""
    doc = pymupdf.open(stream=source, filetype="pdf") if isinstance(source, (bytes, bytearray)) else pymupdf.open(source)
    with doc:
        for page in doc:
            rect, ink, hit = page.rect, _Ink(page), False
            for s in _spans(page):
                r = _attr_reasons(s, rect)
                if _lum(s["color"]) >= LIGHT and s["char_flags"] & FILLED and (r or not ink.visible(s["bbox"])):
                    r.add("light")
                if r:
                    page.add_redact_annot(s["bbox"])
                    hit = True
            if hit:
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        return doc.tobytes(garbage=3, deflate=True)


def scan(source) -> dict:
    """Scan a PDF given as a path or bytes. Returns a JSON-serialisable report."""
    out = {"version": __version__, "status": "ok"}
    try:
        doc = pymupdf.open(stream=source, filetype="pdf") if isinstance(source, (bytes, bytearray)) \
            else pymupdf.open(source)
    except Exception as e:
        return {**out, "status": "unreadable", "error": str(e)[:200]}
    with doc:
        if doc.needs_pass:
            return {**out, "status": "encrypted"}
        out["pages"] = doc.page_count
        runs, probe, errors = [], set(), 0
        try:
            layered = _layer_hidden(doc)
        except Exception:
            layered, errors = {}, errors + 1
        for page in doc:
            try:
                r, p = _page_findings(page, layered.get(page.number, ()))
            except Exception:
                errors += 1
                continue
            runs += r
            probe |= p
        meta = norm(" ".join(v for v in (doc.metadata or {}).values() if isinstance(v, str)))
    hidden_text = norm(" ".join(r["text"] for r in runs))
    hits = lexicon_hits(hidden_text)
    reasons = {}
    for r in runs:
        n = sum(not c.isspace() for c in r["text"])
        for why in r["reasons"]:
            reasons[why] = reasons.get(why, 0) + n
    hidden_chars = sum(not c.isspace() for c in hidden_text)
    strong = [h for h in hits if h not in WEAK]
    asks = [h for h in hits if INTENT[h] != "address"]   # merely addressing an AI is not a prompt (e.g. quoted jailbreaks)
    intents = {INTENT[h] for h in hits}
    out.update(
        hidden_chars=hidden_chars,
        reasons=reasons,
        hits=hits,
        visible_hits=sorted(probe),
        meta_hits=lexicon_hits(meta, PROBE),
        page_errors=errors,
        verdict="prompt" if strong and asks else "suspect" if hits
        else "hidden_text" if hidden_chars >= MIN_CHARS else "clean",
        intent=next((x for x in ("manipulate", "canary", "deter", "override", "address") if x in intents), None),
        runs=[{"page": r["page"], "reasons": sorted(r["reasons"]), "text": r["text"][:2000]} for r in runs],
    )
    return out


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if argv[:1] == ["--strip"]:
        with open(argv[2], "wb") as f:
            f.write(strip(argv[1]))
        print(f"{argv[2]}: written without hidden text ({scan(argv[2]).get('verdict')})")
        return
    as_json = "--json" in argv
    for path in (a for a in argv if a != "--json"):
        rep = scan(path)
        if as_json:
            print(json.dumps({"file": path, **rep}, ensure_ascii=False))
            continue
        print(f"{path}: {rep.get('verdict', rep['status'])}"
              + (f" ({rep['intent']})" if rep.get("intent") else "")
              + (f", {rep['hidden_chars']} hidden chars {rep['reasons']}" if rep.get("hidden_chars") else ""))
        for r in rep.get("runs", [])[:5]:
            print(f"  p{r['page']} [{','.join(r['reasons'])}] {r['text'][:160]!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
