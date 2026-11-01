"""
export_cookies.py
-----------------
Launches a visible Chromium window, lets you log in to claude.ai manually,
then saves your session cookies to cookies.json for use by the scraper.

Usage:
    python export_cookies.py
    # Log in inside the browser window that opens
    # Press ENTER in the terminal when done
"""

import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright


async def export_cookies(output_file: str = "cookies.json"):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)  # visible window
        context = await browser.new_context()
        page = await context.new_page()

        print("Opening claude.ai — please log in…")
        await page.goto("https://claude.ai", wait_until="domcontentloaded")

        input("\nPress ENTER after you have logged in and the chat page is visible…")

        cookies = await context.cookies()
        # Keep only claude.ai / anthropic cookies
        relevant = [c for c in cookies if "claude.ai" in c["domain"] or "anthropic" in c["domain"]]
        Path(output_file).write_text(json.dumps(relevant, indent=2), encoding="utf-8")
        print(f"Saved {len(relevant)} cookies → {output_file}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(export_cookies())
