from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .models import Question


USER_MARKERS = ("You said:", "你说：", "你说:")
ASSISTANT_MARKERS = ("ChatGPT said:", "ChatGPT 说：", "ChatGPT 说:")


@dataclass(slots=True)
class AccessibleNode:
    role: str
    name: str
    element_id: str
    rectangle: tuple[int, int, int, int]
    class_name: str = ""
    children: list["AccessibleNode"] = field(default_factory=list)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def summarize(value: str, limit: int = 50) -> str:
    normalized = normalize_text(value)
    return normalized if len(normalized) <= limit else normalized[:limit] + "…"


def hash_text(value: str) -> str:
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


def collect_text(node: AccessibleNode) -> str:
    """Collect leaf text without duplicating accessible aggregate names."""
    if node.role in {"Text", "Edit", "ListItem"} and normalize_text(node.name):
        return normalize_text(node.name)
    parts = [collect_text(child) for child in node.children]
    return normalize_text(" ".join(part for part in parts if part))


def parse_direct_children(
    children: list[AccessibleNode], scroll_percent: float
) -> list[Question]:
    """Parse the observed ChatGPT UIA sequence: marker then message bubble."""
    found: list[Question] = []
    seen: set[str] = set()
    for index, node in enumerate(children):
        marker = normalize_text(node.name)
        # The real semantic role marker is visually hidden. Requiring the
        # observed sr-only class prevents literal "You said:" text inside an
        # assistant answer from being mistaken for a new user message.
        if marker not in USER_MARKERS or "sr-only" not in node.class_name:
            continue
        message_node = None
        for candidate in children[index + 1 : index + 5]:
            candidate_name = normalize_text(candidate.name)
            if candidate_name in USER_MARKERS + ASSISTANT_MARKERS:
                break
            text = collect_text(candidate)
            if text and text not in {"复制消息", "Copy message"}:
                message_node = candidate
                break
        if message_node is None:
            continue
        text = collect_text(message_node)
        digest = hash_text(text)
        if not text or digest in seen:
            continue
        seen.add(digest)
        found.append(
            Question(
                text=text,
                summary=summarize(text),
                number=len(found) + 1,
                element_id=message_node.element_id or node.element_id,
                rectangle=message_node.rectangle,
                scroll_percent=scroll_percent,
                text_hash=digest,
            )
        )
    for index, question in enumerate(found):
        question.previous_feature = (
            found[index - 1].text_hash[:16] if index > 0 else ""
        )
        question.next_feature = (
            found[index + 1].text_hash[:16] if index + 1 < len(found) else ""
        )
    return found


def merge_questions(
    existing: list[Question],
    incoming: list[Question],
    *,
    preserve_existing_position: bool = False,
) -> list[Question]:
    by_hash = {question.text_hash: question for question in existing}
    order = [question.text_hash for question in existing]
    for question in incoming:
        if question.text_hash not in by_hash:
            order.append(question.text_hash)
        elif preserve_existing_position:
            question.scroll_percent = by_hash[question.text_hash].scroll_percent
        by_hash[question.text_hash] = question
    merged = [by_hash[digest] for digest in order]
    for index, question in enumerate(merged):
        question.number = index + 1
        question.previous_feature = merged[index - 1].text_hash[:16] if index else ""
        question.next_feature = (
            merged[index + 1].text_hash[:16] if index + 1 < len(merged) else ""
        )
    return merged
