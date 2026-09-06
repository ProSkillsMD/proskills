# PR & Issue Triage Skill

**Category:** Automation  
**Author:** dinakars777  
**Repository:** https://github.com/dinakars777/openclaw-triage-skill  
**Status:** Verified & Listed  

## Overview

Efficiently triage GitHub pull requests and issues with intelligent classification, prioritization, staleness detection, and complexity estimation. Designed specifically for high-volume repositories with thousands of open PRs and issues.

## Key Features

- **PR Classification** — Automatically classify PRs by type (bug-fix, feature, refactor, docs, deps, ci, test, chore)
- **Risk Assessment** — Evaluate risk levels from low to critical based on files touched, test coverage, and scope
- **Staleness Detection** — Identify stale (30+ days) and abandoned (90+ days) PRs/issues
- **Complexity Estimation** — Estimate PR size: trivial, small, medium, large, or epic
- **Batch Triage** — Process multiple PRs/issues at once with summary tables
- **CI Analysis** — Identify CI blockers affecting multiple PRs
- **Security Scanning** — Flag security-sensitive changes automatically
- **Suggested Actions** — Get recommendations: merge, review, request-changes, close-stale, etc.

## Installation

### Prerequisites
- `gh` CLI must be installed and authenticated
- Current directory should be a clone of the target repository, OR use `--repo owner/name` flag

### Install via clawhub
```bash
clawhub install dinakars777/openclaw-triage-skill
```

## Usage Examples

### Triage a Single PR
```bash
triage-pr #1234 [--repo owner/repo]
```

### Triage a Single Issue
```bash
triage-issue #567
```

### Batch Triage
```bash
triage-batch prs [--repo owner/repo] [--state open] [--since 7d] [--limit 50]
```

## Scores & Verification

| Dimension | Score | Notes |
|-----------|-------|-------|
| Functionality | 8/10 | Comprehensive triage with all major features |
| Documentation | 8/10 | Clear commands and examples |
| Security | 8/10 | Safe handling of repos and auth |
| Maintenance | 7/10 | Active repository |
| Usefulness | 9/10 | Highly practical for maintainers |
| Uniqueness | 8/10 | Specialized triage tooling |
| Code Quality | 8/10 | Well-structured bash scripts |
| **Average** | **8.14/10** | **APPROVED** |

Verified by Gamora on 2026-03-15.

---

**Support:** https://github.com/dinakars777/openclaw-triage-skill/issues  
**Last Updated:** 2026-03-15  
**Verification Status:** ✅ Approved by Gamora
