"""Small keyword-ranked knowledge base loaded from a JSON file."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

REVIEW_INTERVAL_DAYS = 365

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "cannot", "do", "does", "for", "from",
    "has", "have", "how", "i", "if", "in", "into", "is", "it", "its", "my", "no", "not", "of", "on", "or",
    "the", "their", "then", "this", "to", "up", "was", "when", "with", "you", "your",
}


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9_]+", text.lower()) if t not in _STOPWORDS and len(t) > 1]


class KnowledgeBase:
    def __init__(self, articles: list[dict[str, Any]], today: date | None = None):
        self.articles = {a["id"]: a for a in articles}
        self.today = today
        self._doc_tokens: dict[str, dict[str, Counter]] = {}
        df: Counter = Counter()
        for a in articles:
            fields = {
                "title": Counter(_tokens(a["title"])),
                "tags": Counter(_tokens(" ".join(a.get("tags", [])))),
                "body": Counter(_tokens(a["body"])),
            }
            self._doc_tokens[a["id"]] = fields
            df.update(set().union(*[set(c) for c in fields.values()]))
        n = max(len(articles), 1)
        self._idf = {t: math.log(1 + n / c) for t, c in df.items()}

    @classmethod
    def from_file(cls, path: Path | str) -> "KnowledgeBase":
        return cls(json.loads(Path(path).read_text()))

    def _review_overdue(self, article: dict[str, Any]) -> bool:
        today = self.today or date.today()
        reviewed = date.fromisoformat(article["last_reviewed"])
        return (today - reviewed).days > REVIEW_INTERVAL_DAYS

    def present(self, article: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": article["id"],
            "title": article["title"],
            "category": article["category"],
            "status": article["status"],
            "last_reviewed": article["last_reviewed"],
            "review_overdue": self._review_overdue(article),
            "superseded_by": article.get("superseded_by"),
            "body": article["body"],
        }

    def search(self, query: str, *, limit: int = 5, include_retired: bool = False) -> list[dict[str, Any]]:
        q = _tokens(query)
        if not q:
            return []
        scored = []
        for aid, fields in self._doc_tokens.items():
            article = self.articles[aid]
            if article["status"] == "retired" and not include_retired:
                continue
            score = 0.0
            for t in q:
                idf = self._idf.get(t, 0.0)
                score += idf * (3 * fields["title"][t] + 2 * fields["tags"][t] + min(fields["body"][t], 3))
            if score > 0:
                scored.append((score, aid))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [dict(self.present(self.articles[aid]), score=round(score, 2)) for score, aid in scored[:limit]]

    def get(self, article_id: str) -> dict[str, Any] | None:
        a = self.articles.get(article_id)
        return self.present(a) if a else None
