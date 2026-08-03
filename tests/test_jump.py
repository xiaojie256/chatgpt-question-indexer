from types import SimpleNamespace

from chatgpt_question_indexer.models import Question
from chatgpt_question_indexer.windows_uia import (
    WindowsUIA,
    _accumulate_questions,
    _intersects,
    _nearby_indexes,
    _pair_signature,
    _scan_step,
    _transient_end_jump,
)


class FakeElement:
    def __init__(
        self,
        role: str = "Group",
        name: str = "",
        children: list["FakeElement"] | None = None,
        rectangle=(0, 0, 100, 100),
    ):
        self.element_info = SimpleNamespace(
            control_type=role,
            name=name,
            class_name="",
            runtime_id=(),
            rectangle=SimpleNamespace(
                left=rectangle[0],
                top=rectangle[1],
                right=rectangle[2],
                bottom=rectangle[3],
            ),
        )
        self._children = children or []

    def children(self):
        return self._children


def question(number: int = 1) -> Question:
    return Question(
        text="目标问题",
        summary="目标问题",
        number=number,
        element_id="",
        rectangle=(0, 0, 100, 100),
        scroll_percent=50.0,
        text_hash="target",
    )


def test_nearby_indexes_prioritizes_exact_ordinal():
    assert _nearby_indexes(5, 10, radius=2) == [5, 4, 6, 3, 7]
    assert _nearby_indexes(0, 3, radius=3) == [0, 1, 2]
    assert _nearby_indexes(99, 3, radius=1) == [2, 1]


def test_rectangle_intersection_excludes_offscreen_messages():
    viewport = (100, 100, 500, 500)
    assert _intersects((120, 120, 200, 200), viewport)
    assert not _intersects((120, -200, 200, 90), viewport)
    assert not _intersects((500, 120, 600, 200), viewport)


def test_scan_step_keeps_overlap_for_small_viewports():
    assert _scan_step(8.3) < 8.3
    assert _scan_step(8.3) == 8.3 * 0.65
    assert _scan_step(0.1) == 0.5
    assert _scan_step(100) == 25


def test_pair_signature_changes_with_virtualized_position():
    first = FakeElement(rectangle=(10, 10, 20, 20))
    second = FakeElement(rectangle=(10, 30, 20, 40))

    before = _pair_signature([(FakeElement(), first)])
    after = _pair_signature([(FakeElement(), second)])

    assert before != after


def test_scan_accumulator_updates_overlap_without_rebuilding_order():
    existing = [question(1)]
    existing[0].text_hash = "first"
    positions = {"first": 0}
    updated = question(1)
    updated.text_hash = "first"
    updated.scroll_percent = 25
    new_question = question(2)
    new_question.text_hash = "second"

    added = _accumulate_questions(
        existing, positions, [updated, new_question]
    )

    assert added == 1
    assert [item.text_hash for item in existing] == ["first", "second"]
    assert existing[0].scroll_percent == 25


def test_mid_scan_bottom_report_is_treated_as_transient():
    assert _transient_end_jump(70, 100)
    assert not _transient_end_jump(99, 100)
    assert not _transient_end_jump(70, 70)


def test_first_text_anchor_uses_first_leaf_in_reading_order():
    first = FakeElement("Text", "问题开头")
    second = FakeElement("Text", "后续内容")
    bubble = FakeElement(children=[FakeElement(children=[first]), second])

    assert WindowsUIA._first_text_anchor(bubble) is first


def test_snapshot_ignores_offscreen_accessibility_nodes(monkeypatch):
    marker = FakeElement("Text", "You said:")
    visible = FakeElement(rectangle=(20, 20, 80, 80))
    offscreen = FakeElement(rectangle=(20, -100, 80, -20))
    container = FakeElement(rectangle=(0, 0, 100, 100))
    pairs = [(marker, visible), (marker, offscreen)]

    monkeypatch.setattr(WindowsUIA, "_message_pairs", lambda _container: pairs)
    nodes = WindowsUIA._snapshot(container)

    assert len(nodes) == 2
    assert nodes[1].rectangle == (20, 20, 80, 80)


def test_message_pair_read_retries_transient_virtualization_failure():
    marker = FakeElement("Text", "You said:")
    marker.element_info.class_name = "sr-only select-none"
    bubble = FakeElement("Group")

    class FlakyContainer:
        attempts = 0

        def children(self):
            self.attempts += 1
            if self.attempts == 1:
                raise KeyError(None)
            return [marker, bubble]

    container = FlakyContainer()
    pairs = WindowsUIA._message_pairs(container)

    assert pairs == [(marker, bubble)]
    assert container.attempts == 2


def test_message_pair_allows_many_attachments_before_bubble():
    marker = FakeElement("Text", "你说：")
    marker.element_info.class_name = "sr-only select-none"
    attachments = [FakeElement("Button", f"file-{index}.cpp") for index in range(6)]
    bubble = FakeElement("Group")
    container = FakeElement(children=[marker, *attachments, bubble])

    assert WindowsUIA._message_pairs(container) == [(marker, bubble)]


def test_resilient_scroll_reacquires_invalid_container(monkeypatch):
    stale = object()
    fresh = object()
    calls = []
    uia = WindowsUIA()

    def set_percent(container, percent):
        calls.append((container, percent))
        if container is stale:
            raise RuntimeError("stale")

    monkeypatch.setattr(uia, "set_scroll_percent", set_percent)
    monkeypatch.setattr(uia, "find_thread_container", lambda: fresh)

    result = uia._set_scroll_percent_resilient(stale, 42.0)

    assert result is fresh
    assert calls == [(stale, 42.0), (fresh, 42.0)]


def test_live_message_checks_expected_ordinal_before_other_candidates(monkeypatch):
    pairs = [
        (FakeElement("Text", "You said:"), FakeElement())
        for _ in range(10)
    ]
    calls = []
    target = question(number=6)
    uia = WindowsUIA()

    monkeypatch.setattr(uia, "_message_pairs", lambda _container: pairs)
    monkeypatch.setattr(uia, "scroll_percent", lambda _container: 50.0)

    def matches(_marker, bubble, _question, _percent):
        index = next(i for i, pair in enumerate(pairs) if pair[1] is bubble)
        calls.append(index)
        return index == 5

    monkeypatch.setattr(uia, "_matches_question", matches)
    result = uia._find_live_message(FakeElement(), target)

    assert result == pairs[5]
    assert calls == [5]
