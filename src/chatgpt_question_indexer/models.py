from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class Question:
    text: str
    summary: str
    number: int
    element_id: str
    rectangle: tuple[int, int, int, int]
    scroll_percent: float
    previous_feature: str = ""
    next_feature: str = ""
    text_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["rectangle"] = list(self.rectangle)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Question":
        copied = dict(data)
        copied["rectangle"] = tuple(copied.get("rectangle", (0, 0, 0, 0)))
        return cls(**copied)


@dataclass(slots=True)
class ConversationIndex:
    fingerprint: str
    window_title: str
    conversation_title: str
    questions: list[Question] = field(default_factory=list)
    scroll_mode: str = "percentage"
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "window_title": self.window_title,
            "conversation_title": self.conversation_title,
            "questions": [question.to_dict() for question in self.questions],
            "scroll_mode": self.scroll_mode,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationIndex":
        copied = dict(data)
        copied["questions"] = [
            Question.from_dict(item) for item in copied.get("questions", [])
        ]
        return cls(**copied)


@dataclass(slots=True)
class ConversationSnapshot:
    window_title: str
    conversation_title: str
    questions: list[Question] = field(default_factory=list)
