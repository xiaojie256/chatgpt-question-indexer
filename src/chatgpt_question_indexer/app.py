from __future__ import annotations

import ctypes
import json
import logging
import platform
import sys
import time
import warnings
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Qt uses a single-threaded COM apartment on its GUI thread.
sys.coinit_flags = 2
warnings.filterwarnings("ignore", message="Apply externally defined coinit_flags")

import pythoncom
from PySide6.QtCore import QEvent, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .cache import IndexCache
from .models import (
    CURRENT_SCROLL_MODE,
    ConversationIndex,
    ConversationSnapshot,
    Question,
)
from .parser import merge_questions
from .windows_uia import (
    ChatGPTDock,
    ScanCancelled,
    WindowsUIA,
    is_window_cloaked,
    process_is_elevated,
)


PROJECT_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[2]
)
CONFIG_PATH = PROJECT_ROOT / "config.json"
LOGGER = logging.getLogger(__name__)


class ScanWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    progress = Signal(float, int)

    def __init__(self, mode: str, pause: float = 0):
        super().__init__()
        self.mode = mode
        self.pause = pause

    def run(self) -> None:
        pythoncom.CoInitialize()
        try:
            uia = WindowsUIA()
            if self.mode == "full":
                result = uia.scan(
                    self.pause,
                    self.progress.emit,
                    self.isInterruptionRequested,
                )
            elif self.mode == "visible":
                result = uia.visible_snapshot()
            else:
                result = uia.probe()
            self.completed.emit(result)
        except ScanCancelled:
            self.cancelled.emit()
        except Exception as exc:
            LOGGER.exception("accessibility scan failed")
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            pythoncom.CoUninitialize()


class JumpWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, question: Question):
        super().__init__()
        self.question = question

    def run(self) -> None:
        pythoncom.CoInitialize()
        try:
            self.completed.emit(WindowsUIA().jump(self.question))
        except Exception as exc:
            LOGGER.exception(
                "jump failed hash=%s length=%d",
                self.question.text_hash[:16],
                len(self.question.text),
            )
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            pythoncom.CoUninitialize()


class HotkeyThread(QThread):
    toggle_requested = Signal()
    rebuild_requested = Signal()
    registration_failed = Signal(str)

    def __init__(self, toggle: str, rebuild: str):
        super().__init__()
        self.bindings = ((1, toggle), (2, rebuild))
        self.thread_id = 0

    @staticmethod
    def _parse(value: str) -> tuple[int, int]:
        parts = [part.strip().upper() for part in value.split("+")]
        modifiers = 0
        if "ALT" in parts:
            modifiers |= 0x0001
        if "CTRL" in parts or "CONTROL" in parts:
            modifiers |= 0x0002
        if "SHIFT" in parts:
            modifiers |= 0x0004
        if "WIN" in parts:
            modifiers |= 0x0008
        key = parts[-1]
        vk = ord(key) if len(key) == 1 else getattr(__import__("win32con"), f"VK_{key}")
        return modifiers | 0x4000, vk  # MOD_NOREPEAT

    def run(self) -> None:
        user32 = ctypes.windll.user32
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        registered = []
        for identifier, value in self.bindings:
            modifiers, vk = self._parse(value)
            if user32.RegisterHotKey(None, identifier, modifiers, vk):
                registered.append(identifier)
            else:
                self.registration_failed.emit(f"全局快捷键 {value} 注册失败，可能已被占用")
        message = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == 0x0312:
                    if message.wParam == 1:
                        self.toggle_requested.emit()
                    elif message.wParam == 2:
                        self.rebuild_requested.emit()
        finally:
            for identifier in registered:
                user32.UnregisterHotKey(None, identifier)

    def stop(self) -> None:
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
        self.wait(1500)


