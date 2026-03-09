---
name: skill-multi-publisher
version: 2.0.0
description: |
  One-command publish a Claude Code skill to ALL major marketplaces: GitHub (npx skills), ClawHub, and community marketplaces (composiohq/awesome-claude-skills, anthropics/skills, daymade/claude-code-skills, obra/superpowers-marketplace). Validates SKILL.md, auto-generates missing files, creates repos, publishes, and submits PRs to community directories.
tags: ["skill", "publish", "github", "clawhub", "marketplace", "automation"]
metadata: {"openclaw":{"emoji":"🚀"}}
---

# Skill Multi-Publisher

Publish a skill to ALL major marketplaces with one command.

## When to use

When the user says:
- "Publish this skill" / "发布这个skill"
- "Push skill to all marketplaces" / "发布到所有平台"
- "Release this skill everywhere"
- "Submit to awesome-claude-skills"

## Prerequisites

- `gh` CLI installed and logged in (`gh auth status`)
- `clawhub` CLI available and logged in (`clawhub whoami`)
- Skill directory with valid `SKILL.md` containing YAML frontmatter (name + description)

## All Supported Platforms

| Platform | Stars | Method | Install Command |
|----------|-------|--------|-----------------|
| GitHub (npx skills) | - | `gh repo create` + push | `npx skills add user/repo` |
| ClawHub | - | `clawhub publish` | `clawhub install slug` |
| composiohq/awesome-claude-skills | 41K+ | Fork + PR | `/plugin install` |
| anthropics/skills | 86K+ | Fork + PR | `/plugin install` |
| daymade/claude-code-skills | 600+ | Fork + PR | `/plugin install` |
| obra/superpowers-marketplace | 595 | Fork + PR | `/plugin install` |
| anthropics/claude-plugins-official | 9.3K | Form submission | `/plugin install` |
| trailofbits/skills-curated | 252 | Fork + PR | `/plugin install` |
| MadAppGang/claude-code | 242 | Fork + PR | `/plugin install` |

## Publish Flow

### Phase 1: Direct Publish (automated)

#### Step 1: Validate

```bash
head -10 SKILL.md
```

Required format:
```yaml
---
name: my-skill-name
version: 1.0.0
description: |
  What this skill does. At least 50 characters.
tags: ["tag1", "tag2"]
---
```

#### Step 2: Auto-generate missing files

If missing, create:
- `LICENSE` (MIT, current year, git user.name)
- `README.md` (from SKILL.md content, bilingual CN/EN if user is Chinese-speaking)

#### Step 3: Publish to GitHub

```bash
cd <skill_dir>
git init
git add -A
git commit -m "Release: <skill-name> v<version>"
gh repo create <user>/<skill-name> --public --description "<desc>" --source . --push
```

#### Step 4: Publish to ClawHub

```bash
clawhub publish <skill_dir> \
  --slug <skill-name> \
  --name "<Display Name>" \
  --version <version> \
  --tags "<comma-separated-tags>" \
  --changelog "<changelog text>"
```

### Phase 2: Community Marketplaces (PR submission)

#### Step 5: Submit to composiohq/awesome-claude-skills (41K stars)

This is the largest skill directory. Submit via PR:

```bash
# Fork the repo
gh repo fork composiohq/awesome-claude-skills --clone=false

# Clone your fork
gh repo clone <user>/awesome-claude-skills /tmp/awesome-claude-skills
cd /tmp/awesome-claude-skills

# Create branch
git checkout -b add-<skill-name>

# Copy skill folder (only SKILL.md needed)
mkdir <skill-name>
cp <skill_dir>/SKILL.md <skill-name>/SKILL.md

# Commit and push
git add -A
git commit -m "Add <skill-name>: <short description>"
git push origin add-<skill-name>

# Create PR
gh pr create \
  --repo composiohq/awesome-claude-skills \
  --title "Add <skill-name>" \
  --body "$(cat <<'EOF'
## New Skill: <skill-name>

<description>

### What it does
<bullet points>

### Install
- npx skills add <user>/<skill-name>
- clawhub install <skill-name>

### Tested on
- Claude Code CLI
- OpenClaw
EOF
)"
```

#### Step 6: Submit to anthropics/skills (86K stars)

Official Anthropic skill repo:

