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


def test_next_scan_settles_a_spool_already_loaded():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    w.tray_loaded(A, now=5)
    assert w.scan(43, now=6) == Assign(42, A)
    assert w.armed == 43
    assert w.settle(now=20) is None
    w.tray_loaded(B, now=21)
    assert w.settle(now=31) == Assign(43, B)


def test_next_scan_reports_a_spool_not_loaded_yet():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    assert w.scan(43, now=268) == Expired(42)
    assert w.armed == 43
    assert w.expire(now=300) is None


def test_next_scan_reports_ambiguous_loads():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    w.tray_loaded(A, now=5)
    w.tray_loaded(B, now=6)
    assert w.scan(43, now=7) == Ambiguous(42, (A, B))


def test_rescanning_the_same_spool_keeps_its_load():
    w = LoadWindow(300, 10)
    assert w.scan(42, now=0) is None
    w.tray_loaded(A, now=5)
    assert w.scan(42, now=6) is None
    assert w.settle(now=15) == Assign(42, A)


def test_rescanning_the_same_spool_restarts_its_window():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    w.scan(42, now=100)
    assert w.expire(now=399) is None
    assert w.expire(now=400) == Expired(42)


def test_load_on_a_claimed_slot_is_not_for_the_armed_spool():
    w = LoadWindow(300, 10)
    w.slot_claimed(A, 42, now=0)
    w.scan(43, now=1)
    assert not w.tray_loaded(A, now=5)
    assert w.settle(now=20) is None
    assert w.armed == 43
    assert w.tray_loaded(B, now=21)


def test_slot_claim_ends_with_the_window():
    w = LoadWindow(300, 10)
    w.slot_claimed(A, 42, now=0)
    w.scan(43, now=299)
    assert w.tray_loaded(A, now=301)


def test_slot_claim_covers_flicker_after_the_load_but_not_a_later_swap():
    w = LoadWindow(300, 10)
    w.slot_claimed(A, 42, now=0)
    w.scan(43, now=1)
    assert not w.tray_loaded(A, now=5)
    assert not w.tray_loaded(A, now=15)
    assert w.tray_loaded(A, now=16)


def test_slot_claim_shortens_on_a_load_with_nothing_armed():
    w = LoadWindow(300, 10)
    w.slot_claimed(A, 42, now=0)
    assert not w.tray_loaded(A, now=5)
    w.scan(43, now=60)
    assert w.tray_loaded(A, now=61)


def test_slot_claim_does_not_block_the_spool_that_made_it():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    w.slot_claimed(A, 42, now=1)
    assert w.tray_loaded(A, now=5)
    assert w.settle(now=15) == Assign(42, A)


def test_withdrawn_load_no_longer_counts():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    w.tray_loaded(A, now=5)
    w.tray_withdrawn(A)
    assert w.settle(now=20) is None
    assert w.armed == 42
    w.tray_loaded(B, now=21)
    assert w.settle(now=30) is None
    assert w.settle(now=31) == Assign(42, B)


def test_withdrawing_one_of_two_loads_keeps_the_other():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    w.tray_loaded(A, now=5)
    w.tray_loaded(B, now=6)
    w.tray_withdrawn(A)
    assert w.settle(now=15) == Assign(42, B)
