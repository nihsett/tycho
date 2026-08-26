"""Deterministic prompt-injection quarantine gate."""

import re

_INSTRUCTION_PATTERNS = (
    re.compile(r"\bignore (?:all |any )?(?:previous|prior) instructions\b", re.I),
    re.compile(r"\breveal (?:the |your )?system prompt\b", re.I),
    re.compile(r"\byou are (?:chatgpt|an ai assistant|an llm)\b", re.I),
    re.compile(r"\b(?:assistant|system)\s*:\s*(?:follow|ignore|execute)\b", re.I),
)


def contains_llm_instructions(payload: bytes) -> bool:
    """Return True for explicit text aimed at controlling an LLM."""
    text = payload.decode("utf-8", errors="ignore")
    return any(pattern.search(text) for pattern in _INSTRUCTION_PATTERNS)
