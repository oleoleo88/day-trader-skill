"""Common agent plumbing: prompt templates, lesson injection, JSON calls."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import Channel
from ..llm import LLMResult

if TYPE_CHECKING:
    from ..app import App

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str, base_dir: Path | None = None) -> str:
    """config/prompts/<name>.md overrides the built-in template, so prompts are editable without code."""
    if base_dir:
        p = base_dir / "config" / "prompts" / f"{name}.md"
        if p.exists():
            return p.read_text(encoding="utf-8")
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def render(template: str, **vars) -> str:
    for k, v in vars.items():
        template = template.replace("{{" + k + "}}", str(v if v is not None else ""))
    return template


class Agent:
    name = "agent"
    role = "default"  # LLM role -> model chain in network.yaml
    prompt = ""  # template file name without .md
    lesson_role: str | None = None  # which lessons this agent learns from

    def __init__(self, app: "App"):
        self.app = app
        self.db = app.db
        self.cfg = app.cfg
        self.log = logging.getLogger(f"tgnet.{self.name}")

    def channel_vars(self, ch: Channel | None) -> dict:
        if ch is None:
            return {}
        return {"channel_title": ch.title, "voice": ch.voice.strip(), "format_rules": ch.format_rules.strip(),
                "focus": ch.focus.strip(), "language": ch.language}

    def lessons_block(self, ch: Channel | None) -> str:
        role = self.lesson_role or self.name
        rows = self.db.lessons(ch.id if ch else None, role, k=int(self.cfg.learning.get("lessons_in_prompt", 12)))
        if not rows:
            return ""
        ids = [r["id"] for r in rows]
        self.db.x(f"UPDATE lessons SET hits=hits+1 WHERE id IN ({','.join('?' * len(ids))})", ids)
        lines = "\n".join(f"- {r['text']}" for r in rows)
        return ("\n\n## Уроки из прошлого опыта (накоплены на правках владельца и ошибках – соблюдай строго)\n"
                + lines)

    def system(self, ch: Channel | None = None, **vars) -> str:
        tpl = load_prompt(self.prompt or self.name, self.cfg.base_dir)
        return render(tpl, **{**self.channel_vars(ch), **vars}) + self.lessons_block(ch)

    async def ask_json(self, user: str, ch: Channel | None = None, *, temperature: float = 0.2,
                       grounding: bool = False, role: str | None = None, system: str | None = None,
                       **vars) -> tuple[dict, LLMResult]:
        return await self.app.router.json(role or self.role, system if system is not None else self.system(ch, **vars),
                                          user, temperature=temperature, grounding=grounding)