class AccessibilityEventThread(QThread):
    """Listen for WinEvent accessibility changes from the ChatGPT process."""

    changed = Signal(bool)

    def __init__(self, process_id: int):
        super().__init__()
        self.process_id = process_id
        self.thread_id = 0
        self._callback = None

    def run(self) -> None:
        user32 = ctypes.windll.user32
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        callback_type = ctypes.WINFUNCTYPE(
            None,
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.HWND,
            wintypes.LONG,
            wintypes.LONG,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        last_fast_emit = 0.0
        last_slow_emit = 0.0

        def on_event(
            _hook,
            event,
            _hwnd,
            object_id,
            _child_id,
            _thread,
            _event_time,
        ):
            nonlocal last_fast_emit, last_slow_emit
            if event in {
                0x8000,  # EVENT_OBJECT_CREATE
                0x8002,  # EVENT_OBJECT_SHOW
                0x8004,  # EVENT_OBJECT_REORDER
                0x800E,  # EVENT_OBJECT_VALUECHANGE
            } and object_id != -8:  # ignore caret-only churn
                now = time.monotonic()
                immediate = event in {0x8000, 0x8002, 0x8004}
                if immediate and now - last_fast_emit >= 0.08:
                    last_fast_emit = now
                    self.changed.emit(True)
                elif not immediate and now - last_slow_emit >= 0.30:
                    last_slow_emit = now
                    self.changed.emit(False)

        self._callback = callback_type(on_event)
        hook = user32.SetWinEventHook(
            0x8000,
            0x800E,
            None,
            self._callback,
            self.process_id,
            0,
            0x0000,  # WINEVENT_OUTOFCONTEXT
        )
        message = wintypes.MSG()
        try:
            while hook and user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                pass
        finally:
            if hook:
                user32.UnhookWinEvent(hook)

    def stop(self) -> None:
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
        self.wait(1500)


class QuestionListWidget(QListWidget):
    """Resolve full-text tooltips lazily to avoid duplicating messages in Qt."""

    def __init__(self, question_provider):
        super().__init__()
        self.question_provider = question_provider
        self.viewport().installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.viewport() and event.type() == QEvent.Type.ToolTip:
            item = self.itemAt(event.pos())
            if item is None:
                QToolTip.hideText()
                return True
            position = item.data(Qt.ItemDataRole.UserRole)
            question = self.question_provider(position)
            if question is None:
                QToolTip.hideText()
            else:
                QToolTip.showText(event.globalPos(), question.text, self)
            return True
        return super().eventFilter(watched, event)


class Sidebar(QMainWindow):
    chatgpt_process_changed = Signal(int)

    def __init__(self, config: dict):
        super().__init__()
        self.config = config
        self.cache = IndexCache(
            PROJECT_ROOT / "data" / "indexes",
            max_conversations=int(config.get("cache_max_conversations", 30)),
            max_bytes=int(config.get("cache_max_megabytes", 25)) * 1024 * 1024,
            retention_days=int(config.get("cache_retention_days", 90)),
        )
        self.index: ConversationIndex | None = None
        self.worker: QThread | None = None
        self.worker_mode = ""
        self.busy = False
        self.collapsed = False
        self.force_quit = False
        self.last_jump_mode = "尚未跳转"
        self.pending_rebuild = False
        self.pending_probe = False
        self.pending_visible_refresh = False
        self.pending_jump_position: int | None = None
        self.active_jump_position: int | None = None
        self.active_container_id = ""
        self.cache_dirty = False
        self.last_visible_capture_at = 0.0
        self.highlighted_item: QListWidgetItem | None = None
        self.suppress_refresh_until = 0.0
        self.dock = ChatGPTDock()

        self.setWindowTitle("ChatGPT 提问导航")
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Window
        )
        self.setMinimumWidth(180)
        self._build_ui()
        self._build_tray()

        width = int(config.get("sidebar_width", 280))
        self.resize(width, 760)
        QTimer.singleShot(
            250, self.dock_once
        )
        QTimer.singleShot(550, self.refresh_current)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_current)
        self.refresh_timer.start(
            max(10, int(config.get("auto_refresh_seconds", 15))) * 1000
        )
        self.event_debounce = QTimer(self)
        self.event_debounce.setSingleShot(True)
        self.event_debounce.setInterval(700)
        self.event_debounce.timeout.connect(self.refresh_current)
        self.visible_capture_timer = QTimer(self)
        self.visible_capture_timer.setSingleShot(True)
        self.visible_capture_timer.timeout.connect(self.refresh_visible)
        self.cache_save_timer = QTimer(self)
        self.cache_save_timer.setSingleShot(True)
        self.cache_save_timer.setInterval(1500)
        self.cache_save_timer.timeout.connect(self._flush_cache)
        self.highlight_timer = QTimer(self)
        self.highlight_timer.setSingleShot(True)
        self.highlight_timer.timeout.connect(self._clear_jump_highlight)

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 6)
        header = QHBoxLayout()
        self.collapse_button = QPushButton("◀")
        self.collapse_button.setToolTip("折叠/展开")
        self.collapse_button.clicked.connect(self.toggle_collapse)
        self.dock_button = QPushButton("↔")
        self.dock_button.setToolTip("仅本次重新贴靠；之后不强制跟随窗口")
        self.dock_button.clicked.connect(self.dock_once)
        self.rebuild_button = QPushButton("重建")
        self.rebuild_button.clicked.connect(self.rebuild)
        self.clear_button = QPushButton("清除")
        self.clear_button.clicked.connect(self.clear_data)
        self.hide_button = QPushButton("—")
        self.hide_button.setToolTip("最小化到系统托盘")
        self.hide_button.clicked.connect(self.hide)
        for button in (
            self.collapse_button,
            self.dock_button,
            self.rebuild_button,
            self.clear_button,
            self.hide_button,
        ):
            header.addWidget(button)
        layout.addLayout(header)

        self.list_widget = QuestionListWidget(self.question_at)
        self.list_widget.itemClicked.connect(self.jump_to_item)
        self.list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.list_widget.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        layout.addWidget(self.list_widget, 1)
        self.status = QLabel("正在连接 ChatGPT…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.setCentralWidget(root)

    def _build_tray(self) -> None:
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogListView)
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        menu = QMenu()
        show_action = QAction("显示/隐藏", self)
        show_action.triggered.connect(self.toggle_visible)
        rebuild_action = QAction("重建索引", self)
        rebuild_action.triggered.connect(self.rebuild)
        dock_action = QAction("重新贴靠一次", self)
        dock_action.triggered.connect(self.dock_once)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_application)
        menu.addAction(show_action)
        menu.addAction(dock_action)
        menu.addAction(rebuild_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.toggle_visible()
            if reason == QSystemTrayIcon.ActivationReason.Trigger
            else None
        )
        self.tray.show()

    def on_launch_desktop(self) -> bool:
        try:
            return not is_window_cloaked(int(self.winId()))
        except Exception:
            return True

    def dock_once(self) -> None:
        if not self.on_launch_desktop():
            return
        try:
            self.dock.place_once(self, self.frameGeometry().width())
            self.status.setText("已贴靠；窗口尺寸变化时不再强制跟随")
        except Exception as exc:
            self.status.setText(f"贴靠失败：{type(exc).__name__}")

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            if not self.on_launch_desktop():
                return
            self.show()
            self.raise_()
            self.activateWindow()

    def toggle_collapse(self) -> None:
        self.collapsed = not self.collapsed
        collapsed_width = int(self.config.get("collapsed_width", 46))
        self.list_widget.setVisible(not self.collapsed)
        self.status.setVisible(not self.collapsed)
        self.rebuild_button.setVisible(not self.collapsed)
        self.dock_button.setVisible(not self.collapsed)
        self.clear_button.setVisible(not self.collapsed)
        self.hide_button.setVisible(not self.collapsed)
        self.collapse_button.setText("▶" if self.collapsed else "◀")
        self.setMinimumWidth(collapsed_width if self.collapsed else 180)
        self.setMaximumWidth(collapsed_width if self.collapsed else 16777215)
        self.resize(
            collapsed_width
            if self.collapsed
            else int(self.config.get("sidebar_width", 280)),
            self.height(),
        )

    def rebuild(self) -> None:
        if not self.on_launch_desktop():
            return
        if self.busy:
            if self.worker_mode == "full" and isinstance(
                self.worker, ScanWorker
            ):
                self.worker.requestInterruption()
                self.rebuild_button.setEnabled(False)
                self.status.setText("正在停止重建并恢复原滚动位置…")
            else:
                self.pending_rebuild = True
                self.status.setText("已安排在当前操作结束后重建索引")
            return
        self.pending_rebuild = False
        self.busy = True
        self.worker_mode = "full"
        self.rebuild_button.setText("停止")
        self.rebuild_button.setEnabled(True)
        self.status.setText("正在重建：0% · 已发现 0 条；再次点击可停止")
        pause = float(self.config.get("scan_pause_ms", 120)) / 1000.0
        worker = ScanWorker("full", pause)
        worker.completed.connect(self._full_scan_done)
        worker.failed.connect(self._worker_failed)
        worker.cancelled.connect(self._scan_cancelled)
        worker.progress.connect(self._scan_progress)
        worker.finished.connect(self._worker_finished)
        self.worker = worker
        worker.start()

    def refresh_current(self) -> None:
        if not self.on_launch_desktop():
            return
        if self.busy:
            self.pending_probe = True
            return
        self.pending_probe = False
        self.busy = True
        self.worker_mode = "probe"
        worker = ScanWorker("probe")
        worker.completed.connect(self._probe_done)
        worker.failed.connect(self._worker_failed)
        worker.finished.connect(self._worker_finished)
        self.worker = worker
        worker.start()

    def refresh_visible(self) -> None:
        if (
            not self.on_launch_desktop()
            or time.monotonic() < self.suppress_refresh_until
        ):
            return
        if self.busy:
            self.pending_visible_refresh = True
            return
        self.visible_capture_timer.stop()
        self.pending_visible_refresh = False
        self.last_visible_capture_at = time.monotonic()
        self.busy = True
        self.worker_mode = "visible"
        worker = ScanWorker("visible")
        worker.completed.connect(self._visible_done)
        worker.failed.connect(self._worker_failed)
        worker.finished.connect(self._worker_finished)
        self.worker = worker
        worker.start()

    def accessibility_changed(self, immediate: bool = False) -> None:
        if (
            time.monotonic() >= self.suppress_refresh_until
            and self.on_launch_desktop()
        ):
            self.event_debounce.start()
            if immediate:
                self._request_visible_capture()

    def _request_visible_capture(self) -> None:
        if self.busy:
            self.pending_visible_refresh = True
            return
        elapsed = time.monotonic() - self.last_visible_capture_at
        minimum_interval = 0.10
        if elapsed >= minimum_interval:
            self.refresh_visible()
            return
        remaining_ms = max(1, int((minimum_interval - elapsed) * 1000))
        if not self.visible_capture_timer.isActive():
            self.visible_capture_timer.start(remaining_ms)

    def _scan_progress(self, percent: float, count: int) -> None:
        self.status.setText(
            f"正在重建：{percent:.0f}% · 已发现 {count} 条；再次点击可停止"
        )

    def _scan_cancelled(self) -> None:
        self.status.setText("已停止重建，并恢复到开始前的滚动位置")

    def _full_scan_done(self, index: ConversationIndex) -> None:
        if not index.questions:
            self.status.setText(
                "本次扫描未读取到用户提问，已保留原索引；请稍后重试"
            )
            return
        self.index = index
        self.pending_visible_refresh = False
        self.cache_dirty = False
        path = self.cache.save(index)
        LOGGER.info(
            "indexed conversation questions=%d fingerprint=%s",
            len(index.questions),
            index.fingerprint,
        )
        self.render()
        self.status.setText(
            f"已读取 {len(index.questions)} 条用户提问 · 精确跳转可用"
        )
        print(f"是否成功读取消息文本：{'是' if index.questions else '否'}")
        print("是否找到可编程滚动容器：是")
        print(f"当前检测到的用户提问数量：{len(index.questions)}")
        print("当前跳转模式：精确跳转（ScrollIntoView），失败时百分比回退")
        print(f"索引缓存：{path}")

    def _same_conversation(self, snapshot: ConversationSnapshot) -> bool:
        if self.index is None:
            return False
        if snapshot.window_title != self.index.window_title:
            return False
        if (
            snapshot.container_id
            and self.active_container_id
            and snapshot.container_id != self.active_container_id
        ):
            return False
        return (
            not snapshot.conversation_title
            or snapshot.conversation_title == self.index.conversation_title
        )

    def _merge_snapshot(
        self,
        snapshot: ConversationSnapshot,
        *,
        preserve_existing_position: bool = True,
    ) -> int:
        if self.index is None:
            return 0
        before = len(self.index.questions)
        self.index.questions = merge_questions(
            self.index.questions,
            snapshot.questions,
            preserve_existing_position=preserve_existing_position,
        )
        added = len(self.index.questions) - before
        if added:
            self.cache_dirty = True
            self.cache_save_timer.start()
        return added

    def _probe_done(self, snapshot: ConversationSnapshot) -> None:
        if snapshot.process_id:
            self.chatgpt_process_changed.emit(snapshot.process_id)
        if self._same_conversation(snapshot):
            if snapshot.container_id:
                self.active_container_id = snapshot.container_id
            added = self._merge_snapshot(snapshot)
            if added:
                self._append_items(len(self.index.questions) - added)
                self.status.setText(
                    f"已自动追加，当前 {len(self.index.questions)} 条用户提问"
                )
            return

        if not snapshot.conversation_title:
            self.status.setText("暂时无法确认当前对话，稍后重试")
            return
        self._flush_cache()
        cached = self.cache.load_for_conversation(
            snapshot.window_title, snapshot.conversation_title
        )
        if cached is None or cached.scroll_mode != CURRENT_SCROLL_MODE:
            self.index = None
            self.active_container_id = snapshot.container_id
            self.list_widget.clear()
            self.status.setText("索引缺失或格式已升级，准备后台重新建立…")
            self.pending_rebuild = True
            return

        self.index = cached
        self.active_container_id = snapshot.container_id
        self._merge_snapshot(snapshot)
        self.render()
        count, size = self.cache.stats()
        self.status.setText(
            f"已从磁盘缓存载入 {len(self.index.questions)} 条 · "
            f"缓存 {count} 个/{size / 1024:.0f} KiB"
        )

    def _visible_done(self, snapshot: ConversationSnapshot) -> None:
        if (
            self.index is None
            or not self.active_container_id
            or snapshot.container_id != self.active_container_id
        ):
            self.pending_probe = True
            return
        added = self._merge_snapshot(
            snapshot, preserve_existing_position=True
        )
        if added:
            self._append_items(len(self.index.questions) - added)
            self.status.setText(
                f"滚动中已捕获新提问，当前 {len(self.index.questions)} 条"
            )

    def _flush_cache(self) -> None:
        if self.cache_dirty and self.index is not None:
            self.cache.save(self.index)
            self.cache_dirty = False

    def _worker_failed(self, message: str) -> None:
        self.status.setText(f"无障碍访问失败：{message}")

    def _worker_finished(self) -> None:
        worker = self.sender()
        completed_mode = self.worker_mode
        if isinstance(worker, JumpWorker):
            self.active_jump_position = None
        if completed_mode == "full":
            self.pending_visible_refresh = False
        self.busy = False
        self.worker_mode = ""
        self.rebuild_button.setText("重建")
        self.rebuild_button.setEnabled(True)
        self.worker = None
        if isinstance(worker, QThread):
            worker.deleteLater()
        if self.pending_jump_position is not None:
            position = self.pending_jump_position
            self.pending_jump_position = None
            QTimer.singleShot(0, lambda: self._start_jump(position))
            return
        if self.pending_rebuild:
            self.pending_rebuild = False
            QTimer.singleShot(0, self.rebuild)
            return
        if self.pending_probe:
            self.pending_probe = False
            QTimer.singleShot(0, self.refresh_current)
            return
        if (
            self.pending_visible_refresh
            and completed_mode != "full"
            and time.monotonic() >= self.suppress_refresh_until
        ):
            self.pending_visible_refresh = False
            QTimer.singleShot(0, self._request_visible_capture)

    def render(self) -> None:
        self._clear_jump_highlight()
        self.list_widget.clear()
        if self.index is None:
            return
        self._append_items(0)

    def _append_items(self, start: int) -> None:
        if self.index is None:
            return
        for question in self.index.questions[start:]:
            item = QListWidgetItem(f"{question.number}. {question.summary}")
            item.setData(Qt.ItemDataRole.UserRole, question.number - 1)
            self.list_widget.addItem(item)

    def question_at(self, position) -> Question | None:
        if (
            self.index is None
            or not isinstance(position, int)
            or position < 0
            or position >= len(self.index.questions)
        ):
            return None
        return self.index.questions[position]

    def jump_to_item(self, item: QListWidgetItem) -> None:
        if self.index is None:
            return
        position = item.data(Qt.ItemDataRole.UserRole)
        if (
            not isinstance(position, int)
            or position < 0
            or position >= len(self.index.questions)
        ):
            return
        self._highlight_jump_item(item)
        if self.busy:
            if (
                self.active_jump_position == position
                and self.pending_jump_position is None
            ):
                self.status.setText(f"正在跳到第 {position + 1} 条…")
                return
            self.pending_jump_position = position
            self.status.setText(f"已接收第 {position + 1} 条跳转，等待当前操作结束…")
            return
        self._start_jump(position)

    def _start_jump(self, position: int) -> None:
        if (
            self.busy
            or self.index is None
            or position < 0
            or position >= len(self.index.questions)
        ):
            return
        item = self.list_widget.item(position)
        if item is not None:
            self._highlight_jump_item(item)
        self.busy = True
        self.worker_mode = "jump"
        self.active_jump_position = position
        self.event_debounce.stop()
        self.suppress_refresh_until = time.monotonic() + 1.5
        self.status.setText(f"正在跳到第 {position + 1} 条…")
        worker = JumpWorker(self.index.questions[position])
        worker.completed.connect(
            lambda mode: self._jump_done(position, mode)
        )
        worker.failed.connect(self._worker_failed)
        worker.finished.connect(self._worker_finished)
        self.worker = worker
        worker.start()

    def _jump_done(self, position: int, mode: str) -> None:
        self.last_jump_mode = mode
        self.suppress_refresh_until = time.monotonic() + 1.0
        self.status.setText(f"已跳到第 {position + 1} 条 · 模式：{mode}")
        self.highlight_timer.start(650)

    def _highlight_jump_item(self, item: QListWidgetItem) -> None:
        self._clear_jump_highlight()
        self.highlighted_item = item
        item.setBackground(self.palette().highlight())
        item.setForeground(self.palette().highlightedText())
        self.highlight_timer.start(1200)

    def _clear_jump_highlight(self) -> None:
        if self.highlighted_item is not None:
            try:
                self.highlighted_item.setData(
                    Qt.ItemDataRole.BackgroundRole, None
                )
                self.highlighted_item.setData(
                    Qt.ItemDataRole.ForegroundRole, None
                )
            except RuntimeError:
                pass
        self.highlighted_item = None

    def clear_data(self) -> None:
        if self.busy:
            self.status.setText("当前操作完成后才能清除索引")
            return
        result = QMessageBox.question(
            self,
            "清除全部索引数据",
            "确定删除本机 data/indexes 中的全部对话索引吗？",
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        count = self.cache.clear()
        self.cache_dirty = False
        self.cache_save_timer.stop()
        self.index = None
        self.active_container_id = ""
        self.list_widget.clear()
        self.status.setText(f"已清除 {count} 个本地索引文件")

    def quit_application(self) -> None:
        self._flush_cache()
        if isinstance(self.worker, ScanWorker):
            self.worker.requestInterruption()
        self.force_quit = True
        if self.config.get("restore_chatgpt_geometry_on_exit", True):
            self.dock.restore()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.force_quit:
            event.accept()
        else:
            event.ignore()
            self.hide()
            self.tray.showMessage(
                "ChatGPT 提问导航",
                "程序仍在系统托盘运行。右键托盘图标可退出。",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )


def configure_logging() -> None:
    logs = PROJECT_ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            RotatingFileHandler(
                logs / "app.log",
                maxBytes=1024 * 1024,
                backupCount=2,
                encoding="utf-8",
            ),
            logging.StreamHandler(),
        ],
    )


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def main() -> int:
    if platform.system() != "Windows":
        print("此构建仅实现当前检测到的 Windows 系统。", file=sys.stderr)
        return 2
    configure_logging()
    config = load_config()
    application = QApplication(sys.argv)
    application.setQuitOnLastWindowClosed(False)
    sidebar = Sidebar(config)
    sidebar.show()

    details = WindowsUIA().details()
    print(f"检测到的操作系统：{platform.platform()}")
    print(
        f"检测到的 ChatGPT 进程：{details.process_name} "
        f"(PID {details.process_id}, HWND {details.handle})"
    )
    current_elevated = process_is_elevated(__import__("os").getpid())
    target_elevated = process_is_elevated(details.process_id)
    if (
        current_elevated is not None
        and target_elevated is not None
        and current_elevated != target_elevated
    ):
        QMessageBox.warning(
            sidebar,
            "权限级别不同",
            "ChatGPT 与本工具的 Windows 权限级别不同。请保持两者使用相同权限运行。",
        )

    hotkeys = HotkeyThread(
        config["hotkeys"]["toggle"], config["hotkeys"]["rebuild"]
    )
    hotkeys.toggle_requested.connect(sidebar.toggle_visible)
    hotkeys.rebuild_requested.connect(sidebar.rebuild)
    hotkeys.registration_failed.connect(sidebar.status.setText)
    hotkeys.start()
    events = AccessibilityEventThread(details.process_id)

    def rebind_accessibility_events(process_id: int) -> None:
        nonlocal events
        if process_id <= 0 or process_id == events.process_id:
            return
        LOGGER.info(
            "ChatGPT process changed from %d to %d; rebinding events",
            events.process_id,
            process_id,
        )
        events.stop()
        events = AccessibilityEventThread(process_id)
        events.changed.connect(sidebar.accessibility_changed)
        events.start()

    sidebar.chatgpt_process_changed.connect(rebind_accessibility_events)
    events.changed.connect(sidebar.accessibility_changed)
    events.start()
    result = application.exec()
    if isinstance(sidebar.worker, QThread):
        if isinstance(sidebar.worker, ScanWorker):
            sidebar.worker.requestInterruption()
        sidebar.worker.wait(3000)
    events.stop()
    hotkeys.stop()
    if config.get("restore_chatgpt_geometry_on_exit", True):
        sidebar.dock.restore()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
