# skill-multi-publisher

One-command publish a Claude Code skill to multiple marketplaces: GitHub (npx skills), ClawHub, and 

## 安装

```bash
npx skills add dongsheng123132/skill-multi-publisher
```

# Skill Multi-Publisher

Publish a skill to multiple marketplaces with one command. Supports GitHub (npx skills add), ClawHub, and npm.

## When to use

When the user says:
- "Publish this skill"
- "发布这个skill到所有市场"
- "Push skill to GitHub and ClawHub"
- "Release this skill everywhere"

## Prerequisites

- `gh` CLI installed and logged in (`gh auth status`)
- `clawhub` CLI available and logged in (`clawhub whoami`)
- Skill directory with valid `SKILL.md` containing YAML frontmatter (name + description)

## Publish Flow

### Step 1: Validate

Check the skill directory:

```bash
# Verify SKILL.md has YAML frontmatter with name and description
head -10 SKILL.md
```

Required SKILL.md format:
```yaml
---
name: my-skill-name
version: 1.0.0
description: |
  What this skill does. At least 50 characters.
tags: ["tag1", "tag2"]
---
```

### Step 2: Auto-generate missing files

If missing, create:
- `LICENSE` (MIT, current year, git user.name)
- `README.md` (from SKILL.md content, with install instructions)

### Step 3: Publish to GitHub

```bash
cd <skill_dir>
git init  # if needed
git add -A
git commit -m "Release: <skill-name> v<version>"
gh repo create <user>/<skill-name> --public --description "<desc>" --source . --push
```

Verify: `npx skills add <user>/<skill-name>` should discover it.

### Step 4: Publish to ClawHub

```bash
clawhub publish <skill_dir> \
  --slug <skill-name> \
  --name "<Display Name>" \
  --version <version> \
  --tags "<comma-separated-tags>" \
  --changelog "<changelog text>"
```

### Step 5: Update existing

For already-published skills, detect existing repos/slugs and push updates:

```bash
# GitHub: commit + push
cd <skill_dir> && git add -A && git commit -m "Update: <changelog>" && git push

# ClawHub: re-publish with new version
clawhub publish <skill_dir> --slug <name> --version <new_version> --changelog "<text>"
```

### Step 6: Report

After publishing, show summary:

```
Published <skill-name> v<version>:
  GitHub:  https://github.com/<user>/<skill-name>
  Install: npx skills add <user>/<skill-name>
  ClawHub: clawhub install <skill-name>
```

## Publishing Checklist

Before publishing, verify:
- [ ] SKILL.md has YAML frontmatter (name, version, description)
- [ ] description is at least 50 characters
- [ ] No secrets (.env, credentials) in the directory
- [ ] README.md exists with install instructions
- [ ] LICENSE exists
- [ ] All scripts are executable (chmod +x)

## Platform Reference

| Platform | CLI | Install Command | Registry |
|----------|-----|-----------------|----------|
| GitHub / npx skills | `gh` | `npx skills add user/repo` | skills.sh |
| ClawHub | `clawhub` | `clawhub install slug` | clawhub.com |
| npm | `npm` | `npm install -g name` | npmjs.com |

## Example prompts
- "Publish this skill to GitHub and ClawHub"
- "发布skill到所有平台"
- "Update my skill on all marketplaces"
- "Release v1.1.0 of this skill"

## License

MIT

## 📱 关注作者

如果这个项目对你有帮助，欢迎关注我获取更多技术分享：

- **X (Twitter)**: [@vista8](https://x.com/vista8)
- **微信公众号「向阳乔木推荐看」**:

<p align="center">
  <img src="https://github.com/joeseesun/terminal-boost/raw/main/assets/wechat-qr.jpg?raw=true" alt="向阳乔木推荐看公众号二维码" width="300">
</p>
