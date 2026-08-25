#!/usr/bin/env python3
"""
Eval harness for the Slack Daily Brief agent.

Runs the agent's real summarize() function (imported straight from agent.py,
not reimplemented) against a fixed set of synthetic transcripts, grades each
output with an LLM judge against a rubric, and reports recall (expected facts
found), precision (hallucinations), and category-placement errors.

Usage:
  python eval/run_eval.py                          # run against default model
  python eval/run_eval.py --model claude-opus-5     # test a different model
  python eval/run_eval.py --save                    # write a timestamped report to eval/results/
  python eval/run_eval.py --compare eval/results/run_2026-08-20T10-00-00.json
                                                     # diff this run against a prior saved report

Exit code is 1 if the pass rate falls below --threshold (default: 1.0, i.e.
every fixture must pass) — wire this into CI to catch prompt regressions.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # so `import agent` works
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.table import Table

load_dotenv(Path(__file__).parent.parent / ".env")

from agent import summarize  # noqa: E402  (needs sys.path + dotenv set up first)
from fixtures import FIXTURES  # noqa: E402
from grader import grade_summary  # noqa: E402

console = Console()
RESULTS_DIR = Path(__file__).parent / "results"


def _normalize_grade(grade: dict, fixture_id: str) -> dict:
    """
    Defensively coerce the judge's structured output into the shape we expect.
    A forced tool_choice call gets us valid JSON, but not a guarantee that
    every nested item matches the schema's object shape — the model can still
    hand back a bare string in a list that was supposed to hold {fact, found,
    evidence} objects. Trusting that blindly crashes aggregation on a type
    error with no indication which fixture caused it, so normalize + warn
    instead of blowing up the whole run.
    """
    def clean_list(items, required_key, fallback_key):
        out = []
        for it in items:
            if isinstance(it, dict) and required_key in it:
                out.append(it)
            else:
                console.print(
                    f"  [yellow]warning:[/] {fixture_id} — grader returned a malformed "
                    f"entry ({it!r}), treating as unresolved"
                )
                out.append({fallback_key: str(it), "found": False, "evidence": "malformed grader output",
                            "claim": str(it), "why_unsupported": "malformed grader output"})
        return out

    grade = dict(grade)
    grade["matched_expected"] = clean_list(grade.get("matched_expected") or [], "fact", "fact")
    grade["hallucinations"] = clean_list(grade.get("hallucinations") or [], "claim", "claim")
    grade["category_errors"] = [str(x) for x in (grade.get("category_errors") or [])]
    grade.setdefault("verdict", "fail")
    grade.setdefault("reasoning", "(missing from grader output)")
    return grade


def run(model: str, judge_model: str) -> dict:
    date_str = "Eval Run"
    fixture_results = []

    for fx in FIXTURES:
        console.print(f"  [dim]running[/] {fx['id']} ...", end="\r")
        summary = summarize(fx["transcript"], model, focus=[], date_str=date_str)
        grade = _normalize_grade(grade_summary(fx, summary, judge_model=judge_model), fx["id"])
        fixture_results.append({
            "id": fx["id"],
            "notes": fx.get("notes", ""),
            "summary": summary,
            "grade": grade,
        })
        console.print(f"  [{'green' if grade['verdict'] == 'pass' else 'red'}]{grade['verdict'].upper():4}[/]  {fx['id']}")

    total_facts = sum(len(r["grade"]["matched_expected"]) for r in fixture_results)
    found_facts = sum(
        sum(1 for f in r["grade"]["matched_expected"] if f["found"]) for r in fixture_results
    )
    total_hallucinations = sum(len(r["grade"]["hallucinations"]) for r in fixture_results)
    total_category_errors = sum(len(r["grade"]["category_errors"]) for r in fixture_results)
    passed = sum(1 for r in fixture_results if r["grade"]["verdict"] == "pass")

    return {
        "model": model,
        "judge_model": judge_model,
        "fixture_results": fixture_results,
        "summary_stats": {
            "pass_rate": passed / len(fixture_results),
            "fixtures_passed": passed,
            "fixtures_total": len(fixture_results),
            "recall": found_facts / total_facts if total_facts else 1.0,
            "hallucinations": total_hallucinations,
            "category_errors": total_category_errors,
        },
    }


def print_report(report: dict):
    stats = report["summary_stats"]
    console.print()
    table = Table(box=box.ROUNDED, show_header=True, header_style="bold dim")
    table.add_column("Fixture")
    table.add_column("Verdict", justify="center")
    table.add_column("Facts found", justify="right")
    table.add_column("Hallucinations", justify="right")
    table.add_column("Category errors", justify="right")

    for r in report["fixture_results"]:
        g = r["grade"]
        found = sum(1 for f in g["matched_expected"] if f["found"])
        total = len(g["matched_expected"])
        verdict_style = "green" if g["verdict"] == "pass" else "bold red"
        table.add_row(
            r["id"],
            f"[{verdict_style}]{g['verdict']}[/]",
            f"{found}/{total}",
            str(len(g["hallucinations"])),
            str(len(g["category_errors"])),
        )

    console.print(table)
    console.print()
    console.print(
        f"[bold]Pass rate:[/] {stats['fixtures_passed']}/{stats['fixtures_total']}"
        f"  ·  [bold]Recall:[/] {stats['recall']:.0%}"
        f"  ·  [bold]Hallucinations:[/] {stats['hallucinations']}"
        f"  ·  [bold]Category errors:[/] {stats['category_errors']}"
    )

    for r in report["fixture_results"]:
        g = r["grade"]
        if g["verdict"] == "fail":
            console.print(f"\n[bold red]✗ {r['id']}[/] — {g['reasoning']}")
            for f in g["matched_expected"]:
                if not f["found"]:
                    console.print(f"    [yellow]missing:[/] {f['fact']}")
            for h in g["hallucinations"]:
                console.print(f"    [red]hallucinated:[/] {h['claim']} — {h['why_unsupported']}")
            for c in g["category_errors"]:
                console.print(f"    [magenta]miscategorized:[/] {c}")


def print_diff(current: dict, prior: dict):
    console.print()
    console.print("[bold]Diff vs. prior run:[/]")
    prior_by_id = {r["id"]: r["grade"]["verdict"] for r in prior["fixture_results"]}
    for r in current["fixture_results"]:
        prev = prior_by_id.get(r["id"])
        now = r["grade"]["verdict"]
        if prev is None:
            console.print(f"  [dim]new fixture[/] {r['id']}: {now}")
        elif prev != now:
            arrow = "[red]regressed[/]" if prev == "pass" and now == "fail" else "[green]improved[/]"
            console.print(f"  {arrow} {r['id']}: {prev} -> {now}")
    console.print(
        f"  Pass rate: {prior['summary_stats']['pass_rate']:.0%} -> {current['summary_stats']['pass_rate']:.0%}"
    )


def main():
    parser = argparse.ArgumentParser(description="Eval harness for the Slack Daily Brief agent")
    parser.add_argument("--model", default="claude-sonnet-5", help="Model under test")
    parser.add_argument("--judge-model", default="claude-sonnet-5", help="Model used to grade")
    parser.add_argument("--save", action="store_true", help="Write a timestamped JSON report to eval/results/")
    parser.add_argument("--compare", help="Path to a prior saved report to diff against")
    parser.add_argument("--threshold", type=float, default=1.0, help="Minimum pass rate required to exit 0")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[red]Missing ANTHROPIC_API_KEY[/] — set it in .env")
        sys.exit(1)

    console.print(f"[bold cyan]Running {len(FIXTURES)} fixtures against {args.model} "
                  f"(judged by {args.judge_model})[/]\n")
    report = run(args.model, args.judge_model)
    print_report(report)

    if args.compare:
        prior = json.loads(Path(args.compare).read_text())
        print_diff(report, prior)

    if args.save:
        RESULTS_DIR.mkdir(exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        out_path = RESULTS_DIR / f"run_{ts}.json"
        out_path.write_text(json.dumps(report, indent=2))
        console.print(f"\n[green]✓[/] Saved report to {out_path}")

    pass_rate = report["summary_stats"]["pass_rate"]
    if pass_rate < args.threshold:
        console.print(f"\n[bold red]FAIL[/] — pass rate {pass_rate:.0%} below threshold {args.threshold:.0%}")
        sys.exit(1)
    console.print(f"\n[bold green]PASS[/] — pass rate {pass_rate:.0%} meets threshold {args.threshold:.0%}")


if __name__ == "__main__":
    main()
