"""Which tray did the scanned spool go into? (pure logic)"""

from __future__ import annotations

from dataclasses import dataclass

from .model import SlotRef


@dataclass(frozen=True)
class Assign:
    spool_id: int
    slot: SlotRef


@dataclass(frozen=True)
class Ambiguous:
    spool_id: int
    slots: tuple[SlotRef, ...]


@dataclass(frozen=True)
class Expired:
    spool_id: int


class LoadWindow:
    """One armed spool; the trays that went empty -> loaded while it was armed.

    The first load starts a settle period; loads inside it make the result
    ambiguous instead of guessing.
    """

    def __init__(self, window_s: float, settle_s: float) -> None:
        self.window_s = window_s
        self.settle_s = settle_s
        self._claimed: dict[SlotRef, tuple[int, float]] = {}  # slot -> spool, valid until
        self._reset()

    def _reset(self) -> None:
        self._spool: int | None = None
        self._armed_at = 0.0
        self._first_at: float | None = None
        self._candidates: list[SlotRef] = []

    @property
    def armed(self) -> int | None:
        return self._spool

    def scan(self, spool_id: int, now: float) -> Assign | Ambiguous | Expired | None:
        """Arm `spool_id`. A different spool still armed is settled, not dropped:
        returns what to do with it. Rescanning the same spool restarts its window."""
        if spool_id == self._spool:
            self._armed_at = now
            return None
        previous = self._spool
        slots = tuple(self._candidates)
        self._reset()
        self._spool = spool_id
        self._armed_at = now
        if previous is None:
            return None
        if not slots:
            return Expired(previous)
        return Assign(previous, slots[0]) if len(slots) == 1 else Ambiguous(previous, slots)

    def cancel(self) -> None:
        self._reset()

    def slot_claimed(self, slot: SlotRef, spool_id: int, now: float) -> None:
        """`spool_id` was assigned to this empty slot by hand: the load that follows is that spool."""
        self._claimed[slot] = (spool_id, now + self.window_s)

    def tray_loaded(self, slot: SlotRef, now: float) -> bool:
        claim = self._claimed.get(slot)
        if claim is not None and now <= claim[1]:
            # The claim's load has arrived: it only has to outlast a flicker now, so a later swap counts.
            self._claimed[slot] = (claim[0], min(claim[1], now + self.settle_s))
            if claim[0] != self._spool:
                return False
        if self._spool is None:
            return False
        if self._first_at is None and now > self._armed_at + self.window_s:
            return False
        if self._first_at is None:
            self._first_at = now
        if slot not in self._candidates:
            self._candidates.append(slot)
        return True

    def tray_withdrawn(self, slot: SlotRef) -> None:
        """The load seen on `slot` was not the armed spool: it emptied again or read an RFID tag."""
        if slot in self._candidates:
            self._candidates.remove(slot)
            if not self._candidates:
                self._first_at = None

    def settle(self, now: float) -> Assign | Ambiguous | None:
        if self._spool is None or self._first_at is None or now < self._first_at + self.settle_s:
            return None
        spool, slots = self._spool, tuple(self._candidates)
        self._reset()
        return Assign(spool, slots[0]) if len(slots) == 1 else Ambiguous(spool, slots)

    def expire(self, now: float) -> Expired | None:
        if self._spool is not None and self._first_at is None and now >= self._armed_at + self.window_s:
            spool = self._spool
            self._reset()
            return Expired(spool)
        return None
