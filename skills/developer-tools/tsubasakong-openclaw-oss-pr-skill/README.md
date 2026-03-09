# OpenClaw OSS PR Skill

This repo contains a reusable OpenClaw skill for running a disciplined OSS pull-request workflow on GitHub.

## Contents

- `oss-pr-workflow/` — source skill folder
- `oss-pr-workflow.skill` — packaged artifact for easy sharing

## What the skill teaches

- pick high-probability OSS issues
- check repo etiquette before opening a PR
- validate honestly
- keep PR bodies template-compliant
- prioritize review feedback and actionable CI
- interact with maintainers in a friendly, low-noise way
- structure OpenClaw automation into queue, opener, and monitor lanes
- keep queue/tracker state in machine-readable files

## New in this revision

- helper scripts for queue refill, safe claim, status updates, tracker sync, state validation, and PR-body rendering
- concrete issue-discovery and state-schema references
- error-recovery, cron-setup, CLI-design, and end-to-end walkthrough references
