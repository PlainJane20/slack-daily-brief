"""
Day-over-day tracking for open items.

Without this, an unanswered question shows up in the brief identically every
single day forever, with no signal that it's been pending for a week. This
module fixes that: after each run, pull the bullets out of the generated
brief's "Open Questions" section, fuzzy-match them against still-open items
from previous runs, and inject a stale flag directly into the markdown for
anything that's lingered past the threshold.

Deliberately no extra LLM call here — day-to-day wording of the same
lingering question is usually similar enough that difflib's SequenceMatcher
is sufficient, and it's free, deterministic, and easy to debug compared to
another model round-trip for what's fundamentally a string-similarity problem.

Known limitation: if Claude paraphrases the same underlying question quite
differently across two runs (measured as low as ~0.49 similarity for two
reasonable paraphrases of "did we finalize the vendor?" in testing), the
match misses and the streak resets to day 1. In practice this is rare when
summarizing the same source messages run over run, but it's a real tradeoff
of choosing string similarity over semantic similarity. The fix, if this
turns out to matter in practice, is an embedding-based or LLM-judged match
instead of SequenceMatcher — deliberately not built until there's evidence
it's needed.

History is a flat JSON file: one entry per still-open item, with every date
it's been seen. An item that stops appearing in the brief is assumed resolved
and pruned from history on the next run — no manual "mark resolved" step.
"""

import json
import re
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

DEFAULT_HISTORY_PATH = Path(__file__).parent / "history" / "open_items.json"
STALE_AFTER_DAYS = 2  # flag once an item has been seen on >= this many distinct days
MATCH_THRESHOLD = 0.6  # SequenceMatcher ratio required to consider two items "the same"


def load_history(path: Path = DEFAULT_HISTORY_PATH) -> list:
    if not path.exists():
        return []
    return json.loads(path.read_text())


def save_history(entries: list, path: Path = DEFAULT_HISTORY_PATH):
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(entries, indent=2))


def _extract_open_questions(summary_markdown: str) -> list:
    """Pull each bullet under '## ❓ Open Questions' as {line, text, channel}."""
    items = []
    in_section = False
    for line in summary_markdown.splitlines():
        if line.startswith("## "):
            in_section = "Open Questions" in line
            continue
        if in_section and line.strip().startswith("- "):
            channel_match = re.search(r"\[(#[^\]]+)\]\s*$", line)
            channel_tag = channel_match.group(1) if channel_match else ""
            core = line.strip()[2:]
            core = re.sub(r"\s*—\s*\[#[^\]]+\]\s*$", "", core)
            core = re.sub(r"\s*\((no response|unanswered|no one confirmed)\)\s*$", "", core, flags=re.IGNORECASE)
            items.append({"line": line, "text": core.strip(), "channel": channel_tag})
    return items


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def annotate_stale_items(
    summary_markdown: str,
    today: str = None,
    history_path: Path = DEFAULT_HISTORY_PATH,
    stale_after_days: int = STALE_AFTER_DAYS,
) -> str:
    """
    Match today's open questions against history, update the history file on
    disk, and return the markdown with a stale-days flag injected into any
    bullet that's lingered past the threshold.
    """
    today = today or date.today().isoformat()
    history = load_history(history_path)
    today_items = _extract_open_questions(summary_markdown)

    matched_history_indices = set()
    annotated = summary_markdown

    for item in today_items:
        best_idx, best_score = None, 0.0
        for i, h in enumerate(history):
            if i in matched_history_indices or h.get("channel") != item["channel"]:
                continue
            score = _similar(h["text"], item["text"])
            if score > best_score:
                best_idx, best_score = i, score

        if best_idx is not None and best_score >= MATCH_THRESHOLD:
            h = history[best_idx]
            matched_history_indices.add(best_idx)
            h["last_seen"] = today
            if today not in h["seen_dates"]:
                h["seen_dates"].append(today)
            days_open = len(h["seen_dates"])
            if days_open >= stale_after_days:
                flagged_line = item["line"].rstrip() + f"  🔴 *(open {days_open} days running)*"
                annotated = annotated.replace(item["line"], flagged_line, 1)
        else:
            history.append({
                "text": item["text"],
                "channel": item["channel"],
                "first_seen": today,
                "last_seen": today,
                "seen_dates": [today],
            })

    # Anything not seen in today's brief is assumed resolved — drop it rather
    # than let history grow forever with stale-but-actually-resolved items.
    history = [h for h in history if h["last_seen"] == today]

    save_history(history, history_path)
    return annotated
