from __future__ import annotations

import ctypes
import hashlib
import logging
import time
from ctypes import wintypes
from dataclasses import dataclass

import win32con
import win32gui
import win32process

from .models import (
    CURRENT_SCROLL_MODE,
    ConversationIndex,
    ConversationSnapshot,
    Question,
)
from .parser import (
    ASSISTANT_MARKERS,
    USER_MARKERS,
    AccessibleNode,
    hash_text,
    merge_questions,
    parse_direct_children,
)


LOGGER = logging.getLogger(__name__)
NO_SCROLL = -1.0


@dataclass(slots=True)
class WindowDetails:
    handle: int
    process_id: int
    process_name: str
    title: str
    rectangle: tuple[int, int, int, int]


def _runtime_id(wrapper) -> str:
    try:
        return ".".join(str(part) for part in wrapper.element_info.runtime_id)
    except Exception:
        return ""


def _rectangle(wrapper) -> tuple[int, int, int, int]:
    try:
        rect = wrapper.element_info.rectangle
        return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        return 0, 0, 0, 0


def _node(wrapper, depth: int = 0, max_depth: int = 12) -> AccessibleNode:
    info = wrapper.element_info
    children = []
    if depth < max_depth:
        try:
            children = [_node(child, depth + 1, max_depth) for child in wrapper.children()]
        except Exception:
            children = []
    return AccessibleNode(
        role=info.control_type or "",
        name=info.name or "",
        element_id=_runtime_id(wrapper),
        rectangle=_rectangle(wrapper),
        class_name=info.class_name or "",
        children=children,
    )


def _nearby_indexes(position: int, count: int, radius: int = 3) -> list[int]:
    """Return the indexed message first, followed by nearby fallback positions."""
    if count <= 0:
        return []
    position = max(0, min(count - 1, position))
    indexes = [position]
    for distance in range(1, radius + 1):
        before = position - distance
        after = position + distance
        if before >= 0:
            indexes.append(before)
        if after < count:
            indexes.append(after)
    return indexes


def _intersects(
    first: tuple[int, int, int, int], second: tuple[int, int, int, int]
) -> bool:
    return (
        first[0] < second[2]
        and first[2] > second[0]
        and first[1] < second[3]
        and first[3] > second[1]
    )


def _scan_step(view_size: float) -> float:
    """Advance with enough overlap to catch short bubbles between long answers."""
    return max(3.0, min(25.0, view_size * 0.65))


def _transient_end_jump(requested: float, current: float) -> bool:
    """Detect Chromium briefly reporting the bottom during mid-scan updates."""
    return requested < 95.0 and current >= 99.0


