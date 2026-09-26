# ProSkills pipeline on GitHub Actions (ProSkills GitHub App)

Three workflows in `.github/workflows/` move the box routines to Actions. They do nothing until enabled:
every job starts with a `guard` job (`operator/scripts/actions_guard.py`) and the real job runs only when

* repository variable `PROSKILLS_ACTIONS_ENABLED` is exactly `true`,
* secrets `PROSKILLS_APP_ID` and `PROSKILLS_APP_PRIVATE_KEY` exist (only their presence is checked), and
* no open issue has the label `operator-lock` (add this label to any issue to pause Actions instantly).

Otherwise the guard logs `SKIP: <reason>` and the run ends green.

| Workflow | Schedule (UTC = Asia/Dhaka minute) | Does |
|---|---|---|
| `proskills-intake.yml` | `14 * * * *` (hourly :14 Dhaka) | restore state, `scout.py --write`, `source_candidates.py --write`, `scout_file.py --apply` (25/run, 150/day, 3/repo/day, stop at 200 unreviewed), `review.py --apply --limit 50`, save state |
| `proskills-publish.yml` | `44 * * * *` (hourly :44 Dhaka) | `publish_run.py --apply --cap 100`: reconcile, merge_stuck_publish_pr, select review:pass issues, catalog_update, production build, website PR, squash-merge only when catalog-only + MERGEABLE/CLEAN + checks green, verify live, label `status:listed` + `groot:published`, close |
| `proskills-health.yml` | `1 3 * * *` (09:01 Dhaka) | read-only job summary (label counts, published today, open publish PRs, live catalog total, recent runs) |

Intake and publish share the concurrency group `proskills-operator-pipeline`, so they never overlap.
AI review is not in Actions (no model API keys): `review:needs-ai` issues wait for the box agent
(`ai_review.py prepare` / `apply`).

## State

* Derived from GitHub: candidate identity/sha (issue body block), verdicts (labels + review comment marker),
  published items (`<!-- proskills:publish v1 ... -->` marker in the website PR body), daily publish count
  (merged `operator/catalog-publish-*` PRs of the Dhaka day).
* `actions/cache`: pure caches (repo/scan/skill/http caches). Losing them only costs API calls.
* Branch `operator-state` (orphan, never merged; written by `actions_state.py` with the app token):
  `scout/cursor.json`, `scout/issue-index.json` (dedupe index + daily filing counters),
  `scout/review-state.json` (comment ids, not-found first sightings), `sources/cursor.json`,
  `sources/star-readings.json`.

Review/marker comments are edited in place. A comment the app cannot edit (for example one written by the box
account before cutover) is replaced by one fresh comment. The last marker comment is authoritative after that,
so there is never a third.

## One-time setup (repository owner)

1. GitHub App (the ProSkills app) **repository permissions**: Contents *Read and write*, Issues *Read and
   write*, Pull requests *Read and write*, Checks *Read*, Commit statuses *Read*, Metadata *Read*.
   No organization/account permissions, no webhooks needed.
2. Install the app on **ProSkillsMD/proskills** and **ProSkillsMD/website** (only these two).
3. In ProSkillsMD/proskills -> Settings -> Secrets and variables -> Actions:
   * secret `PROSKILLS_APP_ID` = the app's numeric App ID
   * secret `PROSKILLS_APP_PRIVATE_KEY` = the app's private key (.pem contents)
   * variable `PROSKILLS_ACTIONS_ENABLED` = `false` (set to `true` at cutover)
4. If `main` of ProSkillsMD/website has branch protection that requires reviews, allow the app to merge
   (or bypass), otherwise publish PRs will wait for `merge_stuck_publish_pr` / a human.

## Cutover

1. Run each workflow once via *Run workflow* with the variable still `false`: expect a green run with
   `SKIP: ... PROSKILLS_ACTIONS_ENABLED is not 'true'`.
2. Pause the box intake (:14) and publisher (:44) routines.
3. Set `PROSKILLS_ACTIONS_ENABLED=true`; dispatch `ProSkills intake` manually and check its summary, then
   `ProSkills publish`.
4. Rollback: set the variable to `false` (or open an issue labelled `operator-lock`) and resume the box routines.
