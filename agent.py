#!/usr/bin/env python3
"""
Slack Daily Summary Agent
─────────────────────────
Interactive terminal app: pick your channels, get a smart daily brief.

Usage:
  python agent.py               # interactive channel picker
  python agent.py --quick       # skip picker, summarize all configured channels
  python agent.py --hours 8     # look back only 8 hours
  python agent.py --out FILE    # save to FILE without prompting
  python agent.py --setup       # (re)run the token setup wizard
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml
import questionary
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.rule import Rule
from rich.table import Table
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import anthropic

load_dotenv()
console = Console()

CONFIG_PATH = Path(__file__).parent / "config.yaml"
ENV_PATH    = Path(__file__).parent / ".env"

DEFAULT_CHANNELS = [
    "general",
]

# ─── Config ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return yaml.safe_load(CONFIG_PATH.read_text()) or {}
    return {}

def save_config(cfg: dict):
    CONFIG_PATH.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True))

def get_cfg(cfg: dict, key: str, default):
    return cfg.get(key, default)


# ─── First-run setup wizard ──────────────────────────────────────────────────

def run_setup_wizard():
    console.print()
    console.print(Panel(
        "[bold cyan]Slack Daily Brief — Setup Wizard[/]\n\n"
        "Let's get you connected. This takes about 2 minutes.",
        expand=False,
        border_style="cyan",
    ))
    console.print()

    # ── Slack token ──
    existing_slack = os.environ.get("SLACK_USER_TOKEN") or os.environ.get("SLACK_BOT_TOKEN")
    if existing_slack:
        console.print("[green]✓[/] Slack token already configured")
    else:
        console.print("[bold]Step 1 — Slack token[/]\n")
        console.print("Fastest path — self-install an app on any workspace you're a")
        console.print("member of (a personal workspace needs no admin/IT approval):")
        console.print()
        console.print("  1. Go to [cyan]https://api.slack.com/apps[/] → [bold]Create New App[/] → [bold]From scratch[/]")
        console.print("  2. Pick your workspace")
        console.print("  3. Left sidebar → [bold]OAuth & Permissions[/] → [bold]Scopes[/] → [bold]User Token Scopes[/]")
        console.print("     → add: channels:history, channels:read, groups:history,")
        console.print("       groups:read, users:read")
        console.print("  4. Scroll up → [bold]Install to Workspace[/] → Allow")
        console.print("  5. Copy the [bold]User OAuth Token[/] (starts with xoxp-...)")
        console.print()

        token = questionary.password(
            "Paste your Slack token (xoxp-... or xoxb-...), or press Enter to skip:"
        ).ask()

        if token and token.strip():
            _write_env("SLACK_USER_TOKEN", token.strip())
            os.environ["SLACK_USER_TOKEN"] = token.strip()
            console.print("[green]✓[/] Slack token saved to .env")
        else:
            console.print("[yellow]Skipped — add SLACK_USER_TOKEN to .env when you have it[/]")

    console.print()

    # ── Anthropic key ──
    existing_anthropic = os.environ.get("ANTHROPIC_API_KEY")
    if existing_anthropic:
        console.print("[green]✓[/] Anthropic API key already configured")
    else:
        console.print("[bold]Step 2 — Anthropic API key[/]\n")
        console.print("Get one at: [cyan]https://console.anthropic.com/[/]")
        console.print()

        key = questionary.password(
            "Paste your Anthropic API key (sk-ant-...), or press Enter to skip:"
        ).ask()

        if key and key.strip():
            _write_env("ANTHROPIC_API_KEY", key.strip())
            os.environ["ANTHROPIC_API_KEY"] = key.strip()
            console.print("[green]✓[/] Anthropic key saved to .env")
        else:
            console.print("[yellow]Skipped — add ANTHROPIC_API_KEY to .env when you have it[/]")

    console.print()
    console.print(Rule())
    console.print()
    console.print("[green bold]Setup complete![/] Run [bold cyan]python agent.py[/] to get your first brief.")
    console.print()


def _write_env(key: str, value: str):
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n")


# ─── Slack helpers ────────────────────────────────────────────────────────────

def make_slack_client() -> WebClient:
    token = os.environ.get("SLACK_USER_TOKEN") or os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        console.print(
            "[red]No Slack token found.[/] Run [bold]python agent.py --setup[/] to configure."
        )
        sys.exit(1)
    return WebClient(token=token)


def get_channel_id(client: WebClient, name: str) -> Optional[str]:
    """Return the channel ID for a channel name the user is a member of."""
    name = name.lstrip("#").lower()
    cursor = None
    while True:
        kwargs = {
            "exclude_archived": True,
            "types": "public_channel,private_channel",
            "limit": 200,
        }
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = client.conversations_list(**kwargs)
        except SlackApiError:
            return None
        for ch in resp.get("channels", []):
            if ch["name"].lower() == name and ch.get("is_member"):
                return ch["id"]
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return None


def get_message_count(client: WebClient, channel_id: str, oldest_ts: float) -> int:
    """Quick count of messages since oldest_ts."""
    try:
        resp = client.conversations_history(
            channel=channel_id, oldest=str(oldest_ts), limit=200
        )
        return len([
            m for m in resp.get("messages", [])
            if m.get("subtype") not in ("channel_join", "channel_leave", "bot_add")
        ])
    except SlackApiError:
        return 0


def fetch_messages(client: WebClient, channel_id: str, oldest_ts: float, cfg: dict) -> list:
    """Fetch messages and thread replies for a channel."""
    fetch_threads = get_cfg(cfg, "fetch_threads", True)
    max_replies   = get_cfg(cfg, "max_replies_per_thread", 20)
    skip_trivial  = get_cfg(cfg, "skip_trivial_messages", True)
    min_len       = get_cfg(cfg, "min_message_length", 10)

    messages = []
    cursor = None

    while True:
        kwargs = {"channel": channel_id, "oldest": str(oldest_ts), "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = client.conversations_history(**kwargs)
        except SlackApiError:
            break

        for msg in resp.get("messages", []):
            if msg.get("subtype") in ("channel_join", "channel_leave", "bot_add"):
                continue
            text = msg.get("text", "").strip()
            if skip_trivial and len(text) < min_len:
                continue

            entry = {
                "ts": msg["ts"],
                "user": msg.get("user", msg.get("username", "?")),
                "text": text,
                "replies": [],
            }

            if fetch_threads and msg.get("reply_count", 0) > 0:
                try:
                    tresp = client.conversations_replies(
                        channel=channel_id,
                        ts=msg["ts"],
                        limit=max_replies if max_replies > 0 else 200,
                    )
                    for r in tresp.get("messages", [])[1:]:
                        rt = r.get("text", "").strip()
                        if skip_trivial and len(rt) < min_len:
                            continue
                        entry["replies"].append({
                            "user": r.get("user", "?"),
                            "text": rt,
                        })
                    time.sleep(0.3)
                except SlackApiError:
                    pass

            messages.append(entry)

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.3)

    return sorted(messages, key=lambda m: float(m["ts"]))


def build_user_map(client: WebClient, user_ids: set) -> dict:
    umap = {}
    for uid in user_ids:
        if not uid or uid == "?":
            continue
        try:
            resp = client.users_info(user=uid)
            p = resp["user"].get("profile", {})
            umap[uid] = p.get("display_name") or p.get("real_name") or uid
            time.sleep(0.15)
        except SlackApiError:
            umap[uid] = uid
    return umap


def format_for_prompt(channel_messages: dict, user_map: dict) -> str:
    parts = []
    for name, msgs in channel_messages.items():
        if not msgs:
            continue
        lines = [f"## #{name}"]
        for msg in msgs:
            user = user_map.get(msg["user"], msg["user"])
            ts = datetime.fromtimestamp(float(msg["ts"]), tz=timezone.utc).strftime("%H:%M UTC")
            lines.append(f"[{ts}] {user}: {msg['text']}")
            for r in msg.get("replies", []):
                ru = user_map.get(r["user"], r["user"])
                lines.append(f"    ↳ {ru}: {r['text']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# ─── Claude summarization ────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a senior program manager's personal assistant at Verily.
Read raw Slack channel logs and produce a crisp, actionable daily brief.

Output format — use exactly these markdown headers, skip any section if there is nothing to put in it:

# 📋 Slack Daily Brief — {date}

## 🔑 Key Decisions
Bullet list of decisions made or agreed upon across channels.

## ✅ Action Items
Format each as: "- [ ] **@person**: what they need to do — [#channel]"
Infer owners from context when not explicitly assigned.

## 🚨 Blockers & Urgencies
Anything blocked, on fire, or needing immediate attention.

## 📣 Announcements & FYIs
Launches, policy changes, deadlines, important one-way information.

## ❓ Open Questions
Questions asked in channels that haven't been answered yet.

## 📰 Channel Summaries
One short paragraph per channel that had activity.
For quiet channels, note them briefly: "**#channel-name** — No significant activity."

---

Rules:
- Be concise and direct. No filler phrases like "It's worth noting that..."
- Always try to name action item owners.
- Combine duplicate topics across channels into one item.
- Skip standup bot outputs, pure emoji reactions, and social chatter unless they contain real information.
- If a channel had zero messages, still include it in Channel Summaries as quiet.
"""


