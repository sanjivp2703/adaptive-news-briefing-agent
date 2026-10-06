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


def main() -> None:
    out = {
        "ledger-before-after.svg": ledger_before_after_chart(),
        "ledger-precision-by-state.svg": ledger_precision_chart(),
    }
    for name, content in out.items():
        (HERE / name).write_text(content, encoding="utf-8")
        print("wrote", name)


if __name__ == "__main__":
    main()
