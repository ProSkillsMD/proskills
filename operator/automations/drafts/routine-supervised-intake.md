# DRAFT routine: supervised intake (fixture-only)

**Status:** draft only — requires approval before any issue-listener automation.

## Intent

Run deterministic pipeline stages against TEXT-ONLY fixtures derived from
submission issue metadata (never download/execute remote skill code).

## Proposed command

```bash
cd /workspace/proskills
# dry-run
python3 scripts/pipeline.py \
  --event-id supervised-6014 \
  --name openclaw-skills \
  --source github-issue \
  --source-uri https://github.com/ProSkillsMD/proskills/issues/6014 \
  --local-path fixtures/safe-skill

# apply to TEMP state only
TMP=$(mktemp -d /tmp/proskills-supervised-XXXXXX)
python3 scripts/pipeline.py \
  --event-id supervised-6014 \
  --name openclaw-skills \
  --source github-issue \
  --source-uri https://github.com/ProSkillsMD/proskills/issues/6014 \
  --local-path fixtures/safe-skill \
  --state-root "$TMP" --db "$TMP/pipeline.sqlite3" --apply
```

## Guardrails

- Use `fixtures/safe-skill` or `fixtures/supervised-*` TEXT stubs only.
- No publish / merge / real Oracle.
- Temp state-root for apply paths during pilot.
