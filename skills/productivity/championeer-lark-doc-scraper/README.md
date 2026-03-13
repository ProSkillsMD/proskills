# Lark Doc Scraper

Scrape Feishu/Lark docx documents into local Markdown folders with downloaded images and raw block JSON.

## When to Use

Use when asked to download, archive, crawl, export, mirror, or convert a Feishu/Lark docx link (公开或私有) into Markdown, especially for requests like:
- "把这篇飞书文档爬下来"
- "导出成 markdown"
- "下载文档和图片"
- "保存到本地目录"

## What It Does

Converts a Feishu/Lark docx URL into a local folder containing:
- Markdown export of the document
- Downloaded images in `images/` subdirectory
- Raw block data for debugging or reprocessing
- Metadata files for document information and block ordering

## Quick Start

1. Ensure `playwright` is available in the workspace
   - If missing: `npm install playwright`

2. Run the scraper script with your doc URL and output directory:
   ```bash
   node <skill-dir>/scripts/scrape_lark_doc_api.js "<feishu-docx-url>" <output-dir>
   ```

3. Example:
   ```bash
   node /path/to/skill/scripts/scrape_lark_doc_api.js \
     "https://bytedance.larkoffice.com/docx/MFK7dDFLFoVlOGxWCv5cTXKmnMh" \
     /path/to/output
   ```

## Output Structure

```
<output-dir>/
  <document-title>/
    <document-title>.md           # Markdown export
    <document-title>.meta.json    # Document metadata
    <document-title>.order.json   # Block order mapping
    <document-title>.raw.blocks.json  # Raw block data
    images/                       # Downloaded images
    files/                        # Downloaded files
```

## Authentication

- **Public documents**: Works out of the box
- **Private documents**: 
  - Use `STORAGE_STATE=...` environment variable for pre-authenticated browser session
  - Or run with `HEADLESS=false` to log in interactively first

## Notes

- Uses API-based extraction (`space/api/docx/pages/client_vars`) for stability and structure preservation
- Images are downloaded via Feishu preview endpoints and rewritten to local paths
- Markdown output is production-ready but some inline formatting may need refinement
- Supports both document URLs and document tokens

## Status: CONDITIONAL ⚠️

**Score: 7.3/10**

**Conditions for use:**
- The skill has solid code and correct logic
- Not fully installable/runnable without either (1) a `package.json` being added, or (2) clearer setup documentation
- The core functionality cannot be verified without access to a real Feishu/Lark document

**Recommended before production use:**
- Add or clarify `package.json` dependencies (especially `playwright` version pinning)
- Provide installation instructions for dependencies
- Test with actual Feishu/Lark documents if possible

## Source

📦 [championeer/lark-doc-scraper-openclaw-skill](https://github.com/championeer/lark-doc-scraper-openclaw-skill)

## License

MIT
