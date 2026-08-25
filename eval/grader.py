"""
LLM-as-judge grader for the Slack Daily Brief agent.

Given a fixture (ground-truth transcript + expected facts) and the summary
the agent actually produced, ask Claude to grade it against the rubric and
return structured JSON via a forced tool call — no regex/string matching,
which is too brittle for freeform markdown output.
"""

import json
import os

import anthropic

GRADE_TOOL = {
    "name": "record_grade",
    "description": "Record the grading result for one eval fixture.",
    "input_schema": {
        "type": "object",
        "properties": {
            "matched_expected": {
                "type": "array",
                "description": "One entry per expected fact provided in the rubric.",
                "items": {
                    "type": "object",
                    "properties": {
                        "fact": {"type": "string"},
                        "found": {"type": "boolean", "description": "Was this fact correctly surfaced in the summary?"},
                        "evidence": {"type": "string", "description": "Quote or paraphrase from the summary, or 'missing'."},
                    },
                    "required": ["fact", "found", "evidence"],
                },
            },
            "hallucinations": {
                "type": "array",
                "description": "Claims in the summary NOT supported by the transcript, or forbidden claims from must_not_mention that appeared anyway.",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "why_unsupported": {"type": "string"},
                    },
                    "required": ["claim", "why_unsupported"],
                },
            },
            "category_errors": {
                "type": "array",
                "description": "Facts placed in the wrong section, e.g. a question presented as a firm decision, or an FYI turned into an action item.",
                "items": {"type": "string"},
            },
            "verdict": {"type": "string", "enum": ["pass", "fail"]},
            "reasoning": {"type": "string", "description": "One or two sentences on the overall verdict."},
        },
        "required": ["matched_expected", "hallucinations", "category_errors", "verdict", "reasoning"],
    },
}

GRADER_SYSTEM = """You are a strict eval grader for an LLM summarization agent.
You will be given:
1. The raw transcript (ground truth for what actually happened)
2. A rubric of facts the summary is EXPECTED to contain
3. A list of things the summary must NOT claim (must_not_mention)
4. The summary the agent actually produced

Grade the summary against the rubric using the record_grade tool. Rules:
- "found: true" only if the fact is substantively present, not just vaguely gestured at.
- Flag ANY claim in the summary not supported by the transcript as a hallucination,
  even if it seems plausible — the summary must be grounded in the transcript only.
- Flag anything from must_not_mention that appears in the summary as a hallucination too.
- verdict is "pass" only if ALL expected facts are found AND there are zero
  hallucinations AND zero category errors. Otherwise "fail".
- Be strict. A summary that's "close enough" but invents an owner, a date, or
  a decision that was really just a question, must fail.
"""


def grade_summary(fixture: dict, summary: str, judge_model: str = "claude-sonnet-5") -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    expected = fixture["expects"]
    expected_facts = []
    for category, items in expected.items():
        for item in items:
            expected_facts.append(f"[{category}] {item}")

    rubric = "\n".join(f"- {f}" for f in expected_facts) or "(no facts expected — summary should be minimal/empty for the relevant sections)"
    forbidden = "\n".join(f"- {f}" for f in fixture.get("must_not_mention", [])) or "(none specified)"

    user_msg = f"""TRANSCRIPT:
{fixture['transcript']}

EXPECTED FACTS (rubric):
{rubric}

MUST NOT MENTION:
{forbidden}

AGENT'S SUMMARY TO GRADE:
{summary}
"""

    resp = client.messages.create(
        model=judge_model,
        max_tokens=2048,
        system=GRADER_SYSTEM,
        tools=[GRADE_TOOL],
        tool_choice={"type": "tool", "name": "record_grade"},
        messages=[{"role": "user", "content": user_msg}],
    )

    for block in resp.content:
        if block.type == "tool_use" and block.name == "record_grade":
            return block.input

    raise RuntimeError("Grader did not return a tool_use block")
