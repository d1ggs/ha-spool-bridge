from custom_components.spool_bridge.assignment import Ambiguous, Assign, Expired, LoadWindow
from custom_components.spool_bridge.model import SlotRef

A, B = SlotRef("ams", 0, 1), SlotRef("ams", 1, 2)


def test_single_load_assigns_after_settle():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    assert w.tray_loaded(A, now=20)
    assert w.settle(now=25) is None
    assert w.settle(now=30) == Assign(42, A)
    assert w.armed is None


def test_two_loads_are_ambiguous():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    w.tray_loaded(A, now=20)
    w.tray_loaded(B, now=22)
    assert w.settle(now=31) == Ambiguous(42, (A, B))


def test_load_without_scan_ignored():
    w = LoadWindow(300, 10)
    assert not w.tray_loaded(A, now=1)
    assert w.settle(now=100) is None


def test_load_after_window_ignored_and_expires():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    assert not w.tray_loaded(A, now=301)
    assert w.expire(now=301) == Expired(42)
    assert w.armed is None


def test_expire_not_while_settling():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    w.tray_loaded(A, now=299)
    assert w.expire(now=305) is None
    assert w.settle(now=309) == Assign(42, A)


def test_rescan_replaces():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    w.tray_loaded(A, now=5)
    w.scan(43, now=6)
    assert w.settle(now=20) is None  # candidates reset by the new scan
    w.tray_loaded(B, now=21)
    assert w.settle(now=31) == Assign(43, B)
