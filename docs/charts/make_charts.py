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


# --- 4. Stage 1 training curve -------------------------------------------------


def training_curve_chart() -> str:
    rows = json.loads((HERE / "train_log.json").read_text(encoding="utf-8"))
    train = [(r["step"], r["loss"]) for r in rows if "loss" in r]
    val = [(r["step"], r["eval_loss"]) for r in rows if "eval_loss" in r]
    max_step = max(s for s, _ in train)
    epochs = 3
    width, height = 720, 340
    left, right, top, bottom = 60, 580, 84, 262
    body = heading(
        "Stage 1 fine-tuning: loss per step",
        "Qwen2.5-3B-Instruct, LoRA r=16, 415 examples, 3 epochs on a Colab L4. Lower is better.",
        width,
    )
    ymin, ymax = 0.0, 1.6
    def sx(step): return left + step / max_step * (right - left)
    def sy(v): return bottom - (v - ymin) / (ymax - ymin) * (bottom - top)
    for v in (0.0, 0.4, 0.8, 1.2, 1.6):
        body.append(f'<line x1="{left}" x2="{right}" y1="{sy(v):.1f}" y2="{sy(v):.1f}" stroke="{GRID}"/>')
        body.append(text(left - 8, sy(v) + 4, f"{v:.1f}", fill=INK2, anchor="end", size=11))
    for e in range(1, epochs):
        x = sx(max_step * e / epochs)
        body.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{bottom}" stroke="{GRID}" stroke-dasharray="4 4"/>')
        body.append(text(x, top - 6, f"epoch {e} ends", fill=INK2, anchor="middle", size=10))
    d = " ".join(f"{'M' if i == 0 else 'L'}{sx(s):.1f},{sy(v):.1f}" for i, (s, v) in enumerate(train))
    body.append(f'<path d="{d}" fill="none" stroke="{SERIES[0]}" stroke-width="2" stroke-linejoin="round"/>')
    d = " ".join(f"{'M' if i == 0 else 'L'}{sx(s):.1f},{sy(v):.1f}" for i, (s, v) in enumerate(val))
    body.append(f'<path d="{d}" fill="none" stroke="{SERIES[1]}" stroke-width="2" stroke-linejoin="round"/>')
    for s, v in val:
        body.append(f'<circle cx="{sx(s):.1f}" cy="{sy(v):.1f}" r="4.5" fill="{SERIES[1]}" stroke="{SURFACE}" stroke-width="2"/>')
    body.append(text(right + 10, sy(train[-1][1]) + 4, f"train {train[-1][1]:.2f}", fill=INK, size=12))
    body.append(text(right + 10, sy(val[-1][1]) - 6, f"validation {val[-1][1]:.2f}", fill=INK, size=12))
    for step in (0, 50, 100, 150):
        body.append(text(sx(step), bottom + 18, str(step), fill=INK2, anchor="middle", size=11))
    body.append(text((left + right) / 2, bottom + 36, "training step (8 examples each)", fill=INK2, anchor="middle", size=11))
    lx = 400
    for k, name in enumerate(("training loss", "validation loss")):
        body.append(f'<rect x="{lx}" y="{top - 26}" width="14" height="3" rx="1.5" fill="{SERIES[k]}"/>')
        body.append(text(lx + 20, top - 21, name, fill=INK2, size=12))
        lx += 120
    best = min(val, key=lambda p: p[1])
    body.append(text(20, 312, f"Training loss {train[0][1]:.2f} to {train[-1][1]:.2f}; validation loss {val[0][1]:.2f} to {val[-1][1]:.2f}, lowest {best[1]:.2f} at step {best[0]}. It flattens rather than rising: no sign of memorising.", fill=INK2, size=11))
    return svg(width, height, body, "Stage 1 fine-tuning loss per step",
               f"Training loss falls from {train[0][1]:.2f} to {train[-1][1]:.2f}; validation loss from {val[0][1]:.2f} to {val[-1][1]:.2f} and flattens after step 100.")


# --- 5. Stage 1: hard rules, before and after -----------------------------------


