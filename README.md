# Claude Chat Scraper & Extractor

Three scripts for extracting structured data from Claude conversations —
thinking blocks, artifacts, code, tool calls, and plain text.

---

## Scripts

| File | What it does |
|------|-------------|
| `claude_scraper.py` | Browser scraper — scrapes the claude.ai UI with Playwright |
| `export_cookies.py` | Helper to export your login session cookies |
| `conversation_parser.py` | Parses the JSON output into readable views / Markdown |
| `api_extractor.py` | **Cleanest approach** — uses the Anthropic SDK directly |

---

## Approach A — API extractor (recommended)

No browser needed. Uses your API key to call Claude and extracts every block.

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python api_extractor.py
```

Extracts automatically:
- `text_blocks` — plain text paragraphs
- `thinking_blocks` — extended thinking content (if enabled)
- `tool_use_blocks` — tool calls with name + input
- `artifacts` — code fences parsed from the text

Enable thinking:
```python
extractor = AnthropicExtractor(
    model="claude-opus-4-5",
    enable_thinking=True,
    budget_tokens=8000,
)
r = extractor.extract("Your hard question here")
print(r.to_json())
```

---

## Approach B — Browser scraper (existing chats)

Use this to scrape an existing chat URL from the claude.ai website.

### 1. Install dependencies
```bash
pip install playwright beautifulsoup4 pygments
playwright install chromium
```

### 2. Export your login cookies
```bash
python export_cookies.py
# A browser window opens — log in to claude.ai — press ENTER
# Saves: cookies.json
```

### 3. Scrape a chat URL
```bash
python claude_scraper.py "https://claude.ai/chat/<your-chat-id>" output.json cookies.json
```

### 4. View / parse the results
```bash
# Full summary
python conversation_parser.py output.json

# Only thinking blocks
python conversation_parser.py output.json --thinking

# Only artifacts / code
python conversation_parser.py output.json --artifacts

# Export to Markdown
python conversation_parser.py output.json --export-md chat.md
```

---

## Output JSON structure

```json
{
  "url": "https://claude.ai/chat/...",
  "title": "Chat title",
  "messages": [
    {
      "role": "user",
      "index": 0,
      "text": "Your message",
      "thinking_blocks": [],
      "artifacts": [],
      "tool_results": []
    },
    {
      "role": "assistant",
      "index": 1,
      "text": "Claude's reply",
      "thinking_blocks": [
        { "index": 0, "content": "Let me think step by step…" }
      ],
      "artifacts": [
        {
          "index": 0,
          "title": "Code block 1",
          "language": "python",
          "artifact_type": "code",
          "content": "def hello(): ..."
        }
      ],
      "tool_results": []
    }
  ]
}
```

---

## Notes

- The browser scraper uses CSS selectors that match claude.ai's current DOM.
  Anthropic may change the DOM, so selectors in `claude_scraper.py → SELECTORS`
  may need updating.
- Extended thinking is only available on certain Claude models and plans.
- Use responsibly and in line with Anthropic's Terms of Service.
