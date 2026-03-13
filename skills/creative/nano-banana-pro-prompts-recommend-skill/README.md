# Nano Banana Pro Prompts Recommendation

**Score:** 9.0/10 | **Status:** ✅ Approved  
**Category:** Creative | **Author:** YouMind-OpenLab

## Overview

A robust system for recommending image generation prompts from a curated library of 10,000+ Nano Banana Pro prompts. The skill provides dynamic category loading, efficient searching, critical image display mechanism, custom prompt generation, and template remixing.

## What It Does

Recommend suitable prompts from 10,000+ Nano Banana Pro image generation prompts based on user needs. Optimized for Nano Banana Pro (Gemini), but prompts also work with:
- Nano Banana 2
- Seedream 5.0
- GPT Image 1.5
- Midjourney
- DALL-E
- Flux
- Stable Diffusion
- And any text-to-image AI model

## Use Cases

- **Generate images with AI** — any model (Nano Banana Pro, Gemini, GPT Image, Seedream, etc.)
- **Find proven AI image generation prompts** and prompt templates
- **Get prompt recommendations** for specific use cases (portraits, products, social media, posters, etc.)
- **Create illustrations** for articles, videos, podcasts, or marketing content
- **Browse a curated prompt library** with sample images
- **Translate and understand** prompt techniques

## Key Features

✅ **10,000+ Community Prompts** — Curated from the open community by YouMind.com  
✅ **Dynamic Category Loading** — Categories update daily as new prompts are added  
✅ **Efficient Searching** — grep-based search for fast, token-efficient results  
✅ **Image Display** — Every prompt includes its sample image for quick visualization  
✅ **Custom Prompt Generation** — Generate AI-based prompts when no template matches  
✅ **Template Remixing** — Personalize prompts based on user content and preferences  
✅ **Multi-Platform** — Works with any text-to-image model

## How to Use

### Quick Start

1. User describes what image they need
2. Skill searches the curated library
3. Returns 3 best-matching prompts **with sample images**
4. User selects one and optionally customizes it

### Two Modes

**Direct Generation:**  
User describes image → Skill recommends templates → Done

**Content Illustration:**  
User provides article/script → Skill recommends prompts → User selects → Skill customizes based on content

## Installation & Setup

After installing this skill, the prompt library is automatically downloaded via `postinstall`. No credentials needed.

**Keep references up to date** (GitHub syncs community prompts twice daily):
```bash
pnpm run sync
# or
node scripts/setup.js --force
```

Check reference freshness:
```bash
node scripts/setup.js --check
```

## Platforms

- OpenClaw
- Claude Code
- Cursor
- Codex
- Gemini CLI

## About the Author

**YouMind** — Curators of the Nano Banana Pro prompt library  
🔗 [YouMind.com](https://youmind.com/nano-banana-pro-prompts)  
📖 [GitHub Repository](https://github.com/YouMind-OpenLab/nano-banana-pro-prompts-recommend-skill)

---

**Prompts curated from the open community by [YouMind.com](https://youmind.com) ❤️**

## Approval Details

- **Score:** 9.0/10
- **Verdict:** APPROVE
- **Reviewed:** 2026-03-13
- **Issue:** #1070
- **Assessment Summary:** The skill provides a robust system for recommending image generation prompts, including dynamic category loading, efficient searching, and a critical image display mechanism. It also supports custom prompt generation and template remixing, adding significant value. The core functionality appears well-defined and comprehensive for its stated purpose.
