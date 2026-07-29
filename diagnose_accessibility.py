"""Dump the live ChatGPT window's Microsoft UI Automation tree."""

from __future__ import annotations

import argparse
import ctypes
import json
import platform
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = PROJECT_ROOT / "diagnostics" / "accessibility-tree.txt"
DEFAULT_SUMMARY = PROJECT_ROOT / "diagnostics" / "summary.json"


def safe(callable_, default=""):
    try:
        value = callable_()
        return default if value is None else value
    except Exception as exc:  # UIA nodes can disappear while traversing.
        return f"<error:{type(exc).__name__}>"


def process_name(process_id: int) -> str:
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, process_id)
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return Path(buffer.value).name
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def find_chatgpt_window():
    from pywinauto import Desktop

    candidates = []
    for window in Desktop(backend="uia").windows():
        info = window.element_info
        detected_process_name = process_name(safe(lambda: info.process_id, 0))
        title = safe(window.window_text, "")
        if (
            "chatgpt" in str(detected_process_name).lower()
            or "chatgpt" in str(title).lower()
        ):
            rect = safe(lambda: info.rectangle, None)
            area = 0 if rect is None else max(0, rect.width()) * max(0, rect.height())
            candidates.append((area, window))
    if not candidates:
        raise RuntimeError("没有找到正在运行的 ChatGPT 主窗口")
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def pattern_names(element) -> list[str]:
    names = []
    iface_names = {
        "Invoke": "iface_invoke",
        "SelectionItem": "iface_selection_item",
        "Selection": "iface_selection",
        "Value": "iface_value",
        "Text": "iface_text",
        "Text2": "iface_text2",
        "Scroll": "iface_scroll",
        "ScrollItem": "iface_scroll_item",
        "ExpandCollapse": "iface_expand_collapse",
        "Toggle": "iface_toggle",
        "RangeValue": "iface_range_value",
        "Grid": "iface_grid",
        "GridItem": "iface_grid_item",
        "Table": "iface_table",
        "TableItem": "iface_table_item",
        "Window": "iface_window",
        "Transform": "iface_transform",
        "LegacyIAccessible": "iface_legacy_iaccessible",
    }
    for label, attr in iface_names.items():
        try:
            getattr(element, attr)
            names.append(label)
        except Exception:
            pass
    return names


def describe(element, depth: int, view: str) -> str:
    info = element.element_info
    rect = safe(lambda: info.rectangle, None)
    rect_text = (
        ""
        if rect is None
        else f"({rect.left},{rect.top},{rect.right},{rect.bottom})"
    )
    fields = {
        "view": view,
        "depth": depth,
        "role": safe(lambda: info.control_type),
        "name": safe(lambda: info.name),
        "value": safe(lambda: element.get_value()),
        "description": safe(lambda: info.rich_text),
        "automation_id": safe(lambda: info.automation_id),
        "class_name": safe(lambda: info.class_name),
        "framework_id": safe(lambda: info.framework_id),
        "process_id": safe(lambda: info.process_id),
        "runtime_id": safe(lambda: list(info.runtime_id)),
        "bounding_rectangle": rect_text,
        "enabled": safe(lambda: info.enabled),
        "visible": safe(lambda: info.visible),
        "patterns_actions": pattern_names(element),
    }
    return json.dumps(fields, ensure_ascii=False, default=str)


def dump_view(root, view: str, max_nodes: int) -> tuple[list[str], int]:
    lines = [f"=== UIA {view.upper()} VIEW ==="]
    count = 0

    def walk(node, depth: int) -> None:
        nonlocal count
        if count >= max_nodes:
            return
        count += 1
        lines.append(describe(node, depth, view))
        try:
            children = node.children()
        except Exception as exc:
            lines.append(
                json.dumps(
                    {"depth": depth + 1, "error": f"{type(exc).__name__}: {exc}"},
                    ensure_ascii=False,
                )
            )
            return
        for child in children:
            walk(child, depth + 1)
            if count >= max_nodes:
                break

    walk(root, 0)
    if count >= max_nodes:
        lines.append(f"<truncated at {max_nodes} nodes>")
    return lines, count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--max-nodes", type=int, default=12000)
    args = parser.parse_args()

    if platform.system() != "Windows":
        print("当前实现仅支持正在运行的 Windows 系统。", file=sys.stderr)
        return 2

    started = time.time()
    window = find_chatgpt_window()
    info = window.element_info
    header = {
        "os": platform.platform(),
        "process_id": info.process_id,
        "process_name": process_name(info.process_id),
        "window_title": safe(window.window_text),
        "window_handle": safe(lambda: info.handle),
        "window_rectangle": str(safe(lambda: info.rectangle)),
    }
    output_lines = [
        "# ChatGPT Accessibility Tree Diagnostic",
        json.dumps(header, ensure_ascii=False, default=str),
    ]
    counts = {}
    # pywinauto exposes control view directly. Raw/content traversal support varies;
    # descendants are queried separately so the report records all accessible nodes.
    for view in ("control", "raw", "content"):
        try:
            if view == "control":
                root = window
                lines, count = dump_view(root, view, args.max_nodes)
            else:
                descendants = window.descendants()
                lines = [f"=== UIA {view.upper()} VIEW (accessible descendants) ==="]
                for node in descendants[: args.max_nodes]:
                    lines.append(describe(node, 0, view))
                count = min(len(descendants), args.max_nodes)
            output_lines.extend(lines)
            counts[view] = count
        except Exception as exc:
            output_lines.append(f"=== UIA {view.upper()} VIEW ERROR: {exc!r} ===")
            counts[view] = 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(output_lines), encoding="utf-8")
    summary = {
        **header,
        "view_node_counts": counts,
        "elapsed_seconds": round(time.time() - started, 3),
        "output": str(args.output),
    }
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