def stage1_rules_chart() -> str:
    d = DATA["stage1_hard_rules"]
    n = d["n"]
    palette = {"untuned base": "#b9b6ad", "fine-tuned student": SERIES[0],
               "stage 2 (DPO, first pass)": "#8fd3b4", "stage 2 (DPO, second pass)": SERIES[2],
               "teacher (Claude Sonnet)": SERIES[1]}
    models = [(name, palette[name]) for name in d.get("_order", list(palette)) if name in d]
    rules = d["rules"]
    width, height = 720, 530
    left, right, top, bottom = 230, 640, 84, 440
    body = heading(
        "Hard rules passed on the 45 held-out packets, across training stages",
        "Same 45 packets, same checks the training data was filtered with, one bar per model.",
        width,
    )
    for v in (0, 15, 30, 45):
        x = left + v / n * (right - left)
        body.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top - 6}" y2="{bottom}" stroke="{GRID}"/>')
        body.append(text(x, top - 10, f"{v}/{n}", fill=INK2, anchor="middle", size=11))
    gap = (bottom - top) / len(rules)
    for i, (label, key) in enumerate(rules):
        y0 = top + gap * i + 4
        body.append(text(left - 12, y0 + 15, label, fill=INK, anchor="end", size=12, weight="600" if key == "all" else "normal"))
        for k, (name, color) in enumerate(models):
            v = d[name][key]
            w = max(v / n * (right - left), 2)
            y = y0 + k * 9
            body.append(f'<rect x="{left}" y="{y:.1f}" width="{w:.1f}" height="7" rx="2" fill="{color}"/>')
            if key == "all" or name != "teacher (Claude Sonnet)":
                body.append(text(left + w + 6, y + 7, str(v), fill=INK2, size=10))
    lx, ly = 20, bottom + 14
    for name, color in models:
        if lx + 26 + 6.2 * len(name) > 700:
            lx, ly = 20, ly + 16
        body.append(f'<rect x="{lx}" y="{ly}" width="12" height="7" rx="2" fill="{color}"/>')
        body.append(text(lx + 18, ly + 7, name, fill=INK2, size=11))
        lx += 26 + 6.2 * len(name)
    parts = [f"{d[name]['all']} {name.replace('stage 2 (DPO, ', 'DPO ').rstrip(')').replace('teacher (Claude Sonnet)', 'teacher')}" for name, _ in models]
    body.append(text(20, ly + 30, f"All rules at once, out of {n}: " + "; ".join(parts) + ".", fill=INK2, size=11))
    return svg(width, height, body, "Hard rules passed on the held-out packets, across training stages",
               "On 45 held-out packets: " + "; ".join(parts) + ".")


def stage1_definitions_chart() -> str:
    d = DATA["stage1_definitions_by_profile"]
    labels = {"beginner": "Beginner", "partial": "Partway in", "expert_one_subdomain": "Expert in one area", "expert": "Expert", "returning_reader": "Returning reader"}
    palette = {"untuned base": "#b9b6ad", "fine-tuned student": SERIES[0], "stage 2 (DPO, first pass)": "#8fd3b4",
               "stage 2 (DPO, second pass)": SERIES[2], "teacher": SERIES[1]}
    present = {m for p in d.values() for m in p if m != "n"}
    models = [(m, c) for m, c in palette.items() if m in present]
    width, height = 720, 340
    left, right, top, bottom = 60, 690, 90, 262
    body = heading(
        "Definitions per 100 words, by reader profile, across training stages",
        "How often each model stops to define a term. The teacher defines most for beginners and least for experts.",
        width,
    )
    vmax = max(d[p][m] for p in d for m, _ in models) * 1.1
    def sy(v): return bottom - v / vmax * (bottom - top)
    for v in (0, 2, 4):
        if v <= vmax:
            body.append(f'<line x1="{left}" x2="{right}" y1="{sy(v):.1f}" y2="{sy(v):.1f}" stroke="{GRID}"/>')
            body.append(text(left - 8, sy(v) + 4, str(v), fill=INK2, anchor="end", size=11))
    group_w = (right - left) / len(labels)
    bar_w = (group_w - 24) / len(models)
    for i, (p, label) in enumerate(labels.items()):
        gx = left + i * group_w + 12
        for k, (m, color) in enumerate(models):
            v = d[p][m]
            x = gx + k * bar_w
            h = v / vmax * (bottom - top)
            body.append(f'<rect x="{x:.1f}" y="{bottom - h:.1f}" width="{bar_w - 2:.1f}" height="{h:.1f}" rx="2" fill="{color}"/>')
            body.append(text(x + (bar_w - 2) / 2, bottom - h - 4, f"{v:.1f}", fill=INK2, anchor="middle", size=9))
        words = label.split(" ")
        body.append(text(gx + (group_w - 24) / 2, bottom + 16, " ".join(words[:2]), fill=INK2, anchor="middle", size=10))
        if len(words) > 2:
            body.append(text(gx + (group_w - 24) / 2, bottom + 28, " ".join(words[2:]), fill=INK2, anchor="middle", size=10))
    lx = 60
    for m, color in models:
        body.append(f'<rect x="{lx}" y="{top - 26}" width="12" height="7" rx="2" fill="{color}"/>')
        body.append(text(lx + 18, top - 19, m, fill=INK2, size=10))
        lx += 24 + 5.6 * len(m)
    body.append(text(20, 312, "Untuned: too many. Stage 1: too few. First DPO pass: none. Second pass, on teacher-anchored pairs: back near the teacher for beginners, still low for experts.", fill=INK2, size=10))
    return svg(width, height, body, "Definitions per 100 words by reader profile, three models",
               "The untuned base defines far more than the teacher for every profile; the fine-tuned student defines less than the teacher for every profile.")


def main() -> None:
    out = {
        "ledger-before-after.svg": ledger_before_after_chart(),
        "ledger-precision-by-state.svg": ledger_precision_chart(),
        "training-curve.svg": training_curve_chart(),
        "stage1-hard-rules.svg": stage1_rules_chart(),
        "stage1-definitions.svg": stage1_definitions_chart(),
    }
    for name, content in out.items():
        (HERE / name).write_text(content, encoding="utf-8")
        print("wrote", name)


if __name__ == "__main__":
    main()
