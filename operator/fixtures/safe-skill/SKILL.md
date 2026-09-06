# Safe Skill (Fixture)

**Name:** safe-skill  
**Description:** Benign synthetic candidate used for ProSkills Phase 2 dry-run demos and unit tests.

## Purpose

Demonstrate intake → validate → static_scan without executing any code.

## Steps

1. Read this markdown file as documentation only.
2. Confirm metadata markers are present (`SKILL.md`).
3. Run static text inspection (no imports, no eval, no network).

## Notes

- Contains no executable payloads.
- Safe for offline deterministic pipeline testing.
