"""Render the README charts as SVG from `data.json`. Standard library only.

    python docs/charts/make_charts.py

Every number comes from `data.json`, which cites its sources (the decision log
and the generated training data). The charts are hand-drawn SVG so the repo
needs no plotting dependency and the output is byte-stable.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = json.loads((HERE / "data.json").read_text(encoding="utf-8"))

# Palette: validated categorical slots (blue, orange, aqua) on a light surface.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e6e4df"
FONT = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"


def svg(width: int, height: int, body: list[str], title: str, desc: str) -> str:
    head = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="t d" '
        f'font-family="{FONT}" font-size="13">'
        f"<title id=\"t\">{title}</title><desc id=\"d\">{desc}</desc>"
        f'<rect width="{width}" height="{height}" fill="{SURFACE}" rx="8"/>'
    )
    return head + "".join(body) + "</svg>\n"


def text(x: float, y: float, s: str, *, size=13, fill=INK, anchor="start", weight="normal") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
        f'text-anchor="{anchor}" font-weight="{weight}">{s}</text>'
    )


def heading(title: str, subtitle: str, width: int) -> list[str]:
    return [text(20, 28, title, size=16, weight="600"), text(20, 48, subtitle, fill=INK2)]


# --- 1. Materiality across checkpoints ---------------------------------------


def materiality_chart() -> str:
    pts = DATA["materiality_by_checkpoint"]
    width, height = 720, 340
    left, right, top, bottom = 60, 560, 84, 262
    body = heading(
        "Materiality judgment across evaluation checkpoints",
        "Agreement with labelled real events. Five hand-built personas, about 35 events per run.",
        width,
    )
    ymin, ymax = 0.5, 1.0
    def sx(i): return left + i * (right - left) / (len(pts) - 1)
    def sy(v): return bottom - (v - ymin) / (ymax - ymin) * (bottom - top)
    for v in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        body.append(f'<line x1="{left}" x2="{right}" y1="{sy(v):.1f}" y2="{sy(v):.1f}" stroke="{GRID}"/>')
        body.append(text(left - 8, sy(v) + 4, f"{v:.1f}", fill=INK2, anchor="end", size=11))
    series = [("Precision", "precision"), ("Recall", "recall"), ("F1", "f1")]
    for k, (_, key) in enumerate(series):
        color = SERIES[k]
        d = " ".join(f"{'M' if i == 0 else 'L'}{sx(i):.1f},{sy(p[key]):.1f}" for i, p in enumerate(pts))
        body.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"/>')
        for i, p in enumerate(pts):
            body.append(f'<circle cx="{sx(i):.1f}" cy="{sy(p[key]):.1f}" r="4.5" fill="{color}" stroke="{SURFACE}" stroke-width="2"/>')
    # Direct labels at the right edge, spread so they cannot collide.
    ends = sorted(((pts[-1][key], name) for name, key in series), reverse=True)
    y_prev = None
    for value, name in ends:
        y = sy(value) + 4
        if y_prev is not None and y - y_prev < 15:
            y = y_prev + 15
        body.append(text(right + 12, y, f"{name} {value:.2f}", fill=INK, size=12))
        y_prev = y
    for i, p in enumerate(pts):
        body.append(text(sx(i), bottom + 20, f"{p['checkpoint']} · {p['date'][5:]}", fill=INK, anchor="middle", size=12, weight="600"))
        for j, line in enumerate(p["label"]):
            body.append(text(sx(i), bottom + 36 + 14 * j, line, fill=INK2, anchor="middle", size=11))
    lx = 420
    for k, (name, _) in enumerate(series):
        body.append(f'<rect x="{lx}" y="{top - 24}" width="14" height="3" rx="1.5" fill="{SERIES[k]}"/>')
        body.append(text(lx + 20, top - 19, name, fill=INK2, size=12))
        lx += 86
    return svg(width, height, body, "Materiality judgment across evaluation checkpoints",
               "Precision stays at 0.97; recall rises from 0.83 at checkpoint 1 to 0.94 at checkpoints 2 and 5; F1 from 0.89 to 0.96.")


# --- 2. Ledger quality before and after checkpoint 2 --------------------------


def ledger_before_after_chart() -> str:
    rows = DATA["ledger_before_after_cp2"]
    width, height = 720, 300
    left, right, top, bottom = 250, 620, 80, 260
    body = heading(
        "Knowledge-ledger errors before and after checkpoint 2",
        "Term canonicalisation and concept_evidence v3, same five personas, same events (2026-09-08).",
        width,
    )
    vmax = max(max(r["before"], r["after"]) for r in rows)
    def sx(v): return left + v / vmax * (right - left)
    gap = (bottom - top) / len(rows)
    for i, r in enumerate(rows):
        y = top + gap * i + 8
        body.append(text(left - 12, y + 18, r["measure"], fill=INK, anchor="end", size=12))
        # before: muted bar; after: series-1 bar. 2px surface gap between them.
        body.append(f'<rect x="{left}" y="{y}" width="{sx(r["before"]) - left:.1f}" height="12" rx="3" fill="#b9b6ad"/>')
        body.append(f'<rect x="{left}" y="{y + 14}" width="{max(sx(r["after"]) - left, 2):.1f}" height="12" rx="3" fill="{SERIES[0]}"/>')
        body.append(text(sx(r["before"]) + 8, y + 10, f"{r['before']}", fill=INK2, size=11))
        body.append(text(max(sx(r["after"]), left) + 8, y + 24, f"{r['after']}", fill=INK, size=11, weight="600"))
    body.append(f'<rect x="{left}" y="{bottom + 14}" width="14" height="12" rx="3" fill="#b9b6ad"/>')
    body.append(text(left + 20, bottom + 24, "before", fill=INK2, size=12))
    body.append(f'<rect x="{left + 80}" y="{bottom + 14}" width="14" height="12" rx="3" fill="{SERIES[0]}"/>')
    body.append(text(left + 100, bottom + 24, "after", fill=INK2, size=12))
    return svg(width, height, body, "Knowledge-ledger errors before and after checkpoint 2",
               "Spurious understood credits fell from 42 to 22, ledger false positives from 35 to 17, and false claims about the silent persona from 2 to 0.")


# --- 3. Ledger precision by state --------------------------------------------


def ledger_precision_chart() -> str:
    rows = DATA["ledger_precision_by_state_cp5"]
    width, height = 720, 360
    left, right, top, bottom = 200, 600, 80, 320
    body = heading(
        "How often each ledger state is right (checkpoint 5)",
        "Terms the system believed were in a state, checked against what the persona really knew.",
        width,
    )
    gap = (bottom - top) / len(rows)
    for v in (0, 0.25, 0.5, 0.75, 1.0):
        x = left + v * (right - left)
        body.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top - 6}" y2="{bottom}" stroke="{GRID}"/>')
        body.append(text(x, top - 10, f"{int(v * 100)}%", fill=INK2, anchor="middle", size=11))
    for i, r in enumerate(rows):
        p = r["correct"] / r["n"]
        y = top + gap * i + gap / 2 - 7
        body.append(text(left - 12, y + 11, r["state"], fill=INK, anchor="end", size=12))
        w = max(p * (right - left), 2)
        body.append(f'<rect x="{left}" y="{y}" width="{w:.1f}" height="14" rx="3" fill="{SERIES[0]}"/>')
        body.append(text(left + w + 8, y + 11, f"{r['correct']}/{r['n']}", fill=INK2, size=11))
    body.append(text(20, bottom + 24, "'unknown' is almost always right. A term glossed once in a skimmed briefing is usually still unknown,", fill=INK2, size=11))
    body.append(text(20, bottom + 38, "which is why that state stays out of the proficiency band until a second read.", fill=INK2, size=11))
    return svg(width, height, body, "Ledger precision by state at checkpoint 5",
               "unknown 22 of 23, confirmed 6 of 8, explained after two reads 2 of 3, familiar after two reads 2 of 3, explained after one read 2 of 8, provisional 2 of 9, familiar after one read 2 of 32.")


# --- 4. Briefing depth by reader profile --------------------------------------


def depth_chart() -> str:
    prof = DATA["briefing_depth_by_reader_profile_v7"]
    labels = {
        "beginner": "Beginner",
        "partial": "Partway in",
        "expert_one_subdomain": "Expert in one area",
        "expert": "Expert",
        "returning_reader": "Returning reader",
    }
    width, height = 720, 330
    body = heading(
        "The same event, briefed for five kinds of reader (prompt v7, teacher data)",
        f"Mean over {sum(v['n'] for v in prof.values())} generated briefings across 56 real events, grouped by the reader state the packet described.",
        width,
    )
    panels = [("Reading grade level", "fk_grade_mean", "{:.1f}", SERIES[0]),
              ("Definitions per 100 words", "definitions_per_100w_mean", "{:.2f}", SERIES[1])]
    pw = 300
    for pi, (title, key, fmt, color) in enumerate(panels):
        px = 40 + pi * (pw + 60)
        top, bottom = 100, 270
        body.append(text(px, top - 14, title, fill=INK, size=12, weight="600"))
        vals = [prof[k][key] for k in labels]
        vmax = max(vals) * 1.15
        bw = pw / len(labels) - 10
        for i, k in enumerate(labels):
            v = prof[k][key]
            h = v / vmax * (bottom - top)
            x = px + i * (pw / len(labels))
            body.append(f'<rect x="{x:.1f}" y="{bottom - h:.1f}" width="{bw:.1f}" height="{h:.1f}" rx="3" fill="{color}"/>')
            body.append(text(x + bw / 2, bottom - h - 6, fmt.format(v), fill=INK, anchor="middle", size=11))
            words = labels[k].split(" ")
            body.append(text(x + bw / 2, bottom + 16, " ".join(words[:2]), fill=INK2, anchor="middle", size=10))
            if len(words) > 2:
                body.append(text(x + bw / 2, bottom + 28, " ".join(words[2:]), fill=INK2, anchor="middle", size=10))
        body.append(f'<line x1="{px}" x2="{px + pw}" y1="{bottom}" y2="{bottom}" stroke="{GRID}"/>')
    body.append(text(20, 312, "Beginners get the simplest prose and the most definitions. Experts get denser prose and almost none.", fill=INK2, size=11))
    return svg(width, height, body, "Briefing depth by reader profile",
               "Reading grade level rises from about 10 for beginners to about 12.6 for experts, while definitions per 100 words fall from about 1.5 to about 0.45.")


def main() -> None:
    out = {
        "materiality-by-checkpoint.svg": materiality_chart(),
        "ledger-before-after.svg": ledger_before_after_chart(),
        "ledger-precision-by-state.svg": ledger_precision_chart(),
        "briefing-depth-by-profile.svg": depth_chart(),
    }
    for name, content in out.items():
        (HERE / name).write_text(content, encoding="utf-8")
        print("wrote", name)


if __name__ == "__main__":
    main()
