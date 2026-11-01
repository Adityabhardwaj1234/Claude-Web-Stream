"""
api_extractor.py
----------------
Uses the Anthropic Python SDK directly to send prompts and extract
structured responses: text, thinking blocks, tool use, and artifacts.
This is the cleanest approach — no browser scraping needed.

Requirements:
    pip install anthropic

Set your API key:
    export ANTHROPIC_API_KEY=sk-ant-...
"""

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional
import anthropic


# ─── Data models ──────────────────────────────────────────────────────────────

@dataclass
class ExtractedResponse:
    prompt: str
    text_blocks: list[str] = field(default_factory=list)
    thinking_blocks: list[str] = field(default_factory=list)
    tool_use_blocks: list[dict] = field(default_factory=list)
    tool_result_blocks: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)      # parsed from markdown code fences
    input_tokens: int = 0
    output_tokens: int = 0

    def full_text(self) -> str:
        return "\n".join(self.text_blocks)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent, ensure_ascii=False)


# ─── Extractor ────────────────────────────────────────────────────────────────

class AnthropicExtractor:
    """
    Sends a prompt to Claude and extracts every structured block from the reply.

    Parameters
    ----------
    model : str
        Claude model to use.
    enable_thinking : bool
        If True, requests extended thinking (budget_tokens controls depth).
    budget_tokens : int
        Thinking budget in tokens (min 1000, only used when enable_thinking=True).
    """

    def __init__(
        self,
        model: str = "claude-opus-4-5",
        enable_thinking: bool = False,
        budget_tokens: int = 8000,
        max_tokens: int = 16000,
        api_key: Optional[str] = None,
    ):
        self.model = model
        self.enable_thinking = enable_thinking
        self.budget_tokens = budget_tokens
        self.max_tokens = max_tokens
        self.client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    def extract(self, prompt: str, system: Optional[str] = None) -> ExtractedResponse:
        """Send prompt, return a structured ExtractedResponse."""
        kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        if system:
            kwargs["system"] = system
        if self.enable_thinking:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.budget_tokens,
            }

        response = self.client.messages.create(**kwargs)
        return self._parse_response(prompt, response)

    def _parse_response(self, prompt: str, response) -> ExtractedResponse:
        result = ExtractedResponse(
            prompt=prompt,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

        for block in response.content:
            btype = block.type

            if btype == "thinking":
                result.thinking_blocks.append(block.thinking)

            elif btype == "text":
                result.text_blocks.append(block.text)
                # Also parse markdown artifacts inside the text
                result.artifacts.extend(_extract_code_fences(block.text))

            elif btype == "tool_use":
                result.tool_use_blocks.append({
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

            elif btype == "tool_result":
                content = block.content
                if isinstance(content, list):
                    content = " ".join(c.text for c in content if hasattr(c, "text"))
                result.tool_result_blocks.append({
                    "tool_use_id": getattr(block, "tool_use_id", None),
                    "content": content,
                })

        return result


# ─── Markdown code-fence parser ───────────────────────────────────────────────

import re

_FENCE_RE = re.compile(
    r"```(?P<lang>\w*)\n(?P<code>.*?)```",
    re.DOTALL,
)


def _extract_code_fences(text: str) -> list[dict]:
    artifacts = []
    for i, m in enumerate(_FENCE_RE.finditer(text)):
        artifacts.append({
            "index": i,
            "language": m.group("lang") or None,
            "content": m.group("code").strip(),
        })
    return artifacts


# ─── Pretty printer ───────────────────────────────────────────────────────────

def print_extracted(r: ExtractedResponse):
    sep = "─" * 60
    print(f"\n{'='*60}")
    print(f"PROMPT: {r.prompt[:120]}")
    print(f"Tokens: {r.input_tokens} in / {r.output_tokens} out")
    print("="*60)

    if r.thinking_blocks:
        print(f"\n🧠 THINKING ({len(r.thinking_blocks)} block(s))")
        print(sep)
        for i, t in enumerate(r.thinking_blocks):
            print(f"  [block {i}]\n{t[:800]}")
            if len(t) > 800:
                print(f"  … ({len(t)-800} more chars)")

    if r.text_blocks:
        print(f"\n💬 TEXT ({len(r.text_blocks)} block(s))")
        print(sep)
        for t in r.text_blocks:
            print(t[:1000])

    if r.artifacts:
        print(f"\n📦 ARTIFACTS ({len(r.artifacts)})")
        print(sep)
        for art in r.artifacts:
            lang = art.get("language") or "text"
            print(f"  [{art['index']}] language={lang}")
            print(art["content"][:400])
            if len(art["content"]) > 400:
                print(f"  … ({len(art['content'])-400} more chars)")

    if r.tool_use_blocks:
        print(f"\n🔧 TOOL USE ({len(r.tool_use_blocks)})")
        print(sep)
        for t in r.tool_use_blocks:
            print(f"  {t['name']} → {json.dumps(t['input'])[:200]}")

    print()


# ─── Example usage ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # --- Example 1: basic extraction ---
    extractor = AnthropicExtractor(model="claude-sonnet-4-6")
    r = extractor.extract(
        "Write a Python function to compute Fibonacci numbers, "
        "then explain how it works."
    )
    print_extracted(r)
    print(r.to_json())

    # --- Example 2: with thinking enabled ---
    # extractor_think = AnthropicExtractor(
    #     model="claude-opus-4-5",   # thinking requires Opus or Sonnet 3.7+
    #     enable_thinking=True,
    #     budget_tokens=5000,
    # )
    # r2 = extractor_think.extract("Solve this step by step: 17 × 23 + 41²")
    # print_extracted(r2)
