---
description: Safety rules for git operations — prevents agents from auto-running destructive commands
---

# Git Safety Rules

## CRITICAL: Commands That Must NEVER Be Auto-Run

The following git commands are ALWAYS destructive and must NEVER be marked `SafeToAutoRun: true`.
The user MUST manually approve them every time, no exceptions:

- `git reset` (any form: --hard, --soft, --mixed)
- `git checkout` (switching branches or `-- <file>` to discard changes)
- `git restore`
- `git clean` (any form: -fd, -fx, etc.)
- `git worktree add`
- `git worktree remove`
- `git worktree prune`
- `git branch -D` (force delete)
- `git push --force` or `git push -f`
- `git rebase` (any form)
- `git stash drop` or `git stash clear`
- Any command containing `--hard`, `--force`, or `-f`

## Safe to Auto-Run (Read-Only)

These are read-only and safe to auto-approve:

- `git status`
- `git log`
- `git diff`
- `git branch` (listing only)
- `git reflog`
- `git show`

## Rule for Agents

Before running any git command, ask yourself:
> "Could this command cause a file to be deleted, overwritten, or moved in the working tree or index?"

If yes → set `SafeToAutoRun: false` and wait for user approval.

## Repository Structure

This repo uses a **git worktree** setup. The main worktree is at:
  `c:\Users\bwang\Documents\GitHub\CL_Analyst_Development`

Running `git worktree remove`, `git checkout` on branches, or `git reset --hard` can silently
destroy untracked/uncommitted files (like oos_predictions.csv, model .pkl files, parquet datasets)
that are excluded from version control but are critical to the project.

## Before Any Branch Switch

Always run `git status` first and confirm with the user that:
1. There are no untracked files that need to be saved
2. The user is aware that untracked files will NOT be preserved by git
