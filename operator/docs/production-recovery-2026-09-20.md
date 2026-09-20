# ProSkills low-credit production recovery

Owner goal: resume the existing Grok ProSkills Operator, prioritize popular skills and grow
toward 100 verified new listings/day. This is not a mandate to publish weak candidates.

## Current evidence

- Live catalog: 595 skills, generated September 7.
- Starting open issues: 3,182; title/body-exact, unreviewed duplicate candidates: 866.
- Curio server is retired. Do not restore old SSH jobs or agents.
- GitHub App access works; use installation tokens, not personal PATs for unattended jobs.
- Existing discovery/catalog_update scripts are stubs. Six Grok routines are not verified
  from this environment; this file does not enable them.
- Validation under a hidden host directory was skipping file limits; repair has regression tests.

## Lowest-credit operating policy

1. Python fetches metadata, deduplicates, checks URLs, validates schema/license/source,
   records pinned source versions and builds the queue. No model calls for those steps.
2. Cache source scans by commit/content digest; unchanged items are not re-reviewed.
3. Reuse adequate source descriptions with attribution. Use at most one bounded Grok
   judgment call for an otherwise eligible candidate needing synthesis/review. Ambiguous
   or unsafe items stay on hold; do not buy repeated attempts to force an approval.
4. Daily goal/cap: 100 distinct newly published skills, not 100 repositories or updates.
   Start with five verified end-to-end canaries before increasing to 25 then 100/day.
5. At most 100 ordinary AI review calls/day; independent escalations stay held pending
   explicit resolution. Log actual credits when the host supplies them; never call missing
   usage zero or promise a dollar cap without billing evidence.
6. Batch catalog PRs after validation. Preserve existing IDs, slugs, canonical URLs and
   redirects. Verify deployed listing, download/source link, sitemap and catalog membership
   before closing a submission as published. No invented scores or security endorsements.
7. Stop on failed deployment, conflicting source identity, missing license or critical scan
   finding; pause after three consecutive infrastructure failures. No automatic bypass.

## Popularity and coverage

- GitHub stars and forks prioritize established popularity; pushedAt indicates activity,
  not quality. rank_popular.py prepares a review shortlist from the actionable backlog.
- Add daily GitHub top-star/newly-active searches and ClawHub download/trending discovery
  in the Grok workspace. Record source, timestamp and metric; dedupe before AI.
- Star/download growth needs at least two dated observations; do not label a static star
  count viral growth. Maintain separate evergreen and rising queues with diversity limits.
- Treat monorepo SKILL.md paths as distinct identities. Do not collapse different skills
  merely because they share a repository; verify tree/ref handling before publication.
- Reconcile submitted and discovered candidates against the live catalog each run.
- Coverage is best-effort across named sources, not a claim to find every popular skill.

## Exact-duplicate recovery

The finite cleanup plan is frozen; it only closes unchanged, identical title/body submissions
with no discussion, assignment, milestone or stage labels. Keeps oldest open canonical,
protects supervised holds, saves before-state and writes no comments. Cap 100 per batch,
space batches by 30 minutes; stop on API failure or exhaustion. This is temporary recovery,
not the Grok publishing schedule. No reviewed/ambiguous items are bulk-closed.

## Required host handoff

Connect the existing ProSkills Operator workspace or provide its Bot access route. Inspect
its actual six paused routines, current state DB and source checkout. Reuse existing state;
do not create duplicate routines. Implement/verify discovery-to-website publishing beyond
stubs, run the five canaries, then enable only the bounded Python-first production flow.
The current user authorization covers recovery and routine enablement within these limits;
account-permission changes and credential collection remain outside this plan.
