# Issue-based scout -> review -> publish flow

Deterministic Python for almost every step. AI is used only for `review:needs-ai` issues: at most 5 per run
and 20 per day, 8k input tokens each, and it can never override a deterministic critical finding.
All scripts use stdlib only. Default is a dry run; `--apply` writes. GitHub access goes through
`scout.GitHubClient` (retries, rate-limit backoff, REST core reserve).

| Step | Script | Writes (with --apply) |
|---|---|---|
| labels | `issue_labels.py` | creates missing labels only (never deletes/renames) |
| scout | `scout.py --write` (existing), `source_candidates.py --write` (adapters) | local JSON only |
| file | `scout_file.py` | new `[Candidate]` issues, machine blocks on legacy issues, labels |
| review | `review.py` | one review comment per issue, one verdict label, closes duplicate/not-found |
| AI | `ai_review.py prepare` / `apply --result` | AI section in the review comment, `review:pass`/`review:hold-ai` + `ai:reviewed` |
| publish | `publish_candidates.py` | local `issue-queue-YYYY-MM-DD.json`; `--mark-staged N --apply` adds `publish:staged` |

## Identity and machine block

The identity is `github:owner/repo` for a root SKILL.md, `github:owner/repo::subpath` for a skill in a subfolder,
and `clawhub:@owner/slug` for a skill that exists only on ClawHub. Each candidate issue carries exactly one
HTML-comment block (not visible when rendered):

```
<!-- proskills:candidate v1
psk-id: github:owner/repo::skills/foo
kind: "skill"                        # or "large-collection"
source_type: "github"                # or "clawhub"
repo: "https://github.com/owner/repo"
source_url: "https://github.com/owner/repo/tree/main/skills/foo"
skill_path: "skills/foo/SKILL.md"
default_branch: "main"
commit_sha: "..."
skill_tree_sha: "..."                # git tree sha of the skill folder (repo root tree for root skills);
                                     # ClawHub: "clawhub:<version>:<integrity16>"
license: {"spdx":"MIT","tier":"pass","label":"MIT","evidence":["repo_spdx:MIT"]}
metrics: {"stars":..,"forks":..,"pushed_at":..}   # ClawHub: {"downloads":..,"installs":..,"version":..}
sources: [{"source":"github_topics:agent-skills","url":"...","observed_at":"..."}]
discovered_at: "2026-09-26T08:00:00Z"
scout_version: "issue-flow/1"
-->
```

Values are JSON. `@` is written as `%40` in `psk-id` and as `\u0040` inside JSON, so no posted text contains an
@-handle. Third-party text (names, descriptions) goes through `issue_flow.safe_text`. That function rewrites `@` as `＠`
and `#123` as `＃123`, and it removes URLs and HTML. Nothing the flow posts can ping a user or cross-reference another repository.

## scout_file.py

Dedupe by identity, in this order:
1. The block in open and closed issue bodies (and the marker-comment fallback), mirrored in
   `operator/state/scout/issue-index.json`.
2. The issue number on a scout.py queue record.
3. The normalized repo URL, or the ClawHub page URL, of legacy issues. Open issues are read on every run.
   Closed issues are synced incrementally with `since=`.

Outcomes:
* **Open legacy match.** The block is appended to the issue body. If the token cannot edit issue bodies, one
  marker comment is posted instead. Labels added: `candidate`, `legacy:v0`, `scout:filed`. No new issue is opened.
* **Closed legacy match.** Never re-filed (`legacy_closed`).
* **Legacy issue already claimed by another identity of the same repo.** The sibling is deferred
  (`repo_claimed_by_issue`). The website currently allows one listing per repo. Use `--file-siblings` to override.
* **New candidate.** Opens `[Candidate] owner/repo[/subpath] - <name>` (ClawHub: `[Candidate] clawhub:owner/slug - <name>`)
  with a short summary table and the block. Labels: `candidate`, `source:<github|clawhub>`, `scout:filed`, plus
  `scout:large-collection` when the repo has more than 50 SKILL.md files. Such a repo gets one issue, with identity
  `github:owner/repo` and `kind: large-collection`.
* **New sha on a tracked identity.** The block is edited in place, and `review:*`, `reject:*` and `ai:*` labels are removed.

