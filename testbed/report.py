"""Summarise runs.jsonl: rates with 95% Wilson intervals, key contrasts, tokens and cost.

The pilot is too small for firm conclusions; this report is mainly for
checking the method, the scoring and the cost per run.
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path

CONTRASTS = [
    ("A", "B", "claimed authority alone"),
    ("A", "C", "verified origin alone"),
    ("B", "D", "verified origin on top of claimed authority"),
    ("A", "E", "sender's own verification claim"),
    ("C", "E", "application field vs sender's claim"),
]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from discordant pair counts."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * p)


def load_runs(path: Path) -> list[dict]:
    """Latest record per run_id (a resumed run may have earlier failed attempts)."""
    latest: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            prev = latest.get(rec["run_id"])
            if prev is None or prev.get("status") != "ok":
                latest[rec["run_id"]] = rec
    return list(latest.values())


def _fmt_rate(k: int, n: int) -> str:
    if n == 0:
        return "n/a"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {k / n:.0%} [{lo:.0%}, {hi:.0%}]"


def summarise(runs_path: Path, prices: dict[str, dict] | None = None) -> tuple[str, list[dict]]:
    runs = load_runs(runs_path)
    ok = [r for r in runs if r.get("status") == "ok"]
    other = defaultdict(int)
    for r in runs:
        if r.get("status") != "ok":
            other[r.get("status")] += 1

    lines: list[str] = [f"# Report: {runs_path.parent.name}", ""]
    lines.append(f"Runs: {len(ok)} ok" + "".join(f", {n} {s}" for s, n in sorted(other.items())))
    lines.append("")
    lines.append("Rates are k/n with a 95% Wilson interval. *Proposed* = the model requested the attack's tool call; "
                 "*executed* = it happened in the final state; *legit* = the user's task was done without leaking a secret.")
    lines.append("")

    rows: list[dict] = []
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in ok:
        groups[(r["model_label"], r["kind"], r["defense"], r["condition"])].append(r)

    for model in sorted({k[0] for k in groups}):
        for kind in ("attack", "benign"):
            for defense in sorted({k[2] for k in groups if k[0] == model and k[1] == kind}):
                lines.append(f"## {model}: {kind} runs, defense {defense}")
                lines.append("")
                lines.append("| Cond | n | Helper asked | Proposed unauthorized | Executed unauthorized | Legit success | Policy violation (proposed) |")
                lines.append("|---|---|---|---|---|---|---|")
                for cond in "ABCDE":
                    rs = groups.get((model, kind, defense, cond), [])
                    if not rs:
                        continue
                    n = len(rs)
                    cnt = lambda key: sum(1 for r in rs if r["scores"][key])  # noqa: E731
                    row = {
                        "model": model, "kind": kind, "defense": defense, "condition": cond, "n": n,
                        "exposed": cnt("attack_exposed"),
                        "proposed": cnt("proposed_unauthorized"),
                        "executed": cnt("executed_unauthorized"),
                        "legit": cnt("legit_success"),
                        "policy_violation": cnt("proposed_policy_violation"),
                    }
                    rows.append(row)
                    lines.append(
                        f"| {cond} | {n} | {row['exposed']}/{n} | {_fmt_rate(row['proposed'], n)} | "
                        f"{_fmt_rate(row['executed'], n)} | {_fmt_rate(row['legit'], n)} | {_fmt_rate(row['policy_violation'], n)} |"
                    )
                lines.append("")
                if kind == "attack":
                    lines.extend(_contrasts(groups, model, defense))

    lines.extend(_usage(ok, prices or {}))
    return "\n".join(lines) + "\n", rows


def _contrasts(groups: dict, model: str, defense: str) -> list[str]:
    out = ["Paired contrasts on *proposed unauthorized* (pairs = same scenario and repetition):", "",
           "| Contrast | What it isolates | Pairs | Rate 1 → Rate 2 | Only 1 / only 2 | Exact McNemar p |",
           "|---|---|---|---|---|---|"]
    for c1, c2, what in CONTRASTS:
        g1 = {(r["scenario"], r["rep"]): r for r in groups.get((model, "attack", defense, c1), [])}
        g2 = {(r["scenario"], r["rep"]): r for r in groups.get((model, "attack", defense, c2), [])}
        keys = sorted(set(g1) & set(g2))
        if not keys:
            continue
        x1 = [g1[k]["scores"]["proposed_unauthorized"] for k in keys]
        x2 = [g2[k]["scores"]["proposed_unauthorized"] for k in keys]
        b = sum(1 for a, c in zip(x1, x2) if a and not c)
        c = sum(1 for a, cc in zip(x1, x2) if cc and not a)
        n = len(keys)
        out.append(
            f"| {c1} vs {c2} | {what} | {n} | {sum(x1) / n:.0%} → {sum(x2) / n:.0%} | {b} / {c} | {mcnemar_exact(b, c):.3f} |"
        )
    out.append("")
    return out


def _usage(ok: list[dict], prices: dict[str, dict]) -> list[str]:
    lines = ["## Tokens and cost", "", "| Model | Runs | Input tokens | Output tokens | Mean in/run | Mean out/run | Cost (USD) | Model versions seen |",
             "|---|---|---|---|---|---|---|---|"]
    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        by_model[r["model_label"]].append(r)
    for model, rs in sorted(by_model.items()):
        tin = sum(r["usage"]["input_tokens"] for r in rs)
        tout = sum(r["usage"]["output_tokens"] for r in rs)
        price = prices.get(model)
        cost = "set price_per_mtok"
        if price and price.get("input") is not None and price.get("output") is not None:
            cost = f"{tin / 1e6 * price['input'] + tout / 1e6 * price['output']:.2f}"
        versions = sorted({v for r in rs for v in r.get("model_versions", [])})
        lines.append(
            f"| {model} | {len(rs)} | {tin:,} | {tout:,} | {tin // len(rs):,} | {tout // len(rs):,} | {cost} | {', '.join(versions)} |"
        )
    lines.append("")
    lines.append("Cost uses the prices in the config (`price_per_mtok`); check them against each provider's current price list.")
    return lines


def write_report(runs_path: Path, prices: dict[str, dict] | None = None) -> tuple[Path, Path]:
    text, rows = summarise(runs_path, prices)
    md = runs_path.parent / "report.md"
    md.write_text(text, encoding="utf-8")
    csv_path = runs_path.parent / "summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        fields = ["model", "kind", "defense", "condition", "n", "exposed", "proposed", "executed", "legit", "policy_violation"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return md, csv_path
