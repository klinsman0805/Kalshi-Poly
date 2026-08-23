"""SIGTERM drain: systemd must not kill us between an exchange fill and the
ledger write that records it.

The window this protects is narrow but unrecoverable. _rehydrate trusts the
ledger absolutely and never asks the exchange what we hold, so a fill that
never reached the journal is a position nobody manages: never marked, never
exited, never settled.
"""
import threading
import time

import pytest

from modules import weather_exec as wx


@pytest.fixture(autouse=True)
def _reset_drain_state():
    """Drain state is module-global, so it leaks between tests without this."""
    wx._shutdown.clear()
    with wx._inflight:
        wx._inflight_n = 0
    yield
    wx._shutdown.clear()
    with wx._inflight:
        wx._inflight_n = 0


def test_shutting_down_is_false_until_asked():
    assert wx.shutting_down() is False


def test_begin_shutdown_sets_the_flag():
    wx.begin_shutdown(timeout=0.1)
    assert wx.shutting_down() is True


def test_drains_immediately_when_nothing_is_in_flight():
    t0 = time.monotonic()
    assert wx.begin_shutdown(timeout=5.0) is True
    assert time.monotonic() - t0 < 1.0, "should not wait when no order is open"


def test_uninterruptible_counts_a_call_in_flight():
    seen = {}

    @wx._uninterruptible
    def order():
        seen["n"] = wx._inflight_n

    order()
    assert seen["n"] == 1
    assert wx._inflight_n == 0, "counter must be released on the way out"


def test_counter_is_released_even_when_the_order_raises():
    @wx._uninterruptible
    def order():
        raise RuntimeError("exchange rejected")

    with pytest.raises(RuntimeError):
        order()
    assert wx._inflight_n == 0, "an exception must not leak an in-flight slot"


def test_shutdown_waits_for_an_order_already_running():
    started, release = threading.Event(), threading.Event()

    @wx._uninterruptible
    def slow_order():
        started.set()
        release.wait(5.0)

    t = threading.Thread(target=slow_order, daemon=True)
    t.start()
    assert started.wait(2.0)

    done = {}

    def drain():
        done["ok"] = wx.begin_shutdown(timeout=5.0)

    d = threading.Thread(target=drain, daemon=True)
    d.start()
    time.sleep(0.3)
    assert "ok" not in done, "drain returned while an order was still in flight"

    release.set()
    d.join(5.0)
    assert done.get("ok") is True
    t.join(2.0)


def test_shutdown_reports_failure_when_the_timeout_expires():
    """The caller must be able to tell 'drained' from 'gave up' — that
    distinction is the difference between a clean exit and a possible orphan."""
    release = threading.Event()

    @wx._uninterruptible
    def stuck_order():
        release.wait(10.0)

    t = threading.Thread(target=stuck_order, daemon=True)
    t.start()
    time.sleep(0.2)
    try:
        assert wx.begin_shutdown(timeout=0.5) is False
    finally:
        release.set()
        t.join(5.0)


def test_wrapper_preserves_identity_and_return_value():
    @wx._uninterruptible
    def place(a, b=2):
        """docstring survives"""
        return a + b

    assert place(1, b=3) == 4
    assert place.__name__ == "place"
    assert "docstring survives" in (place.__doc__ or "")


def test_concurrent_orders_each_hold_a_slot():
    peak = {"n": 0}
    release = threading.Event()
    lock = threading.Lock()

    @wx._uninterruptible
    def order():
        with lock:
            peak["n"] = max(peak["n"], wx._inflight_n)
        release.wait(5.0)

    ts = [threading.Thread(target=order, daemon=True) for _ in range(3)]
    for t in ts:
        t.start()
    time.sleep(0.3)
    release.set()
    for t in ts:
        t.join(5.0)
    assert peak["n"] == 3
    assert wx._inflight_n == 0


def test_on_refresh_is_inert_while_draining():
    """Draining must stop NEW activity. Orders already running are protected by
    the decorator; this stops the poll loop opening a fresh one into a process
    that is about to exit."""
    ex = wx.WeatherExecutor.__new__(wx.WeatherExecutor)
    called = {"n": 0}
    ex._mark_open = lambda rows: called.__setitem__("n", called["n"] + 1)

    wx.begin_shutdown(timeout=0.1)
    ex.on_refresh([{"city": "Nowhere"}])
    assert called["n"] == 0, "on_refresh did work while shutting down"
