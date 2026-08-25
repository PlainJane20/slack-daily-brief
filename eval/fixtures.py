"""
Eval fixtures for the Slack Daily Brief agent.

Each fixture is a synthetic, pre-formatted Slack transcript (same shape
`format_for_prompt()` produces in agent.py) paired with a set of "expected
facts" a correct brief must surface, and — just as important — facts it
must NOT hallucinate. This is what lets the harness catch both recall
failures (missed a real action item) and precision failures (invented one).

Categories covered on purpose, because these are the failure modes that
actually show up in production LLM summarization:
  - explicit vs. inferred action-item ownership
  - duplicate topics across channels that should merge into one item
  - a question that gets answered later in the same thread (must NOT
    surface as "open")
  - decisions vs. questions that only *look* like decisions
  - noise filtering (standup bots, emoji reactions, social chatter)
  - quiet channels still need a line in Channel Summaries
"""

FIXTURES = [
    {
        "id": "explicit_decision_and_owner",
        "transcript": """## #product
[09:02 UTC] priya: We're pushing the v2.1 launch to Sep 15, confirmed with leadership.
[09:03 UTC] priya: @deepak can you own updating the external launch doc by EOD Friday?
[09:04 UTC] deepak: 👍 will do
""",
        "expects": {
            "decisions": ["v2.1 launch date moved to Sep 15"],
            "action_items": [{"owner": "deepak", "task": "update the external launch doc", "due": "EOD Friday"}],
            "blockers": [],
            "open_questions": [],
        },
        "must_not_mention": ["priya needs to update the launch doc"],
    },
    {
        "id": "inferred_owner_from_context",
        "transcript": """## #engineering
[14:10 UTC] sam: The staging DB migration script is failing on the new tenants table.
[14:12 UTC] alex: I can take a look this afternoon, I wrote that migration originally.
[14:13 UTC] sam: sounds good, let us know what you find
""",
        "expects": {
            "decisions": [],
            "action_items": [{"owner": "alex", "task": "investigate/fix the failing staging DB migration"}],
            "blockers": ["staging DB migration script is failing on the new tenants table"],
            "open_questions": [],
        },
        "must_not_mention": ["sam is fixing the migration"],
    },
    {
        "id": "duplicate_topic_merges_across_channels",
        "transcript": """## #product
[08:00 UTC] jordan: Reminder — the Q3 infra migration is delayed two weeks for the compliance audit.

## #engineering
[08:05 UTC] jordan: FYI eng — infra migration pushed back 2 weeks, compliance audit needs to finish first.
""",
        "expects": {
            "decisions": ["Q3 infra migration delayed two weeks for the compliance audit"],
            "action_items": [],
            "blockers": [],
            "open_questions": [],
        },
        "must_not_mention": ["two separate infra migration decisions", "two unrelated delays"],
        "notes": "Should appear as ONE decision, not duplicated per-channel.",
    },
    {
        "id": "question_resolved_later_in_thread_not_open",
        "transcript": """## #engineering
[10:00 UTC] mia: Who owns the auth service after the reorg?
    ↳ raj: That's now @priya's team as of this week
    ↳ mia: got it, thanks!
""",
        "expects": {
            "decisions": [],
            "action_items": [],
            "blockers": [],
            "open_questions": [],
        },
        "must_not_mention": ["who owns the auth service after the reorg? (unanswered)"],
        "notes": "The question WAS answered in-thread — must not show up under Open Questions.",
    },
    {
        "id": "question_left_unanswered_stays_open",
        "transcript": """## #product
[11:00 UTC] taylor: Does anyone know if the v2.1 pricing page copy is final yet?
""",
        "expects": {
            "decisions": [],
            "action_items": [],
            "blockers": [],
            "open_questions": ["is the v2.1 pricing page copy final?"],
        },
        "must_not_mention": [],
    },
    {
        "id": "lookalike_decision_is_actually_a_question",
        "transcript": """## #product
[13:00 UTC] noah: Should we delay the v2.1 launch by a week to fix the onboarding bug?
[13:05 UTC] noah: still waiting to hear back from anyone on this
""",
        "expects": {
            "decisions": [],
            "action_items": [],
            "blockers": [],
            "open_questions": ["should the v2.1 launch be delayed a week to fix the onboarding bug?"],
        },
        "must_not_mention": ["decided to delay v2.1 launch by a week"],
        "notes": "A proposal phrased as a question with no reply must NOT become a firm decision.",
    },
    {
        "id": "noise_filtering_standup_bot_and_reactions",
        "transcript": """## #engineering
[09:00 UTC] standup-bot: Daily standup reminder — post your update in thread!
    ↳ sam: 👍
    ↳ alex: 🎉
[09:15 UTC] sam: The prod data pipeline is failing for 3 customers, need access to prod logs to debug — can someone from platform help?
""",
        "expects": {
            "decisions": [],
            "action_items": [],
            "blockers": ["prod data pipeline failing for 3 customers, sam needs prod log access"],
            "open_questions": [],
        },
        "must_not_mention": ["standup bot reminder as an action item", "emoji reactions as content"],
    },
    {
        "id": "quiet_channel_still_listed",
        "transcript": """## #random

""",
        "expects": {
            "decisions": [],
            "action_items": [],
            "blockers": [],
            "open_questions": [],
            "channel_summaries_mention": ["#random"],
        },
        "must_not_mention": [],
        "notes": "Channel had zero messages — must still appear in Channel Summaries as quiet, per system prompt rule.",
    },
    {
        "id": "announcement_not_miscategorized_as_action_item",
        "transcript": """## #verily-it-announce
[07:00 UTC] it-team: New security policy requires 2FA on all service accounts by Sep 1. No action needed if you've already enabled it.
[07:01 UTC] it-team: All-hands moved to Thursday 10 AM PT this week only.
""",
        "expects": {
            "decisions": [],
            "action_items": [],
            "blockers": [],
            "open_questions": [],
            "announcements": ["2FA required on service accounts by Sep 1", "all-hands moved to Thursday 10 AM PT"],
        },
        "must_not_mention": ["action item: enable 2FA"],
        "notes": "'No action needed' FYIs must land in Announcements, not be turned into a checklist item.",
    },
    {
        "id": "multiple_distinct_decisions_not_merged",
        "transcript": """## #product
[10:00 UTC] priya: Decision: we're cutting the legacy CSV export feature in v2.1.
[10:05 UTC] priya: Separately — we've also decided to rename the "Workspaces" tab to "Projects" everywhere in the UI.
""",
        "expects": {
            "decisions": [
                "cutting the legacy CSV export feature in v2.1",
                "renaming the 'Workspaces' tab to 'Projects'",
            ],
            "action_items": [],
            "blockers": [],
            "open_questions": [],
        },
        "must_not_mention": ["decisions merged into a single vague bullet"],
        "notes": "Two unrelated decisions in the same channel — must be listed as two, not blended into one.",
    },
]
