# AI Drama Review

> AI-driven drama/film compliance review and analysis skill

**Stage:** Beta  
**License:** MIT  
**Author:** [@AAAlenwow](https://github.com/AAAlenwow)

## Overview

AI Drama Review is a comprehensive compliance checking skill for AI-generated short drama and film scripts. It identifies and reports on three critical compliance areas:

1. **Copyright Infringement Detection** — Uses triple similarity algorithm verification (n-gram Jaccard coefficient, normalized edit distance, TF-IDF cosine similarity) to detect unauthorized copying or adaptation of existing works. Composite scores > 0.7 are flagged for AI semantic confirmation to eliminate false positives.

2. **Age Rating Compliance Scanning** — Two-layer architecture combining local keyword database scanning (violence, adult content, horror, profanity, substance abuse) with AI context-aware analysis to eliminate false positives from negation, literary rhetoric, and historical references. Outputs standardized ratings: All Ages / 12+ / 18+ / Non-Compliant.

3. **Novel Adaptation Detection** — Uses Needleman-Wunsch dynamic programming algorithm variant to align source material with adapted versions. Detects character deviations (personality, relationships, fate) and scores adaptation fidelity: 0-30 Faithful / 30-60 Reasonable / 60-100 Major Adaptation.

## Key Features

- **Two-Layer Architecture**: Fast local keyword scanning + AI semantic analysis for accuracy
- **Bilingual Support**: Full documentation in English and Chinese
- **Graceful Degradation**: Works in local-only mode without API keys; enables hybrid mode when credentials available
- **Structured Compliance Reports**: Risk assessment (Low/Medium/High/Critical), precise violation tagging, remediation recommendations
- **Modular Design**: Dedicated Python scripts for each analysis phase (copyright, rating, adaptation, reporting)

## Installation

### Via ClawHub

```bash
clawhub install ai-drama-review
```

### Via Git Clone

```bash
git clone https://github.com/AAAlenwow/ai-drama-review.git
cd ai-drama-review
```

## Configuration

### Optional: Enable AI-Powered Deep Analysis

Set environment variables for hybrid mode (recommended for higher accuracy):

```bash
# OpenAI (recommended)
export OPENAI_API_KEY="sk-..."

# Or Anthropic
export ANTHROPIC_API_KEY="sk-ant-..."
```

Without API keys, the skill automatically falls back to **local-only mode** using keyword matching and algorithmic text analysis.

## Quick Start

### Full Compliance Review

```bash
python3 scripts/review_orchestrator.py \
  --input script.txt \
  --reference-dir ./references/ \
  --original original_novel.txt \
  --target-rating 12+ \
  --checks copyright rating adaptation
```

### Individual Checks

```bash
# Copyright detection
python3 scripts/text_similarity.py \
  --input script.txt \
  --reference-dir refs/

# Age rating scan
python3 scripts/age_rating_scanner.py \
  --input script.txt \
  --target-rating 12+

# Adaptation detection
python3 scripts/adaptation_detector.py \
  --original novel.txt \
  --adapted script.txt

# Generate report
python3 scripts/report_generator.py \
  --results results.json \
  --format markdown
```

## Use Cases

- **Content creators** reviewing AI-generated short drama scripts for compliance before publishing
- **Production studios** auditing adapted scripts for source material fidelity and legal risk
- **Compliance teams** implementing automated pre-screening workflows for user-generated content
- **Rights holders** monitoring for unauthorized adaptations of their source material

## Project Structure

```
ai-drama-review/
├── SKILL.md                      # Skill definition
├── scripts/
│   ├── review_orchestrator.py    # Main orchestrator
│   ├── text_similarity.py        # Copyright detection
│   ├── age_rating_scanner.py     # Age rating scanning
│   ├── adaptation_detector.py    # Adaptation analysis
│   ├── content_analyzer.py       # Content analysis
│   ├── report_generator.py       # Report generation
│   ├── credential_manager.py     # Credential handling
│   └── env_detect.py             # Environment detection
├── assets/
│   ├── keyword_databases/        # Classification keyword libraries
│   ├── rating_rules/             # Rating rules
│   └── report_templates/         # Report templates
├── references/                   # Reference documentation
└── tests/                        # Test suite
```

## Dependencies

- Python 3.8+
- jieba (optional, improves Chinese tokenization accuracy)
- OpenAI or Anthropic API key (optional, enables hybrid mode)

## Important Notes

⚠️ **BETA STAGE**: This skill is in active development. Compliance check results are **for reference only** and do **not constitute legal advice**. Users should consult with professional legal counsel before making final compliance decisions. Detection results may contain false positives or false negatives; manual review of high-risk flagged content is recommended.

## License

MIT

## Support

For issues, questions, or contributions, visit: https://github.com/AAAlenwow/ai-drama-review
