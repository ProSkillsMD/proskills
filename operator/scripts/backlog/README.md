# Backlog dry-run

Deterministic Python tool to classify open submission issues. **No GitHub writes.**

```bash
python3 build_backlog_dry_run.py \
  --issues /path/to/open-submissions.json \
  --catalog /path/to/skills-catalog.json \
  --out-dir ./out
```

Outputs (local; `out/` gitignored):
- `backlog-cleanup-manifest.json` — per-issue disposition
- `backlog-cleanup-summary.json` / `.csv` — aggregates
- `backlog-rollback-manifest.json` — empty mutation list (dry-run)

Do not commit full issue dumps or credentials.
