# operator-routines (draft definitions)

Workspace-only documentation of proposed Grok Bot / Cursor routines for the
ProSkills operator. **Not live.** Enabling GitHub listeners requires a user
approval card.

See `automations/drafts/` and the "Proposed routines" section of `BASELINE.md`.

## Access

- Read: local scripts, fixtures, inventory JSON, temp state
- Write: only under explicit `--state-root` when `--apply`
- No merge, publish, delete, permission changes, or real Oracle delivery

## Failure behavior

- Missing approval → leave as draft markdown only
- Network / secrets → fail closed; redact