```bash
gh repo fork anthropics/skills --clone=false
gh repo clone <user>/skills /tmp/anthropics-skills
cd /tmp/anthropics-skills

git checkout -b add-<skill-name>
mkdir -p skills/<skill-name>
cp <skill_dir>/SKILL.md skills/<skill-name>/SKILL.md
# Copy scripts if any
cp -r <skill_dir>/tools skills/<skill-name>/tools 2>/dev/null || true
cp -r <skill_dir>/scripts skills/<skill-name>/scripts 2>/dev/null || true

git add -A
git commit -m "Add <skill-name> skill"
git push origin add-<skill-name>

gh pr create \
  --repo anthropics/skills \
  --title "Add <skill-name> skill" \
  --body "$(cat <<'EOF'
## Summary
<description>

## Skill Structure
- SKILL.md with frontmatter (name, description, tags)
- tools/ or scripts/ directory

## Testing
Tested in Claude Code CLI.
EOF
)"
```

#### Step 7: Submit to daymade/claude-code-skills (623 stars, Chinese community)

```bash
gh repo fork daymade/claude-code-skills --clone=false
gh repo clone <user>/claude-code-skills /tmp/daymade-skills
cd /tmp/daymade-skills

git checkout -b add-<skill-name>
mkdir <skill-name>
cp <skill_dir>/SKILL.md <skill-name>/SKILL.md
cp -r <skill_dir>/scripts <skill-name>/scripts 2>/dev/null || true
cp -r <skill_dir>/tools <skill-name>/tools 2>/dev/null || true
cp -r <skill_dir>/references <skill-name>/references 2>/dev/null || true

git add -A
git commit -m "Add <skill-name>"
git push origin add-<skill-name>

gh pr create \
  --repo daymade/claude-code-skills \
  --title "Add <skill-name>" \
  --body "$(cat <<'EOF'
## New Skill: <skill-name>

<description>

### Structure
- SKILL.md (with YAML frontmatter)
- tools/ or scripts/

### Also available at
- npx skills add <user>/<skill-name>
- clawhub install <skill-name>
EOF
)"
```

#### Step 8: Submit to obra/superpowers-marketplace (595 stars)

```bash
gh repo fork obra/superpowers-marketplace --clone=false
gh repo clone <user>/superpowers-marketplace /tmp/superpowers
cd /tmp/superpowers

git checkout -b add-<skill-name>
mkdir -p plugins/<skill-name>/skills/<skill-name>
cp <skill_dir>/SKILL.md plugins/<skill-name>/skills/<skill-name>/SKILL.md

# Create plugin.json
cat > plugins/<skill-name>/.claude-plugin/plugin.json <<PJSON
{
  "name": "<skill-name>",
  "description": "<description>",
  "version": "<version>"
}
PJSON

git add -A
git commit -m "Add <skill-name> plugin"
git push origin add-<skill-name>

gh pr create \
  --repo obra/superpowers-marketplace \
  --title "Add <skill-name>" \
  --body "<description>"
```

#### Step 9 (Optional): Submit to anthropics/claude-plugins-official

For high-quality plugins only. Submit via official form:
- URL: https://clau.de/plugin-directory-submission
- Requires review by Anthropic team
- Best for mature, well-tested plugins

### Phase 3: Report

After all submissions, show summary:

```
Published <skill-name> v<version>:

Direct publish:
  ✅ GitHub:   https://github.com/<user>/<skill-name>
  ✅ ClawHub:  clawhub install <skill-name>
  ✅ Install:  npx skills add <user>/<skill-name>

PR submitted:
  🔄 composiohq/awesome-claude-skills (41K stars) - PR #xxx
  🔄 anthropics/skills (86K stars) - PR #xxx
  🔄 daymade/claude-code-skills (623 stars) - PR #xxx
  🔄 obra/superpowers-marketplace (595 stars) - PR #xxx

Manual:
  📋 anthropics/claude-plugins-official - submit at clau.de/plugin-directory-submission
```

## Update Flow

For already-published skills:

```bash
# GitHub: commit + push
cd <skill_dir> && git add -A && git commit -m "Update: <changelog>" && git push

# ClawHub: re-publish with bumped version
clawhub publish <skill_dir> --slug <name> --version <new_version> --changelog "<text>"

# Community PRs: update existing PR or create new one
```

## Publishing Checklist

- [ ] SKILL.md has YAML frontmatter (name, version, description)
- [ ] description is at least 50 characters
- [ ] No secrets (.env, credentials) in the directory
- [ ] README.md exists with install instructions
- [ ] LICENSE exists
- [ ] All scripts are executable (chmod +x)

## Example prompts
- "Publish this skill to all platforms"
- "发布skill到所有市场"
- "Submit to awesome-claude-skills"
- "Release v2.0.0 everywhere"