class WindowsUIA:
    _cached_window_handle = 0

    def find_window(self):
        from pywinauto import Desktop

        if self._cached_window_handle and win32gui.IsWindow(
            self._cached_window_handle
        ):
            try:
                return Desktop(backend="uia").window(
                    handle=self._cached_window_handle
                ).wrapper_object()
            except Exception:
                self._cached_window_handle = 0

        handles = []

        def collect(handle, _extra):
            if not win32gui.IsWindowVisible(handle):
                return
            title = win32gui.GetWindowText(handle)
            if title.strip().lower() == "chatgpt":
                rect = win32gui.GetWindowRect(handle)
                area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
                handles.append((area, handle))

        win32gui.EnumWindows(collect, None)
        if handles:
            handle = max(handles, key=lambda item: item[0])[1]
            self._cached_window_handle = handle
            return Desktop(backend="uia").window(handle=handle).wrapper_object()

        candidates = []
        for wrapper in Desktop(backend="uia").windows():
            title = wrapper.window_text() or ""
            if title.strip().lower() == "chatgpt":
                rect = wrapper.rectangle()
                candidates.append((max(0, rect.width()) * max(0, rect.height()), wrapper))
        if not candidates:
            raise RuntimeError("未找到 ChatGPT 桌面应用主窗口")
        wrapper = max(candidates, key=lambda item: item[0])[1]
        self._cached_window_handle = wrapper.handle
        return wrapper

    def details(self) -> WindowDetails:
        window = self.find_window()
        _, pid = win32process.GetWindowThreadProcessId(window.handle)
        rect = _rectangle(window)
        return WindowDetails(
            handle=window.handle,
            process_id=pid,
            process_name="ChatGPT.exe",
            title=window.window_text() or "ChatGPT",
            rectangle=rect,
        )

    def find_thread_container(self, window=None):
        window = window or self.find_window()
        candidates = []
        for element in window.descendants(control_type="Group"):
            try:
                class_name = element.element_info.class_name or ""
                if "thread-scroll-container" not in class_name:
                    continue
                scroll = element.iface_scroll
                if scroll.CurrentVerticallyScrollable:
                    candidates.append(element)
            except Exception:
                continue
        if not candidates:
            raise RuntimeError("未找到支持 UIA Scroll pattern 的对话滚动容器")
        return max(
            candidates,
            key=lambda item: max(0, _rectangle(item)[2] - _rectangle(item)[0])
            * max(0, _rectangle(item)[3] - _rectangle(item)[1]),
        )

    @staticmethod
    def scroll_percent(container) -> float:
        try:
            return float(container.iface_scroll.CurrentVerticalScrollPercent)
        except Exception:
            return 0.0

    @staticmethod
    def set_scroll_percent(container, percent: float) -> None:
        container.iface_scroll.SetScrollPercent(
            NO_SCROLL, max(0.0, min(100.0, percent))
        )

    @staticmethod
    def _message_pairs(container) -> list[tuple[object, object]]:
        last_error = None
        for attempt in range(3):
            try:
                children = container.children()
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.02)
        else:
            raise last_error or RuntimeError("无法读取对话消息节点")
        pairs = []
        for index, marker in enumerate(children):
            try:
                if (
                    (marker.element_info.name or "").strip() not in USER_MARKERS
                    or "sr-only" not in (marker.element_info.class_name or "")
                ):
                    continue
            except Exception:
                continue
            for candidate in children[index + 1 : index + 5]:
                try:
                    name = (candidate.element_info.name or "").strip()
                    if name in USER_MARKERS + ASSISTANT_MARKERS:
                        break
                    if candidate.element_info.control_type == "Group":
                        pairs.append((marker, candidate))
                        break
                except Exception:
                    continue
        return pairs

    @classmethod
    def _snapshot(cls, container) -> list[AccessibleNode]:
        """Build only user marker/bubble nodes, not the entire conversation tree."""
        nodes = []
        container_rect = _rectangle(container)
        for marker, bubble in cls._message_pairs(container):
            # Chromium keeps some virtualized/off-screen messages in the UIA
            # tree. Recording them on every scan page collapses all saved
            # percentages toward the final page and ruins fallback accuracy.
            if not _intersects(_rectangle(bubble), container_rect):
                continue
            nodes.extend([_node(marker, max_depth=0), _node(bubble, max_depth=4)])
        return nodes

    def conversation_title(self, window, container) -> str:
        container_rect = _rectangle(container)
        window_rect = _rectangle(window)
        for element in window.descendants(control_type="Button"):
            try:
                tokens = set((element.element_info.class_name or "").split())
                name = (element.element_info.name or "").strip()
                if (
                    name
                    and "sidebar-item" in tokens
                    and "bg-token-list-hover-background" in tokens
                ):
                    return name
            except Exception:
                continue
        candidates = []
        for element in window.descendants(control_type="Text"):
            try:
                name = (element.element_info.name or "").strip()
                left, top, right, bottom = _rectangle(element)
                if (
                    name
                    and left >= container_rect[0]
                    and left <= container_rect[0] + 100
                    and window_rect[1] + 30 <= top <= window_rect[1] + 100
                    and name not in USER_MARKERS + ASSISTANT_MARKERS
                    and len(name) <= 160
                ):
                    candidates.append((left, name))
            except Exception:
                continue
        return min(candidates, default=(0, ""), key=lambda item: item[0])[1]

    def scan(self, pause_seconds: float = 0.18) -> ConversationIndex:
        window = self.find_window()
        container = self.find_thread_container(window)
        original = self.scroll_percent(container)
        title = self.conversation_title(window, container)
        questions: list[Question] = []
        try:
            self.set_scroll_percent(container, 0.0)
            time.sleep(pause_seconds)
            percent = 0.0
            stagnant = 0
            while True:
                page = []
                for attempt in range(3):
                    window = self.find_window()
                    container = self.find_thread_container(window)
                    current = self.scroll_percent(container)
                    if _transient_end_jump(percent, current):
                        self.set_scroll_percent(container, percent)
                        time.sleep(max(0.03, pause_seconds))
                        continue
                    observed = parse_direct_children(
                        self._snapshot(container), current
                    )
                    # The first non-empty children list can still describe the
                    # previous virtual page. Treat it as a materialization
                    # primer and accept a later observation from the same
                    # scroll position.
                    if attempt > 0 and (observed or attempt == 2):
                        page = observed
                        break
                    # Empty pages can be legitimate between long assistant
                    # answers, but Chromium also briefly exposes stale or empty
                    # children while replacing a virtual page. Settle in place
                    # before advancing so short user questions are not skipped.
                    time.sleep(max(0.03, pause_seconds))
                before = len(questions)
                questions = merge_questions(questions, page)
                stagnant = stagnant + 1 if len(questions) == before else 0
                if percent >= 99.0 and current >= 99.0:
                    break
                try:
                    view_size = float(container.iface_scroll.CurrentVerticalViewSize)
                except Exception:
                    view_size = 20.0
                step = _scan_step(view_size)
                stable_current = (
                    percent
                    if _transient_end_jump(percent, current)
                    else current
                )
                percent = min(
                    100.0, max(percent + step, stable_current + 3.0)
                )
                self.set_scroll_percent(container, percent)
                time.sleep(pause_seconds)
                if percent >= 100.0 and stagnant >= 2:
                    break
        finally:
            try:
                self.set_scroll_percent(self.find_thread_container(), original)
            except Exception:
                LOGGER.exception("failed to restore scroll position")

        first = questions[0].text if questions else ""
        fingerprint = hashlib.sha256(
            f"{window.window_text()}|{title}|{first}".encode("utf-8")
        ).hexdigest()[:32]
        from datetime import datetime, timezone

        return ConversationIndex(
            fingerprint=fingerprint,
            window_title=window.window_text() or "ChatGPT",
            conversation_title=title,
            questions=questions,
            scroll_mode=CURRENT_SCROLL_MODE,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def visible_questions(self) -> list[Question]:
        container = self.find_thread_container()
        return parse_direct_children(
            self._snapshot(container), self.scroll_percent(container)
        )

    def probe(self) -> ConversationSnapshot:
        window = self.find_window()
        container = self.find_thread_container(window)
        return ConversationSnapshot(
            window_title=window.window_text() or "ChatGPT",
            conversation_title=self.conversation_title(window, container),
            questions=parse_direct_children(
                self._snapshot(container), self.scroll_percent(container)
            ),
        )

    @staticmethod
    def _matches_question(marker, bubble, question: Question, percent: float) -> bool:
        try:
            parsed = parse_direct_children(
                [_node(marker, max_depth=0), _node(bubble, max_depth=4)],
                percent,
            )
            return bool(parsed and parsed[0].text_hash == question.text_hash)
        except Exception:
            return False

    def _find_live_message(self, container, question: Question, pairs=None):
        """Find a message without hashing every question in a long conversation."""
        pairs = pairs if pairs is not None else self._message_pairs(container)
        for marker, bubble in pairs:
            if question.element_id and _runtime_id(bubble) == question.element_id:
                return marker, bubble

        percent = self.scroll_percent(container)
        checked: set[int] = set()
        expected = max(0, question.number - 1)
        candidate_indexes = _nearby_indexes(expected, len(pairs))
        container_rect = _rectangle(container)
        candidate_indexes.extend(
            index
            for index, (_marker, bubble) in enumerate(pairs)
            if _intersects(_rectangle(bubble), container_rect)
        )
        for index in candidate_indexes:
            if index in checked:
                continue
            checked.add(index)
            marker, bubble = pairs[index]
            if self._matches_question(marker, bubble, question, percent):
                return marker, bubble
        return None

    @staticmethod
    def _first_text_anchor(wrapper, max_depth: int = 7):
        """Return the first text leaf so long questions jump to their beginning."""
        stack = [(wrapper, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                if (
                    current.element_info.control_type in {"Text", "Edit", "ListItem"}
                    and (current.element_info.name or "").strip()
                ):
                    return current
                if depth < max_depth:
                    stack.extend(
                        (child, depth + 1)
                        for child in reversed(current.children())
                    )
            except Exception:
                continue
        return wrapper

    @classmethod
    def _scroll_message_into_view(cls, bubble) -> None:
        anchor = cls._first_text_anchor(bubble)
        try:
            anchor.iface_scroll_item.ScrollIntoView()
        except Exception:
            bubble.iface_scroll_item.ScrollIntoView()

    def jump(self, question: Question) -> str:
        container = self.find_thread_container()
        current = self.scroll_percent(container)
        distance = abs(current - question.scroll_percent)
        # Only inspect the current page when it is already near the target.
        # Searching a virtualized page that clearly cannot contain the target
        # adds latency without improving accuracy.
        if distance <= 1.0:
            live = self._find_live_message(container, question)
            if live is not None:
                try:
                    self._scroll_message_into_view(live[1])
                    return "exact"
                except Exception:
                    LOGGER.exception("ScrollIntoView failed; using percentage")

        if distance > 0.05:
            self.set_scroll_percent(container, question.scroll_percent)
            time.sleep(0.025)
            # Chromium replaces virtualized UIA nodes asynchronously. A fresh
            # wrapper is required; reusing the pre-scroll wrapper can expose
            # stale children and incorrectly stop at percentage-only mode.
            container = self.find_thread_container()
        # Chromium may expose one or two stale child snapshots while it
        # virtualizes the destination page. Poll the small direct-child list
        # with a strict bound; never fall back to a full-tree search.
        for attempt in range(3):
            pairs = self._message_pairs(container)
            live = self._find_live_message(container, question, pairs)
            if live is not None:
                try:
                    self._scroll_message_into_view(live[1])
                    return "percentage+refine"
                except Exception:
                    break
            if attempt < 2:
                time.sleep(0.025)
        return "percentage"


def process_is_elevated(process_id: int) -> bool | None:
    TOKEN_QUERY = 0x0008
    TOKEN_ELEVATION = 20
    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, process_id)
    if not process:
        return None
    token = wintypes.HANDLE()
    try:
        if not ctypes.windll.advapi32.OpenProcessToken(
            process, TOKEN_QUERY, ctypes.byref(token)
        ):
            return None
        elevation = wintypes.DWORD()
        returned = wintypes.DWORD()
        if not ctypes.windll.advapi32.GetTokenInformation(
            token,
            TOKEN_ELEVATION,
            ctypes.byref(elevation),
            ctypes.sizeof(elevation),
            ctypes.byref(returned),
        ):
            return None
        return bool(elevation.value)
    finally:
        if token:
            ctypes.windll.kernel32.CloseHandle(token)
        ctypes.windll.kernel32.CloseHandle(process)


class ChatGPTDock:
    """Perform explicit one-shot docking without fighting user window changes."""

    def __init__(self):
        self.original_placement = None
        self.chatgpt_handle = 0

    def place_once(self, sidebar, width: int, make_room: bool = True) -> None:
        uia = WindowsUIA()
        details = uia.details()
        handle = details.handle
        self.chatgpt_handle = handle
        if self.original_placement is None:
            self.original_placement = win32gui.GetWindowPlacement(handle)
        monitor = win32api_monitor(handle)
        work_left, work_top, work_right, work_bottom = monitor
        available_width = work_right - work_left
        sidebar_width = min(width, max(180, available_width // 2))
        chat_width = max(640, available_width - sidebar_width)
        if make_room:
            win32gui.ShowWindow(handle, win32con.SW_RESTORE)
            win32gui.SetWindowPos(
                handle,
                0,
                work_left,
                work_top,
                chat_width,
                work_bottom - work_top,
                win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
            )
        geometry = sidebar.geometry()
        frame = sidebar.frameGeometry()
        left_margin = max(0, geometry.left() - frame.left())
        top_margin = max(0, geometry.top() - frame.top())
        right_margin = max(0, frame.right() - geometry.right())
        bottom_margin = max(0, frame.bottom() - geometry.bottom())
        sidebar.setGeometry(
            work_left + chat_width + left_margin,
            work_top + top_margin,
            max(30, sidebar_width - left_margin - right_margin),
            max(
                120,
                work_bottom
                - work_top
                - top_margin
                - bottom_margin,
            ),
        )

    def restore(self) -> None:
        if self.original_placement and self.chatgpt_handle:
            try:
                win32gui.SetWindowPlacement(self.chatgpt_handle, self.original_placement)
            except Exception:
                LOGGER.exception("failed to restore ChatGPT window placement")


def win32api_monitor(handle: int) -> tuple[int, int, int, int]:
    import win32api

    monitor = win32api.MonitorFromWindow(handle, win32con.MONITOR_DEFAULTTONEAREST)
    return tuple(win32api.GetMonitorInfo(monitor)["Work"])


def is_window_cloaked(handle: int) -> bool:
    """Windows cloaks regular windows that belong to another virtual desktop."""
    cloaked = wintypes.DWORD()
    result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        handle, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return result == 0 and bool(cloaked.value)
