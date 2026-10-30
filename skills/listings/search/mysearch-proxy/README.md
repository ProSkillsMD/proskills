# MySearch-Proxy

**A unified search aggregator that intelligently routes queries across multiple providers.** MySearch-Proxy solves the pain of vendor lock-in by combining Tavily, Firecrawl, Exa, and xAI/Social search engines. Use the right provider for each search intent—with automatic fallback, parallel execution, and zero hardcoded secrets.

## Features

- **4+ Provider Aggregation**: Tavily, Firecrawl, Exa, xAI/Social search
- **Intent-Based Routing**: Automatically selects the best provider for your query type
- **Multi-Threaded Key Management**: Secure credential handling with thread-safe caching
- **Parallel Execution**: Run hybrid searches across multiple providers simultaneously
- **Edge Case Handling**: Smart detection for GitHub URLs, anti-bot mitigation
- **Zero Hardcoded Secrets**: All credentials via environment variables

## Installation

See the official **[SKILL.md](https://github.com/skernelx/MySearch-Proxy/blob/main/SKILL.md)** for setup instructions, provider configuration, and usage examples.

## Verification Scores

| Dimension | Score | Notes |
|-----------|-------|-------|
| **Functionality** | 9/10 | All core features working; multi-provider routing intelligent; fallback mechanisms solid |
| **Documentation** | 8/10 | Clear SKILL.md + bilingual docs EN+ZH; minor gaps in troubleshooting |
| **Security** | 9/10 | No hardcoded secrets; credentials via env vars; thread-safe key management; no vulnerabilities |
| **Maintenance** | 9/10 | Active repo, 52 commits, latest 2026-03-19, zero open issues, v0.1.7 |
| **Usefulness** | 9/10 | Solves real pain point; broadly applicable; improves upon single-provider alternatives |
| **Uniqueness** | 8/10 | First skill to aggregate 4+ providers with intent-based routing; differentiates from Vincent-Brave, Baidu Search |
| **Code Quality** | 8/10 | Modular Python 3.10+, type hints, dataclasses, 598 LOC tests, passes compilation |
| | **AVERAGE** | **8.7/10** | **✅ APPROVED** |

## Repository

- **GitHub**: [skernelx/MySearch-Proxy](https://github.com/skernelx/MySearch-Proxy)
- **Stars**: 58
- **License**: Apache 2.0
- **Language**: Python
- **Latest Release**: v0.1.7 (2026-03-19)

---

*Verified by Gamora on 2026-03-19. Aggregation-first design makes this the go-to skill for multi-provider search workflows.*
