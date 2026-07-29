import os
import time

from chatgpt_question_indexer.cache import IndexCache
from chatgpt_question_indexer.models import ConversationIndex, Question


def make_index(number: int, title: str = "对话") -> ConversationIndex:
    text = f"问题-{number}"
    return ConversationIndex(
        fingerprint=f"fingerprint-{number}",
        window_title="ChatGPT",
        conversation_title=f"{title}-{number}",
        questions=[
            Question(
                text=text,
                summary=text,
                number=1,
                element_id=f"id-{number}",
                rectangle=(0, 0, 10, 10),
                scroll_percent=50,
                text_hash=f"hash-{number}",
            )
        ],
        scroll_mode="exact",
        updated_at="2026-07-29T00:00:00+00:00",
    )


def test_cache_loads_by_conversation_without_rescan(tmp_path):
    cache = IndexCache(tmp_path, max_conversations=10)
    index = make_index(1)
    cache.save(index)
    loaded = cache.load_for_conversation(
        index.window_title, index.conversation_title
    )
    assert loaded is not None
    assert loaded.fingerprint == index.fingerprint
    assert loaded.questions[0].text == "问题-1"


def test_cache_prunes_oldest_by_count(tmp_path):
    cache = IndexCache(tmp_path, max_conversations=2)
    for number in range(3):
        cache.save(make_index(number))
        time.sleep(0.01)
    count, _size = cache.stats()
    assert count == 2
    assert cache.load("fingerprint-0") is None


def test_cache_prunes_expired_files(tmp_path):
    cache = IndexCache(tmp_path, max_conversations=10, retention_days=1)
    index = make_index(1)
    path = cache.save(index)
    old = time.time() - 3 * 86400
    os.utime(path, (old, old))
    assert cache.prune() == 1
    assert cache.load(index.fingerprint) is None


def test_clear_preserves_empty_catalog(tmp_path):
    cache = IndexCache(tmp_path)
    cache.save(make_index(1))
    assert cache.clear() == 1
    assert cache.stats() == (0, 0)
    assert (tmp_path / "catalog.json").exists()