def summarize(formatted: str, model: str, focus: list, date_str: str) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    focus_note = f"\n\nPlease give extra attention to: {', '.join(focus)}" if focus else ""

    msg = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT.replace("{date}", date_str),
        messages=[{
            "role": "user",
            "content": (
                f"Here are the Slack messages from the past window. "
                f"Please produce the daily brief.{focus_note}\n\n{formatted}"
            ),
        }],
    )
    return msg.content[0].text


# ─── Output ──────────────────────────────────────────────────────────────────

def display_summary(summary: str):
    console.print()
    console.print(Rule("[bold cyan]Your Daily Brief[/]", style="cyan"))
    console.print()
    console.print(Markdown(summary))
    console.print()
    console.print(Rule(style="dim"))


def save_summary(summary: str, path: str):
    p = Path(path).expanduser()
    p.write_text(summary, encoding="utf-8")
    console.print(f"\n[green]✓[/] Saved to [bold]{p}[/]")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Slack Daily Summary Agent")
    parser.add_argument("--hours", type=float, help="Hours to look back (default: from config or 24)")
    parser.add_argument("--quick", action="store_true", help="Skip channel picker; use all configured channels")
    parser.add_argument("--out", help="Save output to this file (skips save prompt)")
    parser.add_argument("--setup", action="store_true", help="Run the token setup wizard")
    args = parser.parse_args()

    if args.setup:
        run_setup_wizard()
        return

    # ── Preflight ──
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[red]Missing ANTHROPIC_API_KEY.[/] Run [bold]python agent.py --setup[/]")
        sys.exit(1)

    cfg = load_config()
    lookback   = args.hours or get_cfg(cfg, "lookback_hours", 24)
    model      = get_cfg(cfg, "claude_model", "claude-sonnet-5")
    focus      = get_cfg(cfg, "focus_areas", [])
    configured = get_cfg(cfg, "channels", DEFAULT_CHANNELS)
    out_cfg    = get_cfg(cfg, "output_file", "~/slack-summary.md")

    now        = datetime.now(tz=timezone.utc)
    oldest_ts  = (now - timedelta(hours=lookback)).timestamp()
    date_str   = now.strftime("%A, %B %-d, %Y")

    # ── Header ──
    console.print()
    console.print(Panel(
        f"[bold cyan]Slack Daily Brief[/]  ·  {date_str}  ·  Last [bold]{int(lookback)}h[/]",
        expand=False,
        border_style="cyan",
    ))
    console.print()

    # ── Connect + resolve channels ──
    slack = make_slack_client()

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console, transient=True) as prog:
        task = prog.add_task("Checking channels...", total=None)
        channel_info = []  # (name, id_or_None, msg_count)
        for name in configured:
            prog.update(task, description=f"Checking #{name}...")
            cid = get_channel_id(slack, name)
            if cid:
                count = get_message_count(slack, cid, oldest_ts)
            else:
                count = -1  # not accessible
            channel_info.append((name, cid, count))
            time.sleep(0.3)

    # ── Activity table ──
    table = Table(box=box.ROUNDED, show_header=True, header_style="bold dim", expand=False)
    table.add_column("Channel", style="cyan")
    table.add_column("Messages (last {:.0f}h)".format(lookback), justify="right")
    table.add_column("", justify="center")

    accessible = []
    for name, cid, count in channel_info:
        if cid is None:
            table.add_row(f"#{name}", "—", "[red]not a member[/]")
        elif count == 0:
            table.add_row(f"#{name}", "[dim]quiet[/]", "[dim]✓[/]")
            accessible.append((name, cid, count))
        else:
            table.add_row(f"#{name}", f"[green]{count}[/]", "[green]✓[/]")
            accessible.append((name, cid, count))

    console.print(table)
    console.print()

    if not accessible:
        console.print(
            "[yellow]No accessible channels found.[/] "
            "Check your Slack token scopes and channel names in config.yaml."
        )
        sys.exit(0)

    # ── Channel selection ──
    if args.quick:
        selected = [(n, cid) for n, cid, _ in accessible]
    else:
        choices = []
        for name, cid, count in accessible:
            hint = f"  ({count} messages)" if count > 0 else "  (quiet)"
            choices.append(questionary.Choice(
                title=f"#{name}{hint}",
                value=(name, cid),
                checked=(count > 0),  # pre-check channels with activity
            ))
        choices.append(questionary.Choice(
            title="[+] Add a channel not listed above...",
            value="__add__",
            checked=False,
        ))

        selected_raw = questionary.checkbox(
            "Select channels to include in your brief:",
            choices=choices,
            instruction="(space to toggle, ↑↓ to move, enter to confirm)",
        ).ask()

        if selected_raw is None:
            console.print("[dim]Cancelled.[/]")
            return

        selected = []
        new_channels = []

        for item in selected_raw:
            if item == "__add__":
                extra = questionary.text(
                    "Channel name(s) to add (comma-separated, no #):"
                ).ask()
                if extra:
                    for ch_name in [c.strip().lstrip("#").lower() for c in extra.split(",") if c.strip()]:
                        with console.status(f"Looking up #{ch_name}..."):
                            cid = get_channel_id(slack, ch_name)
                        if cid:
                            selected.append((ch_name, cid))
                            new_channels.append(ch_name)
                            console.print(f"[green]✓[/] Found #{ch_name}")
                        else:
                            console.print(f"[red]✗[/] #{ch_name} — not found or not a member of this channel")
            else:
                selected.append(item)

        # Offer to persist new channels
        if new_channels:
            if questionary.confirm(
                f"Save {', '.join('#' + c for c in new_channels)} to config.yaml for next time?",
                default=True,
            ).ask():
                existing = get_cfg(cfg, "channels", [])
                cfg["channels"] = list(dict.fromkeys(existing + new_channels))
                save_config(cfg)
                console.print("[green]✓[/] config.yaml updated")

    if not selected:
        console.print("[yellow]No channels selected.[/]")
        return

    console.print()

    # ── Fetch messages ──
    all_messages: dict = {}
    all_user_ids: set  = set()

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console, transient=True) as prog:
        task = prog.add_task("Fetching...", total=None)

        for name, cid in selected:
            prog.update(task, description=f"Reading #{name}...")
            msgs = fetch_messages(slack, cid, oldest_ts, cfg)
            all_messages[name] = msgs
            for m in msgs:
                all_user_ids.add(m["user"])
                for r in m.get("replies", []):
                    all_user_ids.add(r["user"])
            time.sleep(0.4)

        total_msgs = sum(len(v) for v in all_messages.values())
        prog.update(task, description=f"Resolving {len(all_user_ids)} users...")
        user_map = build_user_map(slack, all_user_ids)

        prog.update(task, description=f"Summarizing {total_msgs} messages with Claude...")
        formatted = format_for_prompt(all_messages, user_map)

    if not formatted.strip():
        console.print(
            "[yellow]No meaningful messages in selected channels for this time window.[/]"
        )
        return

    summary = summarize(formatted, model, focus, date_str)

    # ── Display ──
    display_summary(summary)

    # ── Save ──
    if args.out:
        save_summary(summary, args.out)
    elif out_cfg and out_cfg != "stdout_only":
        if questionary.confirm(f"Save brief to {out_cfg}?", default=True).ask():
            save_summary(summary, out_cfg)


if __name__ == "__main__":
    main()
