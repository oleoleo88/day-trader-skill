"""Ответы на вопросы участников (/ask) по базе фактов и недавним постам канала."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .db import now
from .llm import LLMError
from .prompts import Answer, qa_system
from .textutil import clip

if TYPE_CHECKING:
    from .context import Ctx

log = logging.getLogger(__name__)


async def answer_question(ctx: "Ctx", user_id: int, question: str) -> str | None:
    """Текст ответа или None, если отвечать не нужно (лимиты, сбой модели)."""
    c = ctx.cfg.community
    day = ctx.today()
    mine = await ctx.db.kv_incr(f"qa:{day}:{user_id}")
    if mine > c.qa_per_user_per_day:
        return "На сегодня лимит вопросов исчерпан. Загляни завтра 👋" if mine == c.qa_per_user_per_day + 1 else None
    if await ctx.db.kv_incr(f"qa:{day}") > c.qa_daily_limit:
        return None
    rows = await ctx.db.all(
        """SELECT headline, published_at FROM posts
           WHERE status='published' AND kind IN ('news', 'breaking', 'manual', 'suggestion') AND headline!=''
             AND published_at>? ORDER BY published_at DESC LIMIT 15""", (now() - 10 * 86400,))
    recent = "\n".join(f"• {ctx.local(r['published_at']):%d.%m} — {r['headline']}" for r in rows) or "(постов пока нет)"
    user = (f"Сегодня: {ctx.clock_text()}. Фаза: {ctx.phase_text()}.\n\n"
            f"Недавние посты канала:\n{recent}\n\n"
            f"Вопрос участника:\n\"\"\"\n{clip(question, 600)}\n\"\"\"")
    try:
        result = await ctx.llm.structured("qa", system=qa_system(ctx.facts()), user=user, schema=Answer, max_tokens=3000)
    except LLMError as e:
        log.warning("Не удалось ответить на вопрос: %s", e)
        return None
    return clip(result.answer.strip(), 700) or None
