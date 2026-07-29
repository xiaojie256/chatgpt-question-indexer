from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .models import ConversationIndex


class IndexCache:
    """Bounded on-disk cache with a small title-to-fingerprint catalog."""

    CATALOG_NAME = "catalog.json"

    def __init__(
        self,
        directory: Path,
        max_conversations: int = 30,
        max_bytes: int = 25 * 1024 * 1024,
        retention_days: int = 90,
    ):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_conversations = max(1, max_conversations)
        self.max_bytes = max(1024 * 1024, max_bytes)
        self.retention_seconds = max(1, retention_days) * 86400
        self.catalog_path = directory / self.CATALOG_NAME
        self._catalog = self._load_or_rebuild_catalog()
        self.prune()

    @staticmethod
    def conversation_key(window_title: str, conversation_title: str) -> str:
        value = f"{window_title.strip()}|{conversation_title.strip()}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _path(self, fingerprint: str) -> Path:
        return self.directory / f"{fingerprint}.json"

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _load_or_rebuild_catalog(self) -> dict[str, dict[str, Any]]:
        if self.catalog_path.exists():
            try:
                raw = json.loads(self.catalog_path.read_text(encoding="utf-8"))
                return dict(raw.get("entries", {}))
            except (OSError, ValueError, TypeError):
                pass
        entries: dict[str, dict[str, Any]] = {}
        for path in self.directory.glob("*.json"):
            if path.name == self.CATALOG_NAME:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                key = self.conversation_key(
                    data.get("window_title", ""), data.get("conversation_title", "")
                )
                entries[key] = {
                    "fingerprint": data["fingerprint"],
                    "window_title": data.get("window_title", ""),
                    "conversation_title": data.get("conversation_title", ""),
                    "last_accessed": path.stat().st_mtime,
                }
            except (OSError, ValueError, KeyError, TypeError):
                continue
        self._save_catalog(entries)
        return entries

    def _save_catalog(self, entries: dict[str, dict[str, Any]] | None = None) -> None:
        if entries is not None:
            self._catalog = entries
        self._write_json(
            self.catalog_path, {"version": 1, "entries": self._catalog}
        )

    def save(self, index: ConversationIndex) -> Path:
        path = self._path(index.fingerprint)
        self._write_json(path, index.to_dict())
        now = time.time()
        key = self.conversation_key(index.window_title, index.conversation_title)
        old = self._catalog.get(key)
        if old and old.get("fingerprint") != index.fingerprint:
            old_path = self._path(str(old.get("fingerprint", "")))
            if old_path.exists():
                old_path.unlink()
        self._catalog[key] = {
            "fingerprint": index.fingerprint,
            "window_title": index.window_title,
            "conversation_title": index.conversation_title,
            "last_accessed": now,
        }
        self._save_catalog()
        self.prune(protected_fingerprint=index.fingerprint)
        return path

    def load(self, fingerprint: str) -> ConversationIndex | None:
        path = self._path(fingerprint)
        if not path.exists():
            return None
        try:
            return ConversationIndex.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError):
            return None

    def load_for_conversation(
        self, window_title: str, conversation_title: str
    ) -> ConversationIndex | None:
        key = self.conversation_key(window_title, conversation_title)
        entry = self._catalog.get(key)
        if not entry:
            return None
        loaded = self.load(str(entry.get("fingerprint", "")))
        if loaded is None:
            self._catalog.pop(key, None)
            self._save_catalog()
            return None
        entry["last_accessed"] = time.time()
        try:
            self._path(loaded.fingerprint).touch()
        except OSError:
            pass
        self._save_catalog()
        return loaded

    def prune(self, protected_fingerprint: str = "") -> int:
        now = time.time()
        paths = [
            path
            for path in self.directory.glob("*.json")
            if path.name != self.CATALOG_NAME
        ]
        removed = 0
        cutoff = now - self.retention_seconds

        def remove(path: Path) -> None:
            nonlocal removed
            if path.stem == protected_fingerprint:
                return
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass

        for path in paths:
            try:
                if path.stat().st_mtime < cutoff:
                    remove(path)
            except OSError:
                continue

        paths = sorted(
            (
                path
                for path in self.directory.glob("*.json")
                if path.name != self.CATALOG_NAME
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in paths[self.max_conversations :]:
            remove(path)

        paths = sorted(
            (
                path
                for path in self.directory.glob("*.json")
                if path.name != self.CATALOG_NAME
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        total = sum(path.stat().st_size for path in paths)
        for path in reversed(paths):
            if total <= self.max_bytes:
                break
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            before = path.exists()
            remove(path)
            if before and not path.exists():
                total -= size

        existing = {
            path.stem
            for path in self.directory.glob("*.json")
            if path.name != self.CATALOG_NAME
        }
        self._catalog = {
            key: entry
            for key, entry in self._catalog.items()
            if str(entry.get("fingerprint", "")) in existing
        }
        self._save_catalog()
        return removed

    def stats(self) -> tuple[int, int]:
        paths = [
            path
            for path in self.directory.glob("*.json")
            if path.name != self.CATALOG_NAME
        ]
        return len(paths), sum(path.stat().st_size for path in paths)

    def clear(self) -> int:
        count = 0
        for path in self.directory.glob("*.json"):
            if path.name == self.CATALOG_NAME:
                continue
            path.unlink()
            count += 1
        self._catalog = {}
        self._save_catalog()
        return count
