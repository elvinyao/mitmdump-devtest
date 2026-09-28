from functools import partial

import pytest

from fault_engine.config import Config
from fault_engine.journal import Journal
from fault_engine.plan import compile_plan


def start(journal, request_id, **overrides):
    fields = dict(
        service="orders",
        rule="retry",
        scope="run-1",
        method="GET",
        ordinal=1,
        action="respond",
        sampled=True,
    )
    fields.update(overrides)
    journal.start(request_id, **fields)


def test_journal_keeps_arrival_order_and_terminal_result_once():
    now = [10.0]
    journal = Journal(3, clock=lambda: now[0])
    start(journal, "first")
    now[0] = 11.0
    start(journal, "second", ordinal=2)
    journal.finish("second", "response_prepared", status=200)
    now[0] = 12.0
    journal.mark_upstream("first")
    journal.finish("first", "reset_after")
    journal.finish("first", "transport_error", status=502)
    start(journal, "first")
    rows = journal.page()["requests"]
    assert [r["request_id"] for r in rows] == ["first", "second"]
    assert rows[0]["status"] is None
    assert rows[0]["outcome"] == "reset_after"
    assert rows[0]["duration_seconds"] == 2.0
    assert rows[0]["upstream_received"] is True
    assert rows[1]["duration_seconds"] == 0.0
    assert set(rows[0]) == {
        "id",
        "request_id",
        "service",
        "rule",
        "scope",
        "method",
        "ordinal",
        "action",
        "sampled",
        "status",
        "outcome",
        "started_at",
        "duration_seconds",
        "upstream_received",
    }


def test_verify_exact_count_status_order_and_monotonic_arrival_gaps():
    now = [10.0]
    journal = Journal(3, clock=lambda: now[0])
    checkpoint = journal.checkpoint
    for i, status in enumerate([503, 503, 200]):
        start(journal, str(i), ordinal=i + 1)
        journal.finish(str(i), "response_prepared", status=status)
        now[0] += 0.25
    expected = partial(journal.verify, after=checkpoint, count=3, statuses=[503, 503, 200])
    assert expected(min_interval_seconds=0.25)["matched"] is True
    assert expected(min_interval_seconds=0.3)["matched"] is False
    assert journal.verify(count=2)["matched"] is False
    assert journal.verify(count=3, statuses=[200, 503, 503])["matched"] is False
    assert journal.verify(count=0, scope="other")["matched"] is True


def test_pending_or_disabled_never_passes():
    journal = Journal(2)
    start(journal, "pending")
    result = journal.verify(count=1)
    assert result["complete"] is False
    assert result["matched"] is False
    assert result["pending"] == 1
    journal.finish("pending", "cancelled")
    assert journal.verify(count=1)["matched"] is True
    disabled = Journal(0)
    start(disabled, "ignored")
    disabled.finish("ignored", "response_prepared", status=200)
    assert disabled.page()["requests"] == []
    assert disabled.verify(count=0)["matched"] is False
    assert disabled.verify(count=0)["complete"] is False


def test_eviction_clear_and_inflight_finish_cannot_fabricate_complete_history():
    journal = Journal(2)
    original = journal.checkpoint
    for i in range(3):
        start(journal, str(i))
        journal.finish(str(i), "response_prepared", status=503)
    assert journal.verify(after=original, count=2)["complete"] is False
    assert journal.verify(after=original, count=2)["matched"] is False
    # Even a filter that does not match an evicted record cannot prove history.
    assert journal.verify(after=original, count=0, scope="other")["matched"] is False
    assert len(journal.page()["requests"]) == 2
    start(journal, "inflight")
    checkpoint = journal.clear()
    journal.finish("inflight", "response_prepared", status=200)
    assert journal.verify(after=checkpoint, count=0)["matched"] is True
    assert journal.verify(after=original, count=0)["complete"] is False
    start(journal, "next")
    assert journal.page(after=checkpoint)["requests"][0]["id"] == 5


def test_pages_filter_with_and_and_continue_without_repeating_rows():
    journal = Journal(9)
    for i, scope in enumerate(["a", "b", "a", "a"]):
        start(journal, str(i), scope=scope)
        journal.finish(str(i), "response_prepared", status=200)
    first = journal.page(service="orders", rule="retry", scope="a", limit=2)
    assert [r["id"] for r in first["requests"]] == [1, 3]
    assert first["complete"] is True
    second = journal.page(scope="a", after=first["next_cursor"])
    assert [r["id"] for r in second["requests"]] == [4]
    assert "next_cursor" not in second
    assert first["checkpoint"] == second["checkpoint"] == journal.checkpoint
    # Returned data cannot mutate stored observations.
    first["requests"][0]["status"] = 418
    assert journal.verify(count=4, statuses=[200] * 4)["matched"] is True


@pytest.mark.parametrize("cursor", ["garbage", "", "a:1", "x" * 1000])
def test_malformed_cursor_is_rejected(cursor):
    with pytest.raises(ValueError):
        Journal(1).page(after=cursor)


def test_foreign_and_future_cursors_rejected():
    journal = Journal(1)
    with pytest.raises(ValueError):
        journal.page(after=Journal(1).checkpoint)
    with pytest.raises(ValueError):
        journal.page(after=journal.checkpoint.rsplit(":", 1)[0] + ":99")


def test_limits_compile_into_plan():
    config = Config.model_validate(
        {
            "services": [{"id": "orders", "port": 8080, "upstream": "http://localhost"}],
            "limits": {"journal_capacity": 0},
        }
    )
    assert compile_plan(config).limits.journal_capacity == 0
    with pytest.raises(ValueError):
        Journal(-1)
    with pytest.raises(ValueError):
        Journal(100001)
