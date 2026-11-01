"""
conversation_parser.py
-----------------------
Reads the JSON produced by claude_scraper.py and provides:
  - Pretty-printed terminal summary
  - Extraction of just thinking blocks
  - Extraction of just artifacts (with syntax highlighting via pygments)
  - Markdown export

Usage:
    python conversation_parser.py conversation.json
    python conversation_parser.py conversation.json --thinking
    python conversation_parser.py conversation.json --artifacts
    python conversation_parser.py conversation.json --export-md output.md
"""

import argparse
import json
import sys
from pathlib import Path
from textwrap import indent

try:
    from pygments import highlight
    from pygments.lexers import get_lexer_by_name, TextLexer
    from pygments.formatters import TerminalFormatter
    PYGMENTS_AVAILABLE = True
except ImportError:
    PYGMENTS_AVAILABLE = False


# ─── Loader ───────────────────────────────────────────────────────────────────

def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ─── Formatters ───────────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
PURPLE = "\033[95m"
DIM    = "\033[2m"


def colorize_code(code: str, language: str) -> str:
    if not PYGMENTS_AVAILABLE or not language:
        return code
    try:
        lexer = get_lexer_by_name(language, stripall=True)
        return highlight(code, lexer, TerminalFormatter())
    except Exception:
        return code


def print_divider(char="─", width=70):
    print(f"{DIM}{char * width}{RESET}")


# ─── Summary view ─────────────────────────────────────────────────────────────

def print_summary(conv: dict):
    print(f"\n{BOLD}{'='*70}{RESET}")
    print(f"{BOLD}  Conversation: {conv['title']}{RESET}")
    print(f"  URL: {DIM}{conv['url']}{RESET}")
    print(f"  Messages: {len(conv['messages'])}")
    print(f"{BOLD}{'='*70}{RESET}\n")

    for msg in conv["messages"]:
        role = msg["role"].upper()
        color = CYAN if msg["role"] == "user" else GREEN
        print(f"{color}{BOLD}[{role} — turn {msg['index']}]{RESET}")

        # Plain text
        if msg["text"]:
            wrapped = indent(msg["text"][:600], "  ")
            if len(msg["text"]) > 600:
                wrapped += f"\n  {DIM}… ({len(msg['text'])-600} more chars){RESET}"
            print(wrapped)

        # Thinking blocks
        if msg["thinking_blocks"]:
            print(f"\n  {PURPLE}{BOLD}🧠 Thinking ({len(msg['thinking_blocks'])} block(s)){RESET}")
            for tb in msg["thinking_blocks"]:
                preview = tb["content"][:300]
                print(f"  {DIM}{indent(preview, '  | ')}{RESET}")

        # Artifacts
        if msg["artifacts"]:
            print(f"\n  {YELLOW}{BOLD}📦 Artifacts ({len(msg['artifacts'])}){RESET}")
            for art in msg["artifacts"]:
                lang = art.get("language") or art.get("artifact_type") or "text"
                print(f"  {YELLOW}  [{art['index']}] {art['title']} ({lang}){RESET}")
                code = colorize_code(art["content"][:400], lang)
                print(indent(code, "      "))

        print_divider()


# ─── Thinking-only view ───────────────────────────────────────────────────────

def print_thinking(conv: dict):
    print(f"\n{PURPLE}{BOLD}🧠 All Thinking Blocks{RESET}\n")
    found = False
    for msg in conv["messages"]:
        for tb in msg.get("thinking_blocks", []):
            found = True
            print(f"{DIM}[turn {msg['index']}  thinking block {tb['index']}]{RESET}")
            print(tb["content"])
            print_divider()
    if not found:
        print("No thinking blocks found.")


# ─── Artifacts-only view ──────────────────────────────────────────────────────

def print_artifacts(conv: dict):
    print(f"\n{YELLOW}{BOLD}📦 All Artifacts{RESET}\n")
    found = False
    for msg in conv["messages"]:
        for art in msg.get("artifacts", []):
            found = True
            lang = art.get("language") or "text"
            print(f"{YELLOW}{BOLD}[turn {msg['index']}]  {art['title']}  ({lang}){RESET}")
            code = colorize_code(art["content"], lang)
            print(code)
            print_divider()
    if not found:
        print("No artifacts found.")


# ─── Markdown export ──────────────────────────────────────────────────────────

def export_markdown(conv: dict, output_path: str):
    lines = [
        f"# {conv['title']}\n",
        f"> URL: {conv['url']}\n",
        f"> Messages: {len(conv['messages'])}\n",
        "\n---\n",
    ]

    for msg in conv["messages"]:
        role = "**User**" if msg["role"] == "user" else "**Claude**"
        lines.append(f"\n## {role} (turn {msg['index']})\n")

        if msg["text"]:
            lines.append(msg["text"] + "\n")

        for tb in msg.get("thinking_blocks", []):
            lines.append("\n<details>\n<summary>🧠 Thinking</summary>\n\n")
            lines.append(tb["content"] + "\n\n</details>\n")

        for art in msg.get("artifacts", []):
            lang = art.get("language") or ""
            lines.append(f"\n**{art['title']}**\n\n```{lang}\n{art['content']}\n```\n")

        lines.append("\n---\n")

    Path(output_path).write_text("\n".join(lines), encoding="utf-8")
    print(f"Markdown saved → {output_path}")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Parse claude.ai conversation JSON")
    parser.add_argument("file", help="Path to conversation.json")
    parser.add_argument("--thinking",   action="store_true", help="Show only thinking blocks")
    parser.add_argument("--artifacts",  action="store_true", help="Show only artifacts")
    parser.add_argument("--export-md",  metavar="FILE",      help="Export as Markdown")
    args = parser.parse_args()

    conv = load(args.file)

    if args.export_md:
        export_markdown(conv, args.export_md)
    elif args.thinking:
        print_thinking(conv)
    elif args.artifacts:
        print_artifacts(conv)
    else:
        print_summary(conv)


if __name__ == "__main__":
    main()
