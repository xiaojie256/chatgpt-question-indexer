from chatgpt_question_indexer.parser import (
    AccessibleNode,
    merge_questions,
    parse_direct_children,
    summarize,
)


def node(role, name="", element_id="", children=None, class_name=""):
    return AccessibleNode(
        role=role,
        name=name,
        element_id=element_id,
        rectangle=(1, 2, 101, 42),
        class_name=class_name,
        children=children or [],
    )


def test_observed_marker_sequence_only_indexes_user_messages():
    children = [
        node("Text", "You said:", "label-1", class_name="sr-only select-none"),
        node(
            "Group",
            element_id="bubble-1",
            children=[
                node("Text", "第一条中文提问"),
                node("Text", "，包含第二段。"),
            ],
        ),
        node("Text", "ChatGPT said:"),
        node("Group", children=[node("Text", "这是助手回答，不应建立索引。")]),
        node("Text", "You said:", "label-2", class_name="sr-only select-none"),
        node(
            "Group",
            element_id="bubble-2",
            children=[node("Text", "Second question")],
        ),
    ]
    questions = parse_direct_children(children, 37.5)
    assert [question.text for question in questions] == [
        "第一条中文提问 ，包含第二段。",
        "Second question",
    ]
    assert [question.number for question in questions] == [1, 2]
    assert all(question.scroll_percent == 37.5 for question in questions)
    assert questions[0].element_id == "bubble-1"
    assert questions[0].next_feature == questions[1].text_hash[:16]


def test_merge_deduplicates_across_virtualized_pages():
    page = [
        node("Text", "You said:", class_name="sr-only select-none"),
        node("Group", element_id="one", children=[node("Text", "同一条问题")]),
    ]
    first = parse_direct_children(page, 0)
    second = parse_direct_children(page, 50)
    merged = merge_questions(first, second)
    assert len(merged) == 1
    assert merged[0].scroll_percent == 50


def test_probe_merge_preserves_scanned_scroll_position():
    page = [
        node("Text", "You said:", class_name="sr-only select-none"),
        node("Group", element_id="one", children=[node("Text", "同一条问题")]),
    ]
    scanned = parse_direct_children(page, 12.5)
    visible_probe = parse_direct_children(page, 100)
    merged = merge_questions(
        scanned,
        visible_probe,
        preserve_existing_position=True,
    )

    assert merged[0].scroll_percent == 12.5
    assert merged[0].element_id == "one"


def test_summary_uses_fifty_characters():
    value = "问" * 60
    assert summarize(value) == "问" * 50 + "…"
