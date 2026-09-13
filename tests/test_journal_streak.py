"""Tests for the deputy journal and streak modules.

These tests verify the journaling system's resilience against corruption
and the streak ledger's ability to track consecutive proceedings. This
matters because deputy agents must recover gracefully from partial writes
during crashes and must maintain accurate operational state across restarts
to enforce rate limits and safety caps.
"""

import json
import pytest
from pathlib import Path

from deputy.journal import (
    Entry,
    Journal,
    JournalCorrupt,
    SUCCESS_OUTCOME,
    DEFAULT_FAULT_OUTCOMES,
)
from deputy.streak import (
    StreakState,
    StreakLedger,
    next_state,
    may_proceed,
    record_proceed,
    DEFAULT_CAP,
)


class TestEntrySerialization:
    """Entry serialization must survive round-trips to ensure audit log integrity."""

    def test_entry_round_trip_preserves_all_fields(self):
        """Verify that an Entry survives serialization without data loss.

        Round-trip fidelity matters because journal entries are the primary
        audit trail for debugging agent behavior; losing fields would corrupt
        post-mortem analysis.
        """
        original = Entry(
            ts="2024-01-15T10:00:00",
            outcome=SUCCESS_OUTCOME,
            reason="deploy successful",
            zone="production",
            instruction="deploy --production",
            streak=3,
            sources=("api", "webhook"),
        )
        json_line = original.to_json()
        restored = Entry.from_json(json_line)

        assert restored.ts == original.ts
        assert restored.outcome == original.outcome
        assert restored.reason == original.reason
        assert restored.zone == original.zone
        assert restored.instruction == original.instruction
        assert restored.streak == original.streak
        assert restored.sources == original.sources

    def test_from_json_rejects_garbage_input(self):
        """Ensure corrupted lines raise JournalCorrupt rather than returning bad data.

        Explicit failure on garbage matters because silent data corruption in
        journals leads to incorrect streak calculations and false health reports.
        """
        with pytest.raises(JournalCorrupt):
            Entry.from_json("not valid json {")
        with pytest.raises(JournalCorrupt):
            Entry.from_json('{"missing": "required_fields"}')
        with pytest.raises(JournalCorrupt):
            Entry.from_json("")


class TestJournalDurability:
    """The journal must handle partial corruption without losing intact entries."""

    def test_corrupt_lines_are_skipped_but_counted(self, tmp_path):
        """Verify read_with_faults skips bad lines but tracks them for monitoring.

        Counting skipped lines matters because operators need to know when
        disk corruption or concurrent write issues are occurring, even if the
        journal remains readable.
        """
        journal_path = tmp_path / "corrupt.jsonl"
        journal = Journal(journal_path)

        good_entry = Entry(
            ts="2024-01-15T10:00:00",
            outcome=SUCCESS_OUTCOME,
            reason="test entry",
        )
        journal.append(good_entry)

        # Inject corruption
        with open(journal_path, "a", encoding="utf-8") as f:
            f.write("this is not json\n")

        entries, fault_count = journal.read_with_faults()
        assert len(entries) == 1
        assert entries[0].reason == "test entry"
        assert fault_count == 1

    def test_read_works_despite_corrupt_lines(self, tmp_path):
        """Ensure read() ignores corrupt lines and returns valid entries.

        Journal resilience matters because a single bit-flip or partial write
        should not render the entire operational history inaccessible.
        """
        journal_path = tmp_path / "mixed.jsonl"
        journal = Journal(journal_path)

        entry1 = Entry(
            ts="2024-01-15T10:00:00",
            outcome=SUCCESS_OUTCOME,
            reason="first",
        )
        entry2 = Entry(
            ts="2024-01-15T11:00:00",
            outcome=SUCCESS_OUTCOME,
            reason="second",
        )
        journal.append(entry1)

        # Inject corruption between entries
        with open(journal_path, "a", encoding="utf-8") as f:
            f.write("corrupt data here\n")

        journal.append(entry2)
        entries = journal.read()
        assert len(entries) == 2
        assert entries[0].reason == "first"
        assert entries[1].reason == "second"

    def test_append_is_append_only(self, tmp_path):
        """Verify that appending never modifies existing journal content.

        Append-only behavior matters for forensic integrity; if earlier entries
        could change, we could not trust the journal for compliance or debugging.
        """
        journal_path = tmp_path / "append_only.jsonl"
        journal = Journal(journal_path)

        entry1 = Entry(
            ts="2024-01-15T10:00:00",
            outcome=SUCCESS_OUTCOME,
            reason="original entry",
        )
        journal.append(entry1)
        first_line_content = journal_path.read_text(encoding="utf-8")

        entry2 = Entry(
            ts="2024-01-15T11:00:00",
            outcome=SUCCESS_OUTCOME,
            reason="new entry",
        )
        journal.append(entry2)

        all_content = journal_path.read_text(encoding="utf-8")
        lines = all_content.strip().split("\n")
        assert len(lines) == 2
        assert lines[0] in first_line_content
        assert "original entry" in lines[0]
        assert "new entry" in lines[1]


