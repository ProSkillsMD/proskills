# openclaw-codex-pm-skills

A community-driven adaptation layer for applying PM skill frameworks (inspired by [phuryn/pm-skills](https://github.com/phuryn/pm-skills)) to **OpenClaw + Codex-style** workflows.

This project is designed for teams who want practical, reusable product-management skills that are:

- portable across environments,
- easy to audit,
- safe for collaborative use,
- and realistic to maintain in open source.

---

## Table of Contents

- [Project Overview](#project-overview)
- [Origin and Positioning](#origin-and-positioning)
- [Vision](#vision)
- [Project Direction (2026)](#project-direction-2026)
- [Core Principles](#core-principles)
- [Who This Project Is For](#who-this-project-is-for)
- [What Exists Today](#what-exists-today)
- [What We Are Building Next](#what-we-are-building-next)
- [Architecture and Repository Structure](#architecture-and-repository-structure)
- [How to Install](#how-to-install)
- [How to Use in Real Work](#how-to-use-in-real-work)
- [Quality Standards](#quality-standards)
- [Playwright Interactive Execution Principles](#playwright-interactive-execution-principles)
- [Contributing Guide](#contributing-guide)
- [Governance and Decision-Making](#governance-and-decision-making)
- [Compatibility Notes](#compatibility-notes)
- [Roadmap](#roadmap)
- [References](#references)
- [License](#license)

---

## Project Overview

`openclaw-codex-pm-skills` turns PM workflows into skill assets that can be consistently applied in OpenClaw-based agent operations.

It focuses on practical outcomes:

- better discovery quality,
- clearer strategy artifacts,
- faster PRD drafting,
- more transparent prioritization,
- and repeatable outputs in team environments.

This is **not** a prompt dump.
It is a structured, evolving skill system intended for real delivery workflows.

---

## Origin and Positioning

This repository is inspired by the ideas and workflow style of:

- [phuryn/pm-skills](https://github.com/phuryn/pm-skills)

and adapts those concepts for OpenClaw/Codex usage patterns.

### Important scope statement

- This is an **independent community adaptation**, not an official fork.
- We preserve intent (PM rigor and workflow clarity), not runtime-specific behavior.
- We intentionally avoid vendor-locked orchestration semantics.

---

## Vision

Build the most practical open PM skill pack for agent-assisted product development, where outputs are:

1. **actionable** (usable in real planning and execution),
2. **auditable** (clear rationale and traceable assumptions),
3. **collaborative** (easy for multiple contributors to improve),
4. **portable** (usable across OpenClaw/Codex-style setups).

---

## Project Direction (2026)

### Direction A — Practical PM execution

Prioritize skills that directly improve shipping quality:

- discovery
- strategy
- PRD
- prioritization
- risk/assumption management

### Direction B — Shared team standards

Standardize output formats so multiple contributors can collaborate without style drift.

### Direction C — Safe, transparent automation

Support automation with explicit boundaries and predictable outputs, especially in group/chat contexts.

### Direction D — Open contributor ecosystem

Design the project so contributors can add domain packs (B2B SaaS, Web3, infra tools, etc.) with consistent quality.

---

## Core Principles

1. **Portability over lock-in**
2. **Clarity over magic**
3. **Determinism over ambiguity**
4. **Safety over convenience**
5. **Incremental improvement over giant rewrites**

---

## Who This Project Is For

- Product managers using agent-assisted workflows
- Founders who need PM structure in fast-moving build cycles
- AI-native teams shipping iterative product changes
- OSS contributors who want reusable PM skill assets

---

## What Exists Today

Current initial skill packs:

- `pm-discovery-foundations`
- `pm-strategy-canvas`
- `pm-prd-drafting`
- `pm-prioritization-helper`
- `web-qa-playwright` (now includes QA inventory + failure-recovery references for interactive browser testing)

Current docs:

- `docs/INSTALL_OPENCLAW.md`
- `docs/COMPATIBILITY.md`
- `docs/MAPPING_PM_SKILLS.md`
- `docs/PR_MERGE_POLICY.md`
- `docs/WEB_QA_PLAYWRIGHT_REPLAY_PROFILE.md`
- `docs/WEB_QA_PLAYWRIGHT_PRECHECK.md` (adds a 1-minute pre-run stability/reproducibility checklist before the first interactive step)
- `docs/WEB_QA_PLAYWRIGHT_FAILURE_HANDOFF.md`
- `docs/WEB_QA_PLAYWRIGHT_JSON_HANDOFF.md`
- `docs/WEB_QA_PLAYWRIGHT_EXECUTION_LOOP.md` (now explicitly documents Playwright-interactive-style stability, reproducibility, step-by-step verification, and failure-recovery rules)
- `docs/PLAYWRIGHT_FAILURE_RECOVERY_HANDOFF.md` (adds a compact repair-order checklist for target refs -> evidence -> chronology -> recovery prose handoff)
- `docs/PLAYWRIGHT_INTERACTIVE_REPAIR_LOOP.md` (adds a concise rerun playbook for stabilizing the session, repairing one checkpoint at a time, restoring evidence, and confirming recovery before signoff)
- `docs/WEB_QA_PLAYWRIGHT_STEP_TEMPLATE.md` (adds a copyable single-checkpoint template so interactive QA reports keep one action, one verification step, one evidence artifact, and one recovery note per checkpoint)
- `docs/WEB_QA_PLAYWRIGHT_SIGNOFF_LANES.md` (adds a READY-vs-BLOCKED section triage note so operators can choose the next deterministic replay lane directly from strict-plus JSON)
- `docs/WEB_QA_PLAYWRIGHT_STABILITY_CHECKLIST.md` (condenses the same principles into a fast operator rerun checklist for blocked interactive runs)
- Canonical opt-in traceability example: `examples/web_qa_playwright_strict_plus_with_check_refs_pass.md`
- Deterministic replay validation contract: use `--strict-plus --require-qa-inventory-check-refs --json-out artifacts/validation.json` so CI stores replay-ready metadata (`active_profile_preset`, 5/3/2 counts, QA inventory check refs, QA inventory missing-check coverage, unresolved failed-check coverage, deduplicated next-action failed-check refs, checkpoint target/artifact refs, reused checkpoint refs, per-checkpoint ref coverage counts, and section-level checkpoint timestamp coverage (`functional`/`visual`/`off_happy`) plus per-section replay-readiness blocker counts/coverage for replay triage) as a machine-readable artifact
- Replay triage JSON now also exposes `effective_replay_ready_sections`, `effective_replay_blocked_sections`, and `effective_replay_section_status`, so downstream reruns can instantly split section repair into READY vs BLOCKED lanes without recomputing blocker counts from raw per-section maps.
- Replay triage JSON now also exposes `effective_replay_readiness_hotspot_tied_sections` and `effective_replay_readiness_hotspot_tied_section_labels`, so rerun tooling can keep every section-sharing hotspot lane explicit instead of flattening ties into a single winner.
- Replay triage JSON now also exposes `effective_replay_readiness_hotspot_next_step`, `effective_replay_readiness_hotspot_primary_blocker_summary_by_blocker_key`, giving automation a copy-ready repair instruction per blocker key (`missing_target_refs`, `incomplete_evidence_refs`, etc.) without rebuilding hotspot checkpoint lists downstream.
- Next-action replay metadata now also exposes `next_action_replay_support_dimensions_present` and `next_action_replay_support_missing_dimensions`, so rerun bots can tell whether stable target refs and artifact refs are already present before reconstructing the repair lane from prose.
- Replay triage JSON now also exposes `effective_replay_readiness_hotspot_next_step`, `effective_replay_readiness_hotspot_primary_blocker_summary_by_section`, so rerun tooling can hand the hottest blocked section (`functional`, `visual`, `off_happy`) to an operator with one copy-ready repair sentence instead of reconstructing section/blocker/checkpoint context downstream.
- Replay triage JSON now also exposes `effective_replay_readiness_hotspot_primary_blocker_key_by_section`, `effective_replay_readiness_hotspot_primary_blocker_checkpoint_ids_by_section`, and `effective_replay_readiness_hotspot_primary_blocker_next_step_by_section`, so tied hotspot sections keep their own first-fix lane without forcing downstream automation to flatten every blocked section into one global blocker queue.
- Replay triage JSON now also exposes structured `effective_replay_readiness_hotspot_primary_blocker` and `effective_replay_readiness_hotspot_primary_blocker_by_section` objects, so rerun tooling can consume the hottest blocker lane (global or per section) without stitching together separate key/count/checkpoint fields.
- Replay triage JSON now also exposes `effective_replay_readiness_hotspot_primary_blocker_summary_by_section`, giving rerun bots a copy-ready `blocker_key: checkpoint ids` sentence per hotspot section without rebuilding section-scoped handoff prose.
- Parser-facing QA inventory mapping examples: `docs/WEB_QA_PLAYWRIGHT_REPLAY_PROFILE.md` now includes PASS/FAIL JSON snippets for the opt-in `--require-qa-inventory-check-refs` contract so downstream CI can compare payload shape directly.
- QA inventory triage snippets now cover malformed mappings, partial coverage drift, and recovered PASS payloads side-by-side so machine parsers can distinguish formatting repair from missing-check restoration without replaying the full fixture set.
- Missing-`Checks:` recovery guidance: the replay-profile doc now pairs the isolated FAIL fixture with a count-based recovery checklist (`qa_inventory_check_ref_count` should move from `0` back to `10`) so CI triage can be resolved deterministically.
- Side-by-side PASS/FAIL smoke commands: `docs/WEB_QA_PLAYWRIGHT_REPLAY_PROFILE.md` now keeps paired copy/paste commands for the canonical PASS fixture and the isolated missing-`Checks:` FAIL fixture so replay triage can confirm both validation and recovery paths quickly.
- Replay-profile smoke script now asserts the missing-`Checks:` FAIL fixture still exposes the full 10-item QA universe and 5/3/2 checkpoint split while failing with exactly one mapping error, so CI triage can distinguish coverage drift from mapping-format drift immediately.
- Replay-profile smoke script now also covers isolated missing-artifact-path and status-consistency FAIL fixtures, keeping Playwright-style evidence-capture and step-by-step verification drift parser-visible with a single reusable smoke entrypoint.
- Partial QA-inventory drift now has an explicit landing-page triage cue: if `qa_inventory_check_ref_count` is non-zero but `qa_inventory_missing_check_ref_count` stays above `0`, treat it as incomplete coverage drift (for the isolated fixture: `9 mapped / 1 missing`), not malformed `Checks:` syntax.
- Replay-profile smoke now also asserts the parser-facing coverage-rate split (`1.0` for malformed-but-complete mappings vs `0.9` for partial coverage drift), so CI can distinguish syntax repair from missing-check recovery without recomputing ratios.
- Replay triage JSON now exposes `qa_inventory_check_ref_coverage_rate` and `next_action_failed_check_coverage_rate` so CI can spot partial mapping/handoff drift without recomputing ratios downstream.
- Signoff metadata JSON now also exposes `signoff_field_values`, `signoff_field_status`, `present_signoff_fields`, `missing_signoff_fields`, and `signoff_field_coverage_rate`, so CI replay gates can tell which closure fields are already machine-readable vs still missing without reparsing markdown bullets.
- Failure handoff JSON now also groups both referenced and unresolved failed check ids by classification (`next_action_failed_check_ids_by_classification`, `unresolved_failed_check_ids_by_classification`) so follow-up reruns can route selector/runtime/product fixes without rebuilding that map downstream.
- Failure handoff JSON now also exposes per-classification follow-up coverage (`next_action_failed_check_coverage_rate_by_classification`, `unresolved_failed_check_coverage_rate_by_classification`) so CI can tell whether selector/runtime/product failures were fully handed off vs left unresolved without recomputing ratios downstream.
- Failure handoff JSON now also mirrors recovery owners for both referenced and unresolved failed checks (`next_action_failed_check_recovery_owners`, `unresolved_failed_check_recovery_owners`) so rerun routing can go straight to the right owner without reparsing markdown blocks.
- Failure handoff JSON now also groups failed checks by recovery owner (`failed_check_ids_by_recovery_owner`, `next_action_failed_check_ids_by_recovery_owner`, `unresolved_failed_check_ids_by_recovery_owner`) so rerun triage can route the whole owner lane in one lookup instead of rebuilding owner -> check maps downstream.
- Failure handoff JSON now also emits direct classification maps for the checks named in `Next action:` and the checks still left unresolved (`next_action_failed_check_classifications_by_id`, `unresolved_failed_check_classifications_by_id`) so replay automation can branch by selector/runtime/product without reconstructing it from grouped arrays.
- Checkpoint replay metadata now includes `missing_checkpoint_target_ref_ids` and `missing_checkpoint_artifact_ref_ids`, letting CI distinguish “checkpoint exists but lacks stable ref/artifact evidence” from ordering/count drift without re-parsing the markdown.
- Replay JSON now also exposes `checkpoint_evidence_ref_ids` plus `checkpoint_evidence_ref_coverage_rate`, so CI can see which checkpoints already carry both a stable UI target ref and an artifact path before triggering a replay.
- Section-based evidence triage is now documented end-to-end: the smoke script asserts missing/reused evidence coverage by `functional` / `visual` / `off_happy`, and `docs/WEB_QA_PLAYWRIGHT_JSON_HANDOFF.md` explains how to repair those sections in a deterministic order.
- Replay JSON now also exposes `missing_checkpoint_timestamp_count_by_section` and `missing_checkpoint_timestamp_coverage_rate_by_section`, so deterministic rerun triage can see which section still lacks timestamped checkpoints before replaying the full report.
- An isolated strict-plus missing-timestamp fixture (`examples/web_qa_playwright_strict_fail_missing_timestamp_only.md`) now keeps chronology repair reproducible without mixing it with monotonic-order drift.

Current examples:

- discovery prompt example
- strategy prompt example
- PRD prompt example
- prioritization prompt example
- web QA Playwright sample interactive run (`examples/web_qa_playwright_sample_run.md`)
- web QA Playwright strict-fail fixture for duplicate checkpoints (`examples/web_qa_playwright_strict_fail_duplicate_checkpoint.md`)
- web QA Playwright strict-fail fixture for missing failure evidence (`examples/web_qa_playwright_strict_fail_missing_evidence.md`)
- web QA Playwright strict-fail fixture for missing failure classification (`examples/web_qa_playwright_strict_fail_missing_classification.md`)
- web QA Playwright strict-fail fixture for malformed failure breakdown summary (`examples/web_qa_playwright_strict_fail_malformed_failure_breakdown.md`)
- web QA Playwright strict-fail fixture for missing `## 3) Execution log` header (`examples/web_qa_playwright_strict_fail_missing_execution_log_header.md`)
- web QA Playwright strict-plus combined fail fixture (duplicate checkpoints + missing failure metadata in one report) (`examples/web_qa_playwright_strict_plus_combined_fail.md`)
- web QA Playwright strict-plus pass fixture (deterministic replay gates all satisfied, including replay readiness signoff) (`examples/web_qa_playwright_strict_plus_pass.md`)
- web QA Playwright strict-plus pass fixture with explicit QA inventory `Checks:` mappings for opt-in traceability validation (`examples/web_qa_playwright_strict_plus_with_check_refs_pass.md`)
- web QA Playwright strict-plus isolated missing-`Checks:` fail fixture for QA inventory mapping triage (`examples/web_qa_playwright_strict_fail_missing_check_refs_only.md`)
- web QA Playwright strict-plus partial-coverage fail fixture for incomplete QA inventory mapping drift (`examples/web_qa_playwright_strict_fail_partial_check_refs_only.md`)
- web QA Playwright strict-plus isolated artifact-ref reuse fail fixture for CI triage (`examples/web_qa_playwright_strict_fail_artifact_ref_reuse_only.md`)
- web QA Playwright strict-plus isolated monotonic-timestamp fail fixture for replay-order triage (`examples/web_qa_playwright_strict_fail_monotonic_timestamp_only.md`)
- web QA Playwright strict-plus isolated missing-timestamp fail fixture for chronology-field triage (`examples/web_qa_playwright_strict_fail_missing_timestamp_only.md`)
- web QA Playwright strict-plus isolated status-consistency fail fixture for checkpoint/check drift triage (`examples/web_qa_playwright_strict_fail_status_inconsistency_only.md`)
- web QA Playwright strict-plus isolated missing-artifact-path fail fixture for evidence-capture triage (`examples/web_qa_playwright_strict_fail_missing_artifact_paths_only.md`)
- web QA Playwright strict-plus isolated missing-recovery-owner fail fixture for explicit owner-handoff triage (`examples/web_qa_playwright_strict_fail_missing_recovery_owner_only.md`)

---

## What We Are Building Next

Near-term priorities:

1. stronger skill specifications (inputs/outputs/evaluation criteria),
2. review checklist per skill PR,
3. domain-specific extensions,
4. compatibility matrix across OpenClaw runtime patterns,
5. starter integration recipes for real product teams.

---

## Architecture and Repository Structure

```text
openclaw-codex-pm-skills/
├─ skills/                      # Core and extension skill packs
│  ├─ pm-discovery-foundations/
│  ├─ pm-strategy-canvas/
│  ├─ pm-prd-drafting/
│  └─ pm-prioritization-helper/
├─ docs/
│  ├─ INSTALL_OPENCLAW.md       # Installation and activation guidance
│  ├─ COMPATIBILITY.md          # Runtime compatibility and constraints
│  └─ MAPPING_PM_SKILLS.md      # Upstream concept mapping
├─ examples/                    # Real prompt usage examples
├─ scripts/
│  ├─ install_local.sh          # Local installation helper
│  └─ validate_web_qa_report.py # Fixed-count QA report validator (5/3/2)
└─ .github/workflows/ci.yml     # Minimal structural validation
```

---

## How to Install

```bash
git clone https://github.com/sonwr/openclaw-codex-pm-skills.git
cd openclaw-codex-pm-skills
./scripts/install_local.sh
```

By default this installs to `~/.openclaw/skills`.

For custom location:

```bash
./scripts/install_local.sh /path/to/skills
```

Then ensure your OpenClaw configuration allows these skills for your target agent.

---

## How to Use in Real Work

### 1) Discovery sprint
Use `pm-discovery-foundations` to produce:

- problem framing
- assumption ranking
- low-cost experiments
- decision recommendation

### 2) Strategy alignment
Use `pm-strategy-canvas` to produce:

- target segment
- strategic bets
- risk mitigation plan
- measurable checkpoints

### 3) Build planning
Use `pm-prd-drafting` to produce:

- one-page PRD
- scope boundaries
- acceptance criteria

### 4) Prioritization session
Use `pm-prioritization-helper` to produce:

- weighted rankings
- exclusion rationale
- revisit triggers

### 5) Browser QA signoff discipline
Use `web-qa-playwright` and validate the final markdown run artifact:

```bash
python3 scripts/validate_web_qa_report.py --file examples/web_qa_playwright_sample_run.md --strict
```

For machine-readable CI output, add `--json` (stdout) or `--json-out <path>` (artifact file).

For a reusable explanation of deterministic replay expectations and failure-recovery discipline, see `docs/WEB_QA_PLAYWRIGHT_REPLAY_PROFILE.md`.
For a 1-minute pre-run checklist that freezes target/profile/evidence before the first click, see `docs/WEB_QA_PLAYWRIGHT_PRECHECK.md`.
That note now also documents a stepwise replay recovery order: restore stable target refs first, replay evidence second, checkpoint chronology third, and human recovery notes last.
For a faster blocked-run triage workflow focused on `report_metadata`, owner handoff, and failed-check reruns, see `docs/WEB_QA_PLAYWRIGHT_FAILURE_HANDOFF.md`.
For machine-readable CI contracts and JSON artifact routing, see `docs/WEB_QA_PLAYWRIGHT_JSON_HANDOFF.md`.
For reviewer-facing merge rules and copy-paste PASS/blocked-run commands, see `docs/PR_MERGE_POLICY.md`.
For a concise execution loop that mirrors Playwright-interactive principles (inventory -> bootstrap -> verify -> recover), see `docs/WEB_QA_PLAYWRIGHT_EXECUTION_LOOP.md`.

Practical rule of thumb for browser automation signoff:
- lock one reproducible preset/profile for the whole run,
- verify each meaningful UI transition before moving on,
- capture evidence in the exact state being signed off,
- and write failure owner + next action before widening reruns.
For a shorter operator handoff that freezes profile/refs before reruns, see `docs/WEB_QA_PLAYWRIGHT_STABILITY_CHECKLIST.md`.
For a JSON-first READY-vs-BLOCKED triage note that tells operators which replay lane to repair next, see `docs/WEB_QA_PLAYWRIGHT_SIGNOFF_LANES.md`.

Recommended operator loop before every interactive rerun:

1. lock the replay preset (`--playwright-interactive-profile` / `--strict-plus`) and keep the same target refs/artifact naming scheme,
2. validate one isolated drift fixture before broad reruns,
3. repair only the failing invariant (selector, evidence, ordering, or status consistency),
4. rerun the canonical PASS fixture to prove recovery before updating docs/CI handoff text.

This keeps browser QA changes aligned with Playwright-interactive priorities: stability first, reproducible evidence, step-by-step verification, and explicit failure recovery.

`--strict` adds reproducibility gates aligned with Playwright-interactive principles:

- deterministic preconditions (URL, viewport, test account)
- deterministic check labeling (F1..F5 / V1..V3 / O1..O2)
- screenshot evidence density for visual checks (at least one inline shot per V-check)
- normalized status tokens on every check row (`PASS`/`FAIL`)
- explicit signoff fields (regression count + merge recommendation)
- signoff-check consistency (reported regression count must equal checklist `FAIL` line count)
- section summary ratio consistency (`x/y pass`) vs detailed PASS/FAIL lines
- step-by-step execution log checkpoints for all fixed check ids (F1..F5, V1..V3, O1..O2) in deterministic order
- failure recovery traceability for failed checks (`Expected`/`Observed`/`First failure timestamp`/`Retry`)

Optional hardening flags:

- `--strict-plus`: enable `--strict` and all hardening flags below as a single CI preset for deterministic replay and failure recovery.
- `--playwright-interactive-profile`: apply a Playwright-interactive reliability preset (strict-plus equivalent) to prioritize stability, reproducibility, step verification, and failure recovery in one flag.

Playwright-interactive operating principles reflected in this preset and fixture set:

1. **Stability first** — require explicit checkpoint timestamps/status tokens/target refs so repeated runs stay comparable.
2. **Reproducibility first** — keep deterministic replay artifacts (`--json-out`, isolated fail fixtures, PASS fixture) available for CI and triage.
3. **Step-by-step verification** — enforce execution-log shape plus QA inventory/checkpoint accounting instead of trusting free-form prose.
4. **Failure recovery** — require artifact paths, failure classification, and recovery owner/plan metadata so broken runs remain actionable.
5. **Explicit handoff (opt-in)** — enable `--require-next-action` when you want a concrete `Next action:` signoff line for the next deterministic rerun.
6. **Failure-traceable handoff (opt-in)** — add `--require-next-action-failed-check-ref` when blocked runs must point the next owner to a failed check id such as `F2` or `V1`.
- `--deterministic-replay-profile`: alias for `--playwright-interactive-profile` for teams that prefer deterministic replay wording in CI policies.
- `--strict-replay-profile`: alias for `--playwright-interactive-profile` for teams that want explicit strict replay wording in CI presets.
- `--ci-replay-profile`: alias for `--playwright-interactive-profile` for teams that prefer shorter replay-policy naming in CI jobs.
  - CI now smoke-tests all three aliases against the same PASS fixture so downstream policy names cannot silently drift.
- `--enforce-checkpoint-format`: require each checkpoint line to use `action -> verification` format for clearer step-level reproducibility.
- `--require-checkpoint-timestamps`: require each checkpoint line to include an ISO-8601 UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ`) for deterministic replay timelines.
- `--enforce-monotonic-checkpoint-timestamps`: require checkpoint timestamps to be monotonic in execution-log order, making replay order drift visible in CI.
- `--enforce-checkpoint-status-tokens`: require each checkpoint line to include explicit `PASS`/`FAIL` token for parser-safe replay traces.
- `--require-visual-checkpoint-evidence`: require each visual checkpoint line (`V1..V3`) to include an inline screenshot path for reproducible visual proof.
- `--require-checkpoint-artifact-paths`: require every checkpoint line to include at least one inline artifact path (screenshot/video/log/trace) for post-failure replay and evidence auditing.
- `--require-checkpoint-target-refs`: require every checkpoint line to include a stable target reference token (`ref=<id>`) so interactive replay can bind actions to deterministic UI targets.
- `--enforce-checkpoint-target-ref-uniqueness`: require each checkpoint target ref to map to exactly one checkpoint, so replay selectors stay stable and unambiguous across retries.
- `--enforce-checkpoint-artifact-ref-uniqueness`: require each inline artifact path to appear in only one checkpoint line, reducing ambiguous replay evidence mapping.
- `--require-failure-evidence-artifact-paths`: require failed checks to include an inline artifact path on the `Evidence:` line, so triage links are machine-verifiable.
- `--require-failure-recovery-plan`: require failed checks to include a `Recovery plan:` line with deterministic next-step recovery instructions.
- `--require-failure-recovery-owner`: require failed checks to include a `Recovery owner:` line so ownership is explicit for follow-up recovery.
- `--require-next-action`: require signoff to include a `Next action:` line so replay handoff remains explicit even on PASS runs.
- `--require-next-action-failed-check-ref`: when regressions are present, require `Next action:` to mention at least one failed check id so the next rerun owner knows exactly which check to recover first.
- `--enforce-failure-timestamp-order`: require failed-check `First failure timestamp` values to be monotonic in checklist order, preventing chronology drift in failure triage.
- `--enforce-checkpoint-to-check-status-consistency`: require each checkpoint line to include `PASS`/`FAIL` and match the checklist status for that same check id (prevents replay-log/checklist drift).
- `--require-failure-classification-summary`: require a signoff line in the form `Failure breakdown: selector=<n>, runtime=<n>, product=<n>` and validate it against failed-check classifications.
- `--require-execution-log-step-count-match`: require the execution log to contain only checkpoint bullets and exactly 10 checkpoint lines (F1..F5, V1..V3, O1..O2), which tightens deterministic replay step accounting.
- `--require-qa-inventory-section`: require the report to include a `## QA inventory` section with explicit environment/tool metadata for replayable incident audits.
- `--require-qa-inventory-check-refs --require-qa-inventory-full-coverage`: tighten QA inventory mapping so every checklist id is referenced at least once and stdout explicitly reports the full-coverage gate when enabled.

The validator always enforces the fixed structure rule (5 functional / 3 visual / 2 off-happy-path checks).

Troubleshooting fixture commands:

```bash
python3 scripts/validate_web_qa_report.py --file examples/web_qa_playwright_strict_fail_duplicate_checkpoint.md --strict
python3 scripts/validate_web_qa_report.py --file examples/web_qa_playwright_strict_fail_missing_evidence.md --strict
python3 scripts/validate_web_qa_report.py --file examples/web_qa_playwright_strict_fail_missing_classification.md --strict
python3 scripts/validate_web_qa_report.py --file examples/web_qa_playwright_strict_fail_missing_execution_log_header.md --strict
python3 scripts/validate_web_qa_report.py --file examples/web_qa_playwright_strict_fail_malformed_failure_breakdown.md --strict --require-failure-classification-summary
python3 scripts/validate_web_qa_report.py --file examples/web_qa_playwright_strict_fail_monotonic_timestamp_only.md --strict-plus
python3 scripts/validate_web_qa_report.py --file examples/web_qa_playwright_strict_fail_missing_artifact_paths_only.md --strict-plus
```

Expected outcomes:
- duplicate-checkpoint fixture: `FAIL` with `duplicate checkpoint ids` error.
- missing-evidence fixture: `FAIL` with `failed check ... must include an Evidence: line` error.
- missing-classification fixture: `FAIL` with `failed check ... must include a Failure classification: line` error.
- malformed-failure-breakdown fixture: `FAIL` with failure-classification-summary mismatch when `--require-failure-classification-summary` is enabled.
- missing-execution-log-header fixture: `FAIL` with strict-mode `Execution log` section-header error.
- missing-target-refs fixture: `FAIL` with exactly one strict-plus `checkpoint target refs` error for selector traceability triage.
- missing-artifact-paths-only fixture: `FAIL` with exactly one strict-plus `checkpoint artifact paths` error for evidence-capture triage.
- monotonic-timestamp-only fixture: `FAIL` with exactly one `checkpoint timestamp order` error under `--strict-plus`.

### Strict-plus isolated triage matrix

Use these deterministic fixtures to isolate one replay drift at a time:

- `web_qa_playwright_strict_fail_artifact_ref_reuse_only.md` → artifact mapping drift (`checkpoint artifact refs`)
- `web_qa_playwright_strict_fail_monotonic_timestamp_only.md` → replay-order drift (`checkpoint timestamp order`)
- `web_qa_playwright_strict_fail_status_inconsistency_only.md` → checklist/log state drift (`status consistency`)
- `web_qa_playwright_strict_fail_missing_target_refs.md` → selector-target traceability drift (`checkpoint target refs`)
- `web_qa_playwright_strict_fail_target_ref_reuse_only.md` → selector alias drift (`target ref uniqueness`)
- `web_qa_playwright_strict_fail_missing_artifact_paths_only.md` → evidence-capture drift (`checkpoint artifact paths`)

Copy-paste recovery commands:

```bash
# Artifact mapping drift
python3 scripts/validate_web_qa_report.py --file examples/web_qa_playwright_strict_fail_artifact_ref_reuse_only.md --strict-plus --json-out .tmp/web-qa-artifact-ref-reuse.json

# Replay-order drift
python3 scripts/validate_web_qa_report.py --file examples/web_qa_playwright_strict_fail_monotonic_timestamp_only.md --strict-plus --json-out .tmp/web-qa-monotonic-only.json

# Checklist/log status drift
python3 scripts/validate_web_qa_report.py --file examples/web_qa_playwright_strict_fail_status_inconsistency_only.md --strict-plus --json-out .tmp/web-qa-status-inconsistency.json

# Selector traceability drift
python3 scripts/validate_web_qa_report.py --file examples/web_qa_playwright_strict_fail_missing_target_refs.md --strict-plus --json-out .tmp/web-qa-missing-target-refs.json

# Selector alias drift
python3 scripts/validate_web_qa_report.py --file examples/web_qa_playwright_strict_fail_target_ref_reuse_only.md --strict-plus --json-out .tmp/web-qa-target-ref-reuse.json

# Evidence capture drift
python3 scripts/validate_web_qa_report.py --file examples/web_qa_playwright_strict_fail_missing_artifact_paths_only.md --strict-plus --json-out .tmp/web-qa-missing-artifact-paths.json
```

This mirrors Playwright-interactive-style recovery: isolate a single failing invariant, verify it deterministically, then fix only that layer before broad reruns.

CI now includes a negative-fixture guard: each strict-fail fixture must fail validation so policy regressions are caught early.
CI now also smoke-tests the isolated target-ref-uniqueness fixture so selector alias drift cannot silently bypass replay-policy coverage.
Need a quick alias smoke check? `docs/WEB_QA_PLAYWRIGHT_REPLAY_PROFILE.md` now includes copy-paste commands that run the same PASS fixture through `--playwright-interactive-profile`, `--deterministic-replay-profile`, `--strict-replay-profile`, and `--ci-replay-profile` so CI owners can confirm alias parity without inventing new fixtures.
Need an executable repo-local smoke check? Run `scripts/smoke_replay_profile_examples.sh` to verify all replay-profile aliases against the replay-ready PASS fixture, the isolated missing-`Checks:` / partial-coverage QA inventory FAIL fixtures, and every strict-plus one-invariant replay fixture in the triage matrix.
The smoke script now also emits an explicit malformed-vs-partial QA inventory triage assertion, so recovery automation can tell apart `10 mapped / 0 missing` format drift from `9 mapped / 1 missing` coverage drift without replaying fixtures by hand.
It also locks the full Playwright-interactive recovery loop in one place: isolate exactly one failing invariant, confirm the parser-facing error stays singular, then repair only that layer before broader reruns.
CI also snapshots a strict-plus PASS fixture to JSON and asserts the downstream parser-facing payload shape (`status`, enabled gates, and fixed `counts`).
- JSON payloads now include `validation_schema_version` so CI parsers can detect contract changes explicitly.
- JSON payloads also include `active_profile_preset` so downstream CI can tell which replay preset produced the result without inferring from multiple booleans. Replay metadata also separates first-seen checkpoint refs from `checkpoint_reused_target_refs` / `checkpoint_reused_artifact_refs` so strict handoff jobs can detect evidence reuse without recomputing it downstream. It also emits `missing_checkpoint_evidence_dimensions_by_id` / `missing_checkpoint_evidence_dimensions_by_section` plus aggregate `missing_checkpoint_evidence_dimension_counts`, so replay queues can see whether a checkpoint is blocked by a missing target ref, artifact ref, timestamp, or a combination of them before opening the markdown report. Reused evidence is now also grouped per checkpoint via `checkpoint_reused_target_refs_by_id` / `checkpoint_reused_artifact_refs_by_id`, which makes follow-up reruns immediately traceable to the duplicated checkpoint instead of forcing CI to reverse-map shared refs. Replay metadata now also exposes `has_signoff_section`, `reported_regressions`, `merge_recommendation`, `replay_readiness`, `replay_readiness_reference_regressions`, `replay_readiness_consistent_with_failed_checks`, `replay_readiness_blockers`, `replay_readiness_blocker_keys`, `replay_readiness_blocker_counts`, `effective_replay_readiness_blocker_keys`, `effective_replay_readiness_blocker_counts`, `effective_replay_readiness_blocker_count`, `replay_readiness_blocker_keys_by_section`, `effective_replay_readiness_added_blocker_keys_by_section`, `effective_replay_readiness_blocker_delta_by_section`, `missing_signoff_fields`, `missing_signoff_field_count`, `signoff_field_coverage_rate`, `has_next_action`, and raw `next_action_text`, so handoff jobs can distinguish missing closure, contradictory READY/BLOCKED signoff, and partially filled signoff blocks before parsing free-form markdown.

Parser-facing JSON snippets for downstream CI contracts:

```json
{
  "status": "PASS",
  "validation_schema_version": 1,
  "active_profile_preset": "strict-plus",
  "counts": {"functional": 5, "visual": 3, "off_happy_path": 2},
  "report_metadata": {
    "qa_inventory_check_refs": ["F1", "F2", "F3", "F4", "F5", "V1", "V2", "V3", "O1", "O2"],
    "failed_check_ids": [],
    "failed_check_classifications_by_id": {},
    "failed_check_classification_counts": {"selector": 0, "runtime": 0, "product": 0},
    "missing_failed_check_classification_ids": [],
    "next_action_failed_check_refs": []
  }
}
```

```json
{
  "status": "FAIL",
  "validation_schema_version": 1,
  "errors": [
    "qa inventory check refs: every QA inventory bullet must include 'Checks:' mapping"
  ],
  "report_metadata": {
    "qa_inventory_check_refs": [],
    "failed_check_ids": [],
    "failed_check_classifications_by_id": {},
    "failed_check_classification_counts": {"selector": 0, "runtime": 0, "product": 0},
    "missing_failed_check_classification_ids": [],
    "next_action_failed_check_refs": []
  }
}
```

---

## Quality Standards

A skill should be merged only if it is:

- **Clear**: purpose and when-to-use are explicit.
- **Structured**: includes deterministic output sections.
- **Testable**: has at least one realistic usage example.
- **Safe**: no hidden destructive instructions.
- **Maintainable**: concise, readable, and versionable.

For browser automation and interactive QA work, this repository also follows a local Playwright-interactive operating loop:

- stabilize the target before acting,
- verify each action step-by-step,
- capture evidence on the same checkpoint,
- classify failures for deterministic recovery,
- and hand off the next action clearly for replay.

Primary reference: `docs/PLAYWRIGHT_INTERACTIVE_EXECUTION_PRINCIPLES.md`

---

## Playwright Interactive Execution Principles

If you are updating web QA prompts, fixtures, or report validation rules, read:

- `docs/PLAYWRIGHT_INTERACTIVE_EXECUTION_PRINCIPLES.md`
- `docs/WEB_QA_PLAYWRIGHT_REPLAY_PROFILE.md`
- `docs/WEB_QA_PLAYWRIGHT_STABILITY_CHECKLIST.md`
- `docs/WEB_QA_PLAYWRIGHT_FAILURE_HANDOFF.md`

These docs keep browser automation work aligned with deterministic replay, step-by-step verification, and failure-recovery handoff expectations.

---

## Contributing Guide

We welcome contributors.

### Contribution flow

1. Fork and create a branch
2. Add or improve one focused skill/doc change
3. Include an example in `examples/` if behavior changed
4. Open a PR with:
   - purpose,
   - before/after behavior,
   - sample output format,
   - compatibility notes

### Commit style

- `feat:` new capability
- `fix:` correctness/safety improvement
- `docs:` documentation improvements
- `chore:` maintenance tasks

### PR review checklist (required)

- [ ] Scope is clear and narrow
- [ ] Output format is explicit
- [ ] Example is included/updated
- [ ] No vendor-locked assumptions hidden in skill text
- [ ] Compatibility implications are documented

---

## Governance and Decision-Making

This repository currently follows a maintainer-driven model with transparent rationale in PRs.

As contributor volume grows, governance will evolve to:

- lightweight RFCs for major changes,
- domain maintainers for extension packs,
- release notes with migration guidance.

---

## Compatibility Notes

- Works well for file-based skill execution patterns.
- Requires adaptation for assistant-specific slash-command engines.
- This repository intentionally avoids hard dependencies on a single vendor runtime.

See full details in `docs/COMPATIBILITY.md`.

---

## Roadmap

### v0.2

- richer skill specs
- contributor checklist hardening
- additional examples

### v0.3

- domain extension packs
- stronger compatibility documentation
- quality scoring for skill submissions

### v0.4

- reusable "skill test scenarios" for regression checks
- release process for community contributions

### v1.0

Stable, community-supported PM skill framework for OpenClaw/Codex-style teams.

---

## References

- Upstream inspiration: [phuryn/pm-skills](https://github.com/phuryn/pm-skills)
- This project: adaptation for OpenClaw + Codex-style workflows

---

## License

MIT
