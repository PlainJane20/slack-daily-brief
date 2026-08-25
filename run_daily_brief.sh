#!/bin/bash
# Wrapper for launchd — activates the venv and runs a fully unattended pass.
# --quick skips the interactive channel picker; --out skips the "save?"
# confirm prompt. Together these make it safe to run with no TTY attached.
set -euo pipefail
cd "$(dirname "$0")"
source venv/bin/activate
python3 agent.py --quick --out ~/slack-summary.md