class TestJournalAnalytics:
    """Journal analytics drive operational decisions and alerting."""

    def test_counts_aggregates_by_outcome(self, tmp_path):
        """Verify counts() properly tallies outcomes for dashboard display.

        Accurate outcome counting matters because operators rely on these
        aggregates to detect systemic failures versus isolated incidents.
        """
        journal = Journal(tmp_path / "counts.jsonl")
        journal.append(
            Entry(ts="2024-01-15T10:00:00", outcome=SUCCESS_OUTCOME, reason="ok1")
        )
        journal.append(
            Entry(ts="2024-01-15T10:01:00", outcome=SUCCESS_OUTCOME, reason="ok2")
        )

        fault_outcome = next(iter(DEFAULT_FAULT_OUTCOMES))
        journal.append(
            Entry(ts="2024-01-15T10:02:00", outcome=fault_outcome, reason="fail1")
        )

        counts = journal.counts()
        assert counts[SUCCESS_OUTCOME] == 2
        assert counts[fault_outcome] == 1

    def test_health_detects_outage_on_zero_success_window(self, tmp_path):
        """Verify health() reports 'outage' when no successes exist in window.

        Outage detection matters because a complete absence of successful
        operations indicates a systemic failure requiring immediate alert,
        distinct from gradual performance degradation.
        """
        journal = Journal(tmp_path / "outage.jsonl")
        fault_outcome = next(iter(DEFAULT_FAULT_OUTCOMES))

        for i in range(5):
            journal.append(
                Entry(
                    ts=f"2024-01-15T10:{i:02d}:00",
                    outcome=fault_outcome,
                    reason=f"failure {i}",
                )
            )

        health_status = journal.health(window=50)
        assert "outage" in health_status.lower()

    def test_health_does_not_report_outage_on_healthy_window(self, tmp_path):
        """Verify health() does not report outage when successes are present.

        Avoiding false outage alerts matters because false positives erode
        trust in monitoring and cause unnecessary on-call escalations.
        """
        journal = Journal(tmp_path / "healthy.jsonl")
        journal.append(
            Entry(ts="2024-01-15T10:00:00", outcome=SUCCESS_OUTCOME, reason="good")
        )

        health_status = journal.health(window=50)
        assert "outage" not in health_status.lower()

    def test_health_handles_empty_journal(self, tmp_path):
        """Verify health() does not crash on empty journals.

        Empty journal handling matters because newly initialized agents must
        not crash when queried before their first operation completes.
        """
        journal = Journal(tmp_path / "empty.jsonl")
        health_status = journal.health(window=50)
        assert isinstance(health_status, str)


