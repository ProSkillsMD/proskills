# openclaw-skills (Synthetic Supervised Fixture)

**Name:** openclaw-skills  
**Source issue:** https://github.com/ProSkillsMD/proskills/issues/6014  
**Event id:** supervised-6014  
**Kind:** TEXT-ONLY stub — no remote skill code downloaded or executed.

## Issue metadata (extracted text only)

- **Title:** [SUBMISSION] openclaw-skills
- **Repo (declared in issue body):** https://github.com/xlhbh89757/openclaw-skills
- **Description:** OpenClaw skill
- **Author:** xlhbh89757
- **Source:** GitHub auto-discovery (Scout)
- **State:** open
- **Labels (text):** submission, auto-discovered, curio:publishing, curio:routed-to-gamora, curio:routed-to-drax, curio:routed-to-rocket, drax-pass, rocket-pass, gamora-conditional
- **Issue author:** curiobot4proskills[bot]
- **Created (UTC):** 2026-04-23T08:00:14Z
- **Updated (UTC):** 2026-05-01T07:04:50Z

## Purpose

Provide a local TEXT-ONLY candidate fixture for supervised dry-run / temp-state
`--apply` of `scripts/pipeline.py` without fetching or executing remote skill
code from the submission URL.

## Steps (documentation only)

1. Treat this file as markdown documentation.
2. Confirm SKILL.md presence for validate stage.
3. Allow static text inspection only (no imports, no eval, no network).

## Notes

- Contains no executable payloads.
- Remote repository contents were intentionally NOT cloned for this fixture.
- Prefer `fixtures/safe-skill` for the supervised pipeline local-path when
  exercising the deterministic stages; this stub records issue context.
