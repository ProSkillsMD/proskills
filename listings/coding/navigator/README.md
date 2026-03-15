# Navigator — Context Engineering for Claude Code

## Overview

**Navigator** is a Claude Code plugin that implements intelligent context engineering to keep AI-assisted development sessions productive and focused.

Instead of loading all documentation upfront (and hitting context limits), Navigator loads exactly what you need, when you need it—preserving 94% of your context window for actual work.

### Key Features

- **Context Optimization**: Reduce token usage by 92% while maintaining 20+ exchange sessions
- **Theory of Mind**: AI learns your preferences and coding style across sessions
- **Knowledge Graph**: Persistent project memory with intelligent context recall
- **Loop Mode**: Structured, autonomous task execution with automatic completion detection
- **Session Persistence**: Experiential memory prevents hallucinations and context drift

## Problem Statement

Standard Claude Code workflows suffer from context exhaustion:

```
Exchange 5:  Claude forgets recent changes
Exchange 7:  Hallucinations begin
Exchange 8:  "Context limit reached" → Restart cycle
```

Navigator breaks this loop by intelligently managing context:

| Metric | Without Navigator | With Navigator |
|--------|-------------------|----------------|
| Tokens loaded | 150,000 | 12,000 |
| Session length | 5-7 exchanges | 20+ exchanges |
| Context usage (end) | 95% (exhausted) | 35% (comfortable) |
| Token savings | — | **92%** |

## How It Works

### 1. Smart Loading
Navigator analyzes your current task and loads only relevant documentation and codebase context, deferring non-urgent information.

### 2. Adaptive Context Pruning
Automatically identifies and removes stale or irrelevant context as your session progresses.

### 3. Profile-Based Personalization (v5.0+)
- Learns your coding style preferences
- Remembers your project conventions
- Auto-applies customizations to future sessions

### 4. Knowledge Graph (v6.0+)
- Unified search across tasks, SOPs, system docs, and patterns
- Experiential memory: decisions, pitfalls, lessons learned
- Automatic relevance detection on session start

### 5. Loop Mode (v5.1+)
- Run complex workflows autonomously: `"Add user authentication until complete"`
- Dual-condition exit: Heuristics + explicit completion signal
- Stagnation detection prevents infinite loops

## Installation

### Prerequisites
- Claude Code (latest version)
- 2.4+ GB disk space for skills directory
- Basic understanding of OpenClaw or Claude Code workflow

### Quick Start

```bash
# Clone the repository
git clone https://github.com/alekspetrov/navigator.git

# Follow README in the cloned directory for setup
cd navigator
cat README.md
```

### Integration with OpenClaw

```bash
# Within OpenClaw environment
openclaw skill install alekspetrov/navigator

# Or manually add to skills directory
cp -r navigator ~/.openclaw/skills/navigator
```

## Skills Included

Navigator comes with **27+ production-ready skills** covering:

- **Navigation**: `nav-init`, `nav-start`, `nav-install-multi-claude`
- **Diagnostics**: `nav-diagnose`, `nav-marker`, `nav-graph`
- **Workflow**: `nav-task`, `nav-loop`, `nav-task-mode`
- **Code Generation**: Frontend/backend components, database migrations, tests
- **DevOps**: Release management, updates, upgrades
- **Documentation**: SOP generation, feature documentation

## Configuration

Navigator exposes key settings via environment variables and config files:

```json
{
  "context_limit": 120000,
  "token_threshold": 35000,
  "profile_enabled": true,
  "knowledge_graph_enabled": true,
  "loop_mode_exit_heuristics": true
}
```

See the project README for detailed configuration options.

## Support & Documentation

- **Repository**: https://github.com/alekspetrov/navigator
- **Issues**: https://github.com/alekspetrov/navigator/issues
- **Release Notes**: https://github.com/alekspetrov/navigator/releases
- **Security**: https://github.com/alekspetrov/navigator/blob/main/SECURITY.md

## Review & Verification

- **Score**: 8.7/10 (Gamora Review, 2026-03-15)
- **Verdict**: ✅ APPROVED
- **Reviewer**: Gamora Deep Review System
- **GitHub Issue**: https://github.com/ProSkillsMD/proskills/issues/687

### Review Dimensions

| Dimension | Score | Notes |
|-----------|-------|-------|
| Functionality | 8/10 | Measurable improvements to Claude Code context management |
| Documentation | 9/10 | Excellent README with clear problem/solution, features, metrics |
| Security | 8/10 | Minimal external dependencies; secure context isolation |
| Maintenance | 9/10 | Active development with versioning (v5.0, v5.1, v6.0) |
| Usefulness | 10/10 | Addresses critical AI agent development bottleneck |
| Uniqueness | 9/10 | Novel context engineering approach; distinctive features |
| Code Quality | 8/10 | Well-structured, OpenTelemetry metrics, efficiency reports |

**Average**: 8.7/10

## License & Attribution

Navigator is maintained by **Aleks Petrov** (@alekspetrov).

**Note**: The repository lacks an explicit LICENSE file. It is recommended that users clarify licensing intent with the author. Approval assumes permissive licensing (MIT recommended).

## Conditional Notes

⚠️ **Important**: Ensure you contact the author or check for a LICENSE file update before deploying to production environments that require explicit license compliance.

---

**ProSkills.md Catalog Entry** | Published: 2026-03-15 | Status: Verified & Approved