class TestStreakLogic:
    """Streak logic enforces safety caps and tracks operational continuity."""

    def test_next_state_keeps_count_on_same_message(self):
        """Verify next_state increments count for repeated identical messages.

        Streak continuity matters for rate limiting; if every edit reset the
        count, we could not enforce 'max N consecutive operations' policies.
        """
        current = StreakState(count=5, anchor="deploy to prod", trusted=True)
        # The anchor is a digest of the message, not the message. Two reasons:
        # a ledger on disk should not hold a copy of what the principal wrote,
        # and a digest is fixed-size however long the message was. So the
        # anchor is derived here rather than compared to the raw text.
        same_anchor = next_state(StreakState(count=0, anchor=""), "deploy to prod").anchor
        current = StreakState(count=6, anchor=same_anchor, trusted=True)

        new = next_state(current, "  deploy to prod  \n")

        assert new.count == 6, "whitespace is not a new instruction; the budget must not reset"
        assert new.anchor == same_anchor

    def test_next_state_resets_on_new_anchor(self):
        """Verify next_state resets count when the operation type changes.

        Resetting on new anchors matters because switching tasks should clear
        rate-limit counters; otherwise unrelated operations would be throttled.
        """
        old_anchor = next_state(StreakState(count=0, anchor=""), "deploy to prod").anchor
        current = StreakState(count=5, anchor=old_anchor, trusted=True)

        new = next_state(current, "rollback now")

        assert new.count == 0, "a real new instruction re-anchors everything"
        assert new.anchor != old_anchor
        assert new.anchor == next_state(StreakState(count=0, anchor=""), "rollback now").anchor

    def test_may_proceed_refuses_when_cap_is_zero(self):
        """Verify may_proceed refuses when cap is zero (disabled).

        Explicit disabled state matters because zero must mean 'operations
        disabled' rather than 'unlimited operations' for safety-critical
        shutdown scenarios.
        """
        state = StreakState(count=0, anchor="test", trusted=True)
        allowed, reason = may_proceed(state, cap=0)
        assert not allowed
        assert "disabled" in reason.lower()
        assert "unlimited" not in reason.lower()

    def test_may_proceed_refuses_untrusted_state(self):
        """Verify may_proceed refuses when state is untrusted.

        Refusing untrusted state matters because corrupt ledgers might
        under-count operations; proceeding could violate safety limits.
        """
        state = StreakState(count=1, anchor="test", trusted=False)
        allowed, reason = may_proceed(state, cap=10)
        assert not allowed

    def test_record_proceed_increments_count(self):
        """Verify record_proceed increments the streak counter.

        Incrementing the counter matters because this is the mechanism by
        which the ledger tracks progress toward rate-limit caps.
        """
        state = StreakState(count=3, anchor="test", trusted=True)
        new_state = record_proceed(state)
        assert new_state.count == 4
        assert new_state.anchor == "test"
        assert new_state.trusted == state.trusted


class TestStreakLedgerPersistence:
    """Streak ledger persistence must survive restarts and corruption."""

    def test_save_and_load_round_trip(self, tmp_path):
        """Verify StreakLedger preserves state across save/load cycles.

        Persistence round-trip matters because agents must resume streak
        counting after restarts without losing rate-limit state.
        """
        ledger = StreakLedger(tmp_path / "ledger.json")
        original = StreakState(count=7, anchor="important-op", trusted=True)
        ledger.save(original)

        loaded = ledger.load()
        assert loaded.count == 7
        assert loaded.anchor == "important-op"
        assert loaded.trusted is True

    def test_load_missing_file_returns_default_trusted_state(self, tmp_path):
        """Verify loading a missing ledger returns a clean default state.

        Missing-file defaulting matters because first-run initialization must
        not fail; agents should start with empty streaks on new environments.
        """
        ledger = StreakLedger(tmp_path / "nonexistent.json")
        state = ledger.load()
        assert state.count == 0
        assert state.trusted is True

    def test_load_corrupt_file_returns_untrusted_state(self, tmp_path):
        """Verify loading corrupted data marks state as untrusted.

        Corruption detection matters because damaged ledgers might contain
        stale or incorrect counts; distrusting them prevents limit violations.
        """
        path = tmp_path / "corrupt.json"
        path.write_text("{ invalid json !", encoding="utf-8")
        ledger = StreakLedger(path)
        state = ledger.load()
        assert state.trusted is False