Filed statuses: `pass`, `license_review`, `large_collection`. Holds, missing SKILL.md, missing license,
catalog duplicates, transient and deferred records are never filed. Protected (#714, #3644, #4353, #5214, #5226,
#5403, #2028, #2029, #2030, #2850), critical (#2396, #4869), publisher-skip (#2833) and
`groot:published` / `status:listed` / `blocked:no-github-repo` / `blocked:security` / `curio:duplicate` issues
are never touched. A candidate that matches one of them is skipped.

Caps, all configurable:
* `--max-new 25` per run, 150 per Dhaka day, 3 new per repo per day.
* No new issues while more than 200 open candidate issues have no `review:*` label.
* `--max-legacy 25` legacy blocks and `--max-refresh 25` block refreshes per run.
* At least 3 s between issue creates (hard minimum), 1 s between other writes.
* Stops at REST core < `--core-reserve 1500`.
* Issue creation is never retried automatically.

## review.py

Scope: at most 50 issues per run. Picks candidate issues with no verdict label, whose block sha changed since
the last review, or with a pending not-found confirmation. Checks run in this order:
1. Source exists.
2. SKILL.md is at the recorded path.
3. Frontmatter has a name and a description.
4. License tier (`scout.license_tier`).
5. `static_scan` severity (skill folder files, text only).
6. Catalog duplicate by identity (live catalog; without a catalog the issue is skipped, never passed).
7. ClawHub moderation flags (`isSuspicious`, `isMalwareBlocked`, `isHiddenByMod`, scanner verdicts
   `suspicious`/`malicious`, critical platform findings).
8. Skill sha.

The first matching verdict wins:

| Condition | Verdict |
|---|---|
| repo/page 404 twice, >= 24 h apart | `review:reject` + `reject:not-found`, closed as not planned |
| already in catalog | `review:reject` + `reject:duplicate`, closed as not planned |
| no SKILL.md at path | `review:reject` + `reject:no-skill-md` (stays open) |
| critical static finding, unscannable ClawHub bundle scripts, ClawHub moderation flag | `review:hold-critical` |
| no license evidence | `review:reject` + `reject:no-license` (stays open) |
| more than 50 SKILL.md files | `review:large-collection` |
| license evidence without SPDX id | `review:license-review` |
| any warning (missing name/description, high-severity finding, archived repo) | `review:needs-ai` |
| otherwise | `review:pass` |

Each issue has exactly one comment, starting with
`<!-- proskills:review v1 psk-id=... sha=... verdict=... -->`. On re-review the comment is edited in place.
Nothing is written when verdict and sha are unchanged. Other comments are never posted. The first not-found
sighting writes nothing: it is only recorded in `operator/state/scout/review-state.json`.

## ai_review.py

```
python3 operator/scripts/ai_review.py prepare            # -> operator/state/artifacts/ai-review-request-<stamp>.json
# the calling agent answers each item's "prompt" with ONE JSON object:
# {"issue": N, "sha": "<as given>", "verdict": "pass"|"hold", "reasons": [...<=5],
#  "scores": {"functionality":n,"documentation":n,"security":n,"maintenance":n,"usefulness":n,"uniqueness":n,"code_quality":n}}
python3 operator/scripts/ai_review.py apply --result results.json --request <request file>          # dry run
python3 operator/scripts/ai_review.py apply --result results.json --request <request file> --apply
```

The weighted score uses the Gamora weights: functionality 1.5, documentation 1.0, security 1.5, maintenance 1.0,
usefulness 1.5, uniqueness 1.0, code_quality 1.0.
* Verdict `pass` and weighted score >= 6.0: `review:pass` + `ai:reviewed`.
* Anything else: `review:hold-ai` + `ai:reviewed`.

`apply` refuses the result when:
* the issue has a recorded deterministic critical finding,
* the sha changed,
* the issue no longer carries `review:needs-ai`, or
* the result does not validate strictly.

Budget: `prepare` reserves the calls it hands out (5 per run, 20 per Dhaka day) in `ai-budget.json`. Token
estimates are logged to `ai-log.jsonl`.

## publish_candidates.py

`python3 operator/scripts/publish_candidates.py [--limit 40]` writes
`operator/state/artifacts/issue-queue-YYYY-MM-DD.json`. It uses the same keys as `candidate-queue-*.json`
(`passed` in publish order, `eligible`, holds, `publisher_skip_batch_seed`), so the publisher can switch between
the two files.

Selection:
* open `candidate` issues with the `review:pass` label;
* the skill sha re-fetched now equals the sha in the review marker;
* not protected, critical or publisher-skip;
* no `blocked:*`, `review:hold*` or `flag:*` label, no published label, no `publish:staged` label;
* one skill per repo per batch;
* GitHub repos that already have a catalog listing are skipped.

ClawHub-only records have `source_type: clawhub`, `repo_url` set to the ClawHub page, `stars: null`, MIT-0
platform license evidence, and the public-page SKILL.md in `skill_md`. `catalog_update.plan_update` stages them
with `publish_lib.build_clawhub_skill_record`, which produces the same shape as the 40 existing ClawHub listings:
`external_ratings.clawhub_*`, `github_stars: 0`, `works_with: ["openclaw"]`, `source_type: "clawhub"`, `license: "MIT-0"`.

After the catalog PR is opened: `publish_candidates.py --mark-staged 123,456 --apply`.
After deploy and page verification the publisher labels and closes the issue as it does today
(`status:listed` + `groot:published`, closed as completed).

## Locks and windows

Every writer takes the private scout lock (`operator/state/scout/scout.lock`) and returns `LOCK_SKIP` when a
live routine lock exists in `operator/state/locks/`. Manual runs also refuse the routine windows (Dhaka :10-:26
and :40-:58). Routines pass `--ignore-routine-windows`.
