# DRAFT routine: local health report

**Status:** draft only — do not enable live listeners without user approval card.

## Intent

Periodically emit a redacted health JSON summarizing local SQLite counts and the
saved inventory file.

## Proposed command

```bash
cd /workspace/proskills
python3 scripts/health_report.py \
  --inventory-json inventory/inventory-2026-09-06.json \
  --state-root /workspace/proskills/state \
  --db /workspace/proskills/state/pipeline.sqlite3 \
  --apply
```

## Trigger (proposed)

- Manual, or scheduled (e.g. daily) **after** approval.

## Guardrails

- No network calls from `health_report.py`.
- `--include-github` is ignored / false by design.
- Output under `state/logs/` only; secrets redacted.
