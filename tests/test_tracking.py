import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import tracking
from tracking import _extract_open_questions, _similar, annotate_stale_items


def brief(*bullets):
    return "## ✅ Action Items\n- not a question — [#x]\n\n## ❓ Open Questions\n" + "\n".join(bullets) + "\n"


Q = "- Still no word on the analytics vendor decision — [#got-a-sec]"


def run(md, day, path, **kw):
    return annotate_stale_items(md, today=day, history_path=path, **kw)


def test_extract_only_open_questions_and_strips_suffixes():
    items = _extract_open_questions(brief(Q, "- Who owns the rollout? (no response) — [#eng]"))
    assert [i["text"] for i in items] == ["Still no word on the analytics vendor decision", "Who owns the rollout?"]
    assert [i["channel"] for i in items] == ["#got-a-sec", "#eng"]


def test_first_day_not_flagged_and_recorded(tmp_path):
    h = tmp_path / "h.json"
    out = run(brief(Q), "2026-01-01", h)
    assert "open" not in out.split("Open Questions")[1]
    assert len(tracking.load_history(h)) == 1


def test_second_day_same_question_is_flagged(tmp_path):
    h = tmp_path / "h.json"
    run(brief(Q), "2026-01-01", h)
    out = run(brief(Q), "2026-01-02", h)
    assert "(open 2 days running)" in out


def test_same_day_rerun_does_not_double_count(tmp_path):
    h = tmp_path / "h.json"
    run(brief(Q), "2026-01-01", h)
    out = run(brief(Q), "2026-01-01", h)
    assert "days running" not in out
    assert tracking.load_history(h)[0]["seen_dates"] == ["2026-01-01"]


def test_different_channel_never_matches(tmp_path):
    h = tmp_path / "h.json"
    run(brief(Q), "2026-01-01", h)
    out = run(brief(Q.replace("#got-a-sec", "#other")), "2026-01-02", h)
    assert "days running" not in out


def test_dissimilar_text_resets_streak(tmp_path):
    h = tmp_path / "h.json"
    run(brief(Q), "2026-01-01", h)
    out = run(brief("- Did legal approve the new data retention policy? — [#got-a-sec]"), "2026-01-02", h)
    assert "days running" not in out


def test_threshold_boundary():
    assert _similar("abc", "abc") == 1.0
    assert tracking.MATCH_THRESHOLD == 0.6
    # moderately reworded question stays above the 0.6 threshold
    assert _similar("Still no word on the analytics vendor decision",
                    "Still no word on the analytics vendor decision yet") >= tracking.MATCH_THRESHOLD
    # unrelated text falls below it
    assert _similar("Still no word on the analytics vendor decision",
                    "Who owns the rollout?") < tracking.MATCH_THRESHOLD


def test_unseen_item_is_pruned_as_resolved(tmp_path):
    h = tmp_path / "h.json"
    run(brief(Q), "2026-01-01", h)
    run(brief("- Totally different open question about budgets — [#fin]"), "2026-01-02", h)
    texts = [e["text"] for e in tracking.load_history(h)]
    assert texts == ["Totally different open question about budgets"]


def test_streak_continues_to_three_days(tmp_path):
    h = tmp_path / "h.json"
    for d in ("2026-01-01", "2026-01-02"):
        run(brief(Q), d, h)
    assert "(open 3 days running)" in run(brief(Q), "2026-01-03", h)
