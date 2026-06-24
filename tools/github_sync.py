"""Commit and push changed project files to the configured GitHub remote.

This script never stages .env because it is excluded by .gitignore. It does not pull,
rebase, or resolve conflicts automatically: a remote conflict is logged for review.
"""

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = PROJECT_ROOT / "logs" / "github_sync.log"


def run_git(*args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=check,
    )


def log(message):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[{timestamp}] {message}"
    LOG_PATH.open("a", encoding="utf-8").write(line + "\n")
    print(line)


def main():
    parser = argparse.ArgumentParser(description="Commit and push HR Chatbot changes to GitHub.")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without committing or pushing.")
    parser.add_argument("--message", help="Optional commit message.")
    args = parser.parse_args()

    remote = run_git("remote", "get-url", "origin", check=False)
    if remote.returncode:
        log("ERROR: Git remote 'origin' is not configured.")
        return 1

    status = run_git("status", "--porcelain").stdout.strip()
    if not status:
        log("No changed files. Nothing to commit.")
        return 0

    if args.dry_run:
        log("Dry run: changes detected. No commit or push was made.")
        print(status)
        return 0

    run_git("add", "-A")
    staged = run_git("diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        log("No stageable changes. Nothing to commit.")
        return 0

    message = args.message or f"chore: daily project sync {datetime.now().astimezone():%Y-%m-%d}"
    commit = run_git("commit", "-m", message, check=False)
    if commit.returncode:
        log("ERROR: Commit failed: " + (commit.stderr.strip() or commit.stdout.strip()))
        return 1

    push = run_git("push", "origin", "main", check=False)
    if push.returncode:
        log(
            "ERROR: Commit was created locally but push failed. Review the remote before retrying. "
            + (push.stderr.strip() or push.stdout.strip())
        )
        return 1

    revision = run_git("rev-parse", "--short", "HEAD").stdout.strip()
    log(f"Pushed commit {revision} to origin/main.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
