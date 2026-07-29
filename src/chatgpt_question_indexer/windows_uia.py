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

from .models import ConversationIndex, ConversationSnapshot, Question
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
        children = container.children()
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
        for marker, bubble in cls._message_pairs(container):
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
                window = self.find_window()
                container = self.find_thread_container(window)
                current = self.scroll_percent(container)
                page = parse_direct_children(self._snapshot(container), current)
                before = len(questions)
                questions = merge_questions(questions, page)
                stagnant = stagnant + 1 if len(questions) == before else 0
                if current >= 99.0:
                    break
                try:
                    view_size = float(container.iface_scroll.CurrentVerticalViewSize)
                except Exception:
                    view_size = 20.0
                step = max(8.0, min(35.0, view_size * 0.8))
                percent = min(100.0, max(percent + step, current + 3.0))
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
            scroll_mode="exact",
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

    def _find_live_message(self, container, question: Question):
        pairs = self._message_pairs(container)
        for _marker, bubble in pairs:
            if question.element_id and _runtime_id(bubble) == question.element_id:
                return bubble
        for marker, bubble in pairs:
            try:
                parsed = parse_direct_children(
                    [_node(marker, max_depth=0), _node(bubble, max_depth=4)],
                    self.scroll_percent(container),
                )
                if parsed and parsed[0].text_hash == question.text_hash:
                    return bubble
            except Exception:
                continue
        return None

    def jump(self, question: Question) -> str:
        container = self.find_thread_container()
        live = self._find_live_message(container, question)
        if live is not None:
            try:
                live.iface_scroll_item.ScrollIntoView()
                return "exact"
            except Exception:
                LOGGER.exception("ScrollIntoView failed; using percentage")

        self.set_scroll_percent(container, question.scroll_percent)
        time.sleep(0.06)
        live = self._find_live_message(container, question)
        if live is not None:
            try:
                live.iface_scroll_item.ScrollIntoView()
                return "percentage+refine"
            except Exception:
                pass
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
