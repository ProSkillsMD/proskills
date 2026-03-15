# Low-Altitude Guardian

**Real-time emergency crisis response system for unmanned devices in the low-altitude economy**

## Overview

Low-Altitude Guardian is a comprehensive OpenClaw skill that automates emergency crisis response for UAVs, drones, eVTOLs, and autonomous devices operating in low-altitude economy scenarios (delivery, inspection, surveillance, etc.).

### Key Capabilities

- **Real-time Situation Awareness** — Captures structured emergency snapshots with device status, location, velocity, battery, environmental conditions, and threats
- **Crisis Classification (L1-L5)** — Automatically classifies emergency severity from Caution (L1) to Disaster (L5)
- **Loss Priority Decision-Making** — Enforces an unbreakable priority pyramid: P0 (Human Safety) > P1 (Public Safety) > P2 (Third-Party Property) > P3 (Device Safety) > P4 (Mission Completion)
- **Enterprise-Grade Analytics** — Collects incident data, builds knowledge bases, generates emergency plans, and provides fleet analytics
- **Self-Iterating Knowledge System** — Learns from historical incidents and continuously improves response strategies

### Architecture

The skill operates in two tiers:

**Device Side (Phases 1-7):**
1. Situation Awareness & Incident Recording
2. Crisis Classification & Severity Assessment
3. Solution Matching from Knowledge Base
4. Optimal Decision-Making
5. Action Execution
6. Post-Incident Review

**Enterprise Side (Phases 8-10):**
8. Data Collection & Aggregation
9. Knowledge Base Construction
10. Emergency Plan Generation & Fleet Analytics

## Installation

### Prerequisites

- Python 3.7+
- OpenClaw runtime

### Quick Start

```bash
# Install the skill
clawhub install low-altitude-guardian

# Or clone and use directly
git clone https://github.com/AAAlenwow/low-altitude-guardian.git
cd low-altitude-guardian
python3 -m pip install -r requirements.txt
```

### Basic Usage

```bash
# Run situation awareness snapshot
python3 scripts/situation_awareness.py --snapshot

# Classify crisis level
python3 scripts/crisis_engine.py --classify --input <snapshot_file>

# Execute decision logic
python3 scripts/decision_manager.py --match --crisis-type <type> --level <level>
```

## Core Concepts

### Loss Priority Pyramid (P0-P4)

All emergency decisions must strictly follow this irreversible priority order:

```
┌─────────────────┐
│  P0: 人员安全    │  Human Safety (absolute priority)
├─────────────────┤
│  P1: 公共安全    │  Public Safety
├─────────────────┤
│  P2: 第三方财产  │  Third-Party Property
├─────────────────┤
│  P3: 本机安全    │  Device Safety
├─────────────────┤
│  P4: 任务完成    │  Mission Completion
└─────────────────┘
```

**Example**: To protect human safety (P0), the system will autonomously destroy the device (sacrifice P3).

### Crisis Levels (L1-L5)

| Level | Name | Time Limit | Example |
|-------|------|-----------|---------|
| L5 | CRITICAL | < 3 sec | Device losing control, heading toward crowd |
| L4 | SEVERE | < 10 sec | Total power loss, imminent crash |
| L3 | MAJOR | < 30 sec | Single engine failure, total GPS loss |
| L2 | MINOR | < 2 min | Battery low threshold, sensor fault |
| L1 | CAUTION | < 5 min | Wind approaching limits, signal degradation |

## Status

⚠️ **ALPHA** — This skill is in concept validation stage. It is **NOT suitable for real-world flight control**. Use only as a crisis decision support and analysis tool.

- Sensor fusion, real-time threat detection, and environmental scanning are currently simulated/mocked
- Full operational capability pending further development
- Designed as a foundation for enterprise emergency response workflows

## Documentation

For detailed documentation in Chinese (中文), see the original [SKILL.md](https://raw.githubusercontent.com/AAAlenwow/low-altitude-guardian/master/SKILL.md) which covers:
- All 10 phases in detail
- Decision formulas and algorithms  
- Data structures and API reference
- Knowledge base format and examples

## Safety & Compliance

- **P0 Principle**: Human safety is non-negotiable in all decision-making
- **Compliance Recording**: All critical decisions are logged for regulatory review
- **Irreversible Operations**: Clear safeguards for decisions that cannot be undone

## Scoring (Gamora Review)

| Dimension | Score |
|-----------|-------|
| Functionality | 5/10 |
| Documentation | 9/10 |
| Security | 7/10 |
| Maintenance | 8/10 |
| Usefulness | 9/10 |
| Uniqueness | 9/10 |
| Code Quality | 8/10 |
| **Average** | **7.9/10** |

**Reviewed**: 2026-03-15 | **Verdict**: APPROVED

## License

See [LICENSE](https://github.com/AAAlenwow/low-altitude-guardian/blob/master/LICENSE)

## Repository

- **GitHub**: https://github.com/AAAlenwow/low-altitude-guardian
- **Author**: AAAlenwow
- **Version**: 0.2.0
