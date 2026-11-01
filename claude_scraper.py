"""
claude_scraper.py
-----------------
Playwright-based scraper for claude.ai chat windows.
Extracts: thinking blocks, artifacts, regular text, tool results.

Requirements:
    pip install playwright beautifulsoup4
    playwright install chromium
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, Browser


# ─── Data models ──────────────────────────────────────────────────────────────

@dataclass
class ThinkingBlock:
    index: int
    content: str


@dataclass
class Artifact:
    index: int
    title: str
    language: Optional[str]        # e.g. "python", "html", "jsx"
    artifact_type: Optional[str]   # e.g. "code", "html", "react"
    content: str


@dataclass
class ToolResult:
    tool_name: str
    content: str


@dataclass
class Message:
    role: str                              # "user" | "assistant"
    index: int
    text: str                              # plain text portion
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    raw_html: str = ""


@dataclass
class Conversation:
    url: str
    title: str
    messages: list[Message] = field(default_factory=list)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent, ensure_ascii=False)


# ─── Scraper ──────────────────────────────────────────────────────────────────

class ClaudeScraper:
    """
    Scrapes a claude.ai conversation.

    Usage:
        scraper = ClaudeScraper(cookies_file="cookies.json")
        conversation = await scraper.scrape("https://claude.ai/chat/<id>")
        print(conversation.to_json())
    """

    # CSS selectors — update if claude.ai changes its DOM
    SELECTORS = {
        # Each turn in the conversation
        "turn": '[data-testid="human-turn"], [data-testid="ai-turn"]',
        # Expandable "thinking" section
        "thinking_toggle": 'button[aria-label*="thinking"], button[aria-label*="Thinking"]',
        "thinking_content": '.font-tiempos-text, [class*="thinking"], pre.whitespace-pre-wrap',
        # Artifact panel / code blocks inside the turn
        "artifact_block": '[data-testid="artifact-block"], .artifact-block',
        "code_block": "pre code",
        # Artifact title shown in the header
        "artifact_title": '[data-testid="artifact-title"], .artifact-title',
        # Plain prose paragraphs
        "prose": "p, li, h1, h2, h3",
    }

    def __init__(
        self,
        cookies_file: Optional[str] = None,
        headless: bool = True,
        slow_mo: int = 200,
        timeout: int = 30_000,
    ):
        self.cookies_file = cookies_file
        self.headless = headless
        self.slow_mo = slow_mo
        self.timeout = timeout

    # ── Public API ──────────────────────────────────────────────────────────

    async def scrape(self, url: str) -> Conversation:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                slow_mo=self.slow_mo,
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )

            if self.cookies_file:
                await self._load_cookies(context)

            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=self.timeout)
            await self._wait_for_content(page)

            title = await self._get_title(page)
            messages = await self._extract_all_messages(page)

            await browser.close()

        return Conversation(url=url, title=title, messages=messages)

    # ── Internals ───────────────────────────────────────────────────────────

    async def _load_cookies(self, context):
        path = Path(self.cookies_file)
        if path.exists():
            cookies = json.loads(path.read_text())
            await context.add_cookies(cookies)
        else:
            print(f"[warn] Cookie file not found: {self.cookies_file}")

    async def _wait_for_content(self, page: Page):
        """Wait until at least one conversation turn is rendered."""
        try:
            await page.wait_for_selector(
                '[data-testid="human-turn"], [data-testid="ai-turn"]',
                timeout=self.timeout,
            )
        except Exception:
            # Might still be useful even if selector isn't found
            await page.wait_for_timeout(3000)

    async def _get_title(self, page: Page) -> str:
        try:
            return await page.title()
        except Exception:
            return "Unknown"

    async def _extract_all_messages(self, page: Page) -> list[Message]:
        turns = await page.query_selector_all(self.SELECTORS["turn"])
        messages = []
        for idx, turn in enumerate(turns):
            role = await self._get_role(turn)
            msg = await self._extract_message(turn, idx, role, page)
            messages.append(msg)
        return messages

    async def _get_role(self, turn) -> str:
        test_id = await turn.get_attribute("data-testid") or ""
        return "user" if "human" in test_id else "assistant"

    async def _extract_message(self, turn, idx: int, role: str, page: Page) -> Message:
        raw_html = await turn.inner_html()
        text = await self._extract_plain_text(turn)
        thinking = await self._extract_thinking(turn, page) if role == "assistant" else []
        artifacts = await self._extract_artifacts(turn)

        return Message(
            role=role,
            index=idx,
            text=text,
            thinking_blocks=thinking,
            artifacts=artifacts,
            raw_html=raw_html,
        )

    async def _extract_plain_text(self, turn) -> str:
        """Get visible prose text, excluding code/artifact blocks."""
        try:
            # Remove code blocks first by evaluating JS
            text = await turn.evaluate("""el => {
                const clone = el.cloneNode(true);
                clone.querySelectorAll('pre, code, [data-testid="artifact-block"]')
                     .forEach(n => n.remove());
                return clone.innerText.trim();
            }""")
            return text
        except Exception:
            return await turn.inner_text()

    async def _extract_thinking(self, turn, page: Page) -> list[ThinkingBlock]:
        """Click open any 'thinking' toggles and extract their content."""
        blocks = []
        toggles = await turn.query_selector_all(
            'button[aria-label*="thinking"], button[aria-label*="Thinking"], '
            'button:has-text("Thinking"), details summary'
        )

        for i, toggle in enumerate(toggles):
            try:
                await toggle.click()
                await page.wait_for_timeout(600)
            except Exception:
                pass

        # After expanding, find the content sections
        expanded = await turn.query_selector_all(
            'details[open], [class*="thinking-content"], [class*="ThinkingBlock"]'
        )
        for i, el in enumerate(expanded):
            content = await el.inner_text()
            if content.strip():
                blocks.append(ThinkingBlock(index=i, content=content.strip()))

        return blocks

    async def _extract_artifacts(self, turn) -> list[Artifact]:
        """Extract embedded code blocks and named artifact panels."""
        artifacts = []

        # 1. Named artifact blocks (the side-panel style)
        artifact_els = await turn.query_selector_all(
            '[data-testid*="artifact"], .artifact-block, [class*="ArtifactBlock"]'
        )
        for i, el in enumerate(artifact_els):
            title = await self._safe_inner_text(
                el, '[data-testid="artifact-title"], .artifact-title, h3, h4'
            )
            content = await el.inner_text()
            artifacts.append(Artifact(
                index=i,
                title=title or f"Artifact {i+1}",
                language=None,
                artifact_type="artifact",
                content=content.strip(),
            ))

        # 2. Code blocks (```lang ... ```)
        code_blocks = await turn.query_selector_all("pre")
        for i, pre in enumerate(code_blocks):
            code_el = await pre.query_selector("code")
            if not code_el:
                continue
            class_attr = await code_el.get_attribute("class") or ""
            lang = self._parse_language(class_attr)
            content = await code_el.inner_text()
            if content.strip():
                artifacts.append(Artifact(
                    index=len(artifacts),
                    title=f"Code block {i+1}",
                    language=lang,
                    artifact_type="code",
                    content=content.strip(),
                ))

        return artifacts

    async def _safe_inner_text(self, parent, selector: str) -> str:
        try:
            el = await parent.query_selector(selector)
            return (await el.inner_text()).strip() if el else ""
        except Exception:
            return ""

    @staticmethod
    def _parse_language(class_str: str) -> Optional[str]:
        """Extract language from class like 'language-python'."""
        match = re.search(r"language-(\w+)", class_str)
        return match.group(1) if match else None


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def scrape_and_save(url: str, output_file: str = "conversation.json", cookies: Optional[str] = None):
    scraper = ClaudeScraper(cookies_file=cookies, headless=True)
    print(f"Scraping: {url}")
    conv = await scraper.scrape(url)
    Path(output_file).write_text(conv.to_json(), encoding="utf-8")
    print(f"Saved {len(conv.messages)} messages → {output_file}")
    return conv


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python claude_scraper.py <claude.ai/chat/URL> [output.json] [cookies.json]")
        sys.exit(1)

    url = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "conversation.json"
    ck  = sys.argv[3] if len(sys.argv) > 3 else None

    asyncio.run(scrape_and_save(url, out, ck))
