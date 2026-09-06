# DRAFT routine: inventory refresh

**Status:** draft only — not a live GitHub listener.

## Intent

Refresh `inventory/inventory-2026-09-06.json` (or dated successor) from local
shallow clones + known facts (`--from-json`). Optional `gh auth` check only.

## Proposed command

```bash
cd /workspace/proskills
python3 scripts/inventory_github.py --apply --out inventory/inventory-2026-09-06.json
# optional: --use-gh   # auth check only; no bulk issue dump in baseline
```

## Guardrails

- Prefer static / `--from-json` facts over live API dumps.
- Never print tokens.
- Do not execute scripts from `repos/*` clones.
