"""Append-only NDJSON decision journal for an agent that sometimes acts alone.

This module exists because of a simple failure pattern: a decision mechanism
produced many decisions over a long period, none of them a "proceed", and every
failure was filed under a name that looked like caution rather than an outage.
The journal is the only reason such an outage is discoverable after the fact.

The journal is deliberately boring: one JSON object per line, append-only,
byte-exact even on Windows, tolerant of corrupt lines, and loud about the
pathological case where caution has silently become a cover for a dead panel.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# Fraction of a fault outcome in the health window above which the summary
# calls the situation an outage rather than caution.
FAULT_SHARE_ALARM: float = 0.4

# Absolute number of consecutive fault outcomes in the health window at or
# above which the summary calls the situation an outage regardless of share.
FAULT_STREAK_ALARM: int = 5

# The canonical "success" outcome. A window with entries but zero of these is
# always called out explicitly, because that is the failure mode that went
# unnoticed in the incident that motivated this module.
SUCCESS_OUTCOME: str = "proceed"

# Default fault outcomes. Outcomes containing these substrings are treated as
# the mechanism failing to answer rather than as principled refusals.
# Os nomes vem de quorum.QuorumOutcome. "lanes_silent" e o importante: e o outcome
# que separa "a banca nao respondeu" de "a banca discordou". Se os dois dividissem
# um nome, health() nao teria como gritar - foi assim que 7 semanas de apagao
# passaram por prudencia.
DEFAULT_FAULT_OUTCOMES: tuple[str, ...] = (
    "lanes_silent", "silent", "undersized", "error", "timeout", "unavailable",
)


class JournalCorrupt(Exception):
    """Raised when a journal line cannot be parsed into an :class:`Entry`."""


@dataclass(frozen=True)
class Entry:
    """One decision, one line of NDJSON.

    Attributes:
        ts: ISO-8601 UTC timestamp of the decision.
        outcome: What was decided, e.g. "proceed", "refuse", or a fault name.
        reason: Why, in the mechanism's own words.
        zone: Optional scope the decision applied to.
        instruction: Optional standing instruction that was active.
        streak: Consecutive identical outcomes up to and including this entry.
        sources: Inputs the decision drew on, in any order.
    """

    ts: str
    outcome: str
    reason: str
    zone: str = ""
    instruction: str = ""
    streak: int = 0
    sources: tuple[str, ...] = ()

    def to_json(self) -> str:
        """Serialize this entry to one compact NDJSON line.

        Keys are sorted and non-ASCII characters are kept as-is, so the
        emitted line is deterministic for a given entry.

        Returns:
            The entry as a single-line JSON string without a trailing newline.
        """
        return json.dumps(
            {
                "ts": self.ts,
                "outcome": self.outcome,
                "reason": self.reason,
                "zone": self.zone,
                "instruction": self.instruction,
                "streak": self.streak,
                "sources": list(self.sources),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, line: str) -> "Entry":
        """Parse one NDJSON line back into an entry.

        Args:
            line: A single line, with or without a trailing newline.

        Returns:
            The parsed entry.

        Raises:
            JournalCorrupt: If the line is not valid JSON, is not an object,
                lacks the required string fields, or has the wrong types.
        """
        try:
            raw = json.loads(line.strip())
        except (json.JSONDecodeError, ValueError) as exc:
            raise JournalCorrupt(f"not valid JSON: {line!r: <200}"[:200]) from exc
        if not isinstance(raw, dict):
            raise JournalCorrupt(f"expected a JSON object, got {type(raw).__name__}")
        for key in ("ts", "outcome", "reason"):
            if not isinstance(raw.get(key), str):
                raise JournalCorrupt(f"missing or non-string required field {key!r}")
        for key in ("zone", "instruction"):
            if key in raw and not isinstance(raw[key], str):
                raise JournalCorrupt(f"field {key!r} must be a string")
        streak = raw.get("streak", 0)
        if not isinstance(streak, int) or isinstance(streak, bool):
            raise JournalCorrupt("field 'streak' must be an int")
        sources = raw.get("sources", [])
        if not isinstance(sources, (list, tuple)) or not all(
            isinstance(source, str) for source in sources
        ):
            raise JournalCorrupt("field 'sources' must be a list of strings")
        return cls(
            ts=raw["ts"],
            outcome=raw["outcome"],
            reason=raw["reason"],
            zone=raw.get("zone", ""),
            instruction=raw.get("instruction", ""),
            streak=streak,
            sources=tuple(sources),
        )


@dataclass
class Journal:
    """An append-only NDJSON journal at a given path.

    Attributes:
        path: Where the journal lives on disk.
        clock: Zero-argument callable returning the current time; injectable so
            tests never touch the real clock.
    """

    path: Path
    clock: Callable[[], datetime] = field(
        default=lambda: datetime.now(timezone.utc)
    )

    def _iso(self) -> str:
        """Return the current timestamp as ISO-8601 UTC.

        Returns:
            The clock's current time normalised to UTC and ISO-formatted.
        """
        moment = self.clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat()

    def append(self, entry: Entry) -> None:
        """Append one entry as exactly one NDJSON line.

        Parent directories are created as needed. The file is opened in append
        mode with UTF-8 encoding and ``newline=""`` so the bytes on disk stay
        byte-exact NDJSON regardless of platform.

        Args:
            entry: The entry to append; written verbatim from ``to_json()``.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(entry.to_json() + "\n")

    def read(self, limit: int | None = None) -> tuple[Entry, ...]:
        """Read entries, skipping but corrupt lines entirely.

        Corrupt lines are silently skipped here; use :meth:`read_with_faults`
        if their count matters.

        Args:
            limit: Maximum number of valid entries to return, from the start.
                ``None`` means no limit.

        Returns:
            The valid entries in file order.
        """
        entries, _ = self.read_with_faults(limit=limit)
        return entries

    def read_with_faults(
        self, limit: int | None = None
    ) -> tuple[tuple[Entry, ...], int]:
        """Read entries and report how many lines were corrupt.

        Args:
            limit: Maximum number of valid entries to return, from the start.
                ``None`` means no limit. Corrupt lines encountered anywhere in
                the scan are still counted.

        Returns:
            A pair ``(entries, corrupt_count)`` in file order.
        """
        if not self.path.exists():
            return (), 0
        collected: list[Entry] = []
        corrupt = 0
        with self.path.open("r", encoding="utf-8", newline="") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    entry = Entry.from_json(line)
                except JournalCorrupt:
                    corrupt += 1
                    continue
                if limit is None or len(collected) < limit:
                    collected.append(entry)
        return tuple(collected), corrupt

    def tail(self, n: int) -> tuple[Entry, ...]:
        """Return the last ``n`` valid entries.

        Args:
            n: How many entries to take from the end; ``n <= 0`` yields none.

        Returns:
            The trailing entries in file order.
        """
        if n <= 0:
            return ()
        entries, _ = self.read_with_faults()
        return entries[-n:]

    def counts(self) -> dict[str, int]:
        """Count entries by outcome over the whole journal.

        Returns:
            A mapping from outcome name to how many entries carry it.
        """
        tally: dict[str, int] = {}
        for entry in self.read():
            tally[entry.outcome] = tally.get(entry.outcome, 0) + 1
        return tally

    def health(self, window: int = 50) -> str:
        """Summarize the last ``window`` entries in one loud human line.

        This is the point of the module. The summary always states the window
        size and counts, and it explicitly names the pathological case: entries
        exist but the success outcome never appears, and/or a fault outcome is
        dominant or has a long streak. In that case it says "this is an
        outage, not caution" rather than letting caution-flavoured wording
        cover for a mechanism that is not answering.

        Args:
            window: How many trailing entries to inspect.

        Returns:
            A single line describing the window and any alarm conditions.
        """
        entries = self.tail(window)
        if not entries:
            return "0 decisions in the journal yet - no signal either way"

        total = len(entries)
        tally: dict[str, int] = {}
        for entry in entries:
            tally[entry.outcome] = tally.get(entry.outcome, 0) + 1

        success_count = tally.get(SUCCESS_OUTCOME, 0)
        parts = [
            f"{total} decisions",
            f"{success_count} {SUCCESS_OUTCOME}" + ("s" if success_count != 1 else ""),
        ]
        alarms: list[str] = []

        worst_fault_name = ""
        worst_fault_count = 0
        for outcome, count in sorted(tally.items(), key=lambda item: (-item[1], item[0])):
            if outcome == SUCCESS_OUTCOME:
                continue
            parts.append(f"{count} {outcome}")
            if self._is_fault(outcome) and count > worst_fault_count:
                worst_fault_name = outcome
                worst_fault_count = count

        if success_count == 0:
            alarms.append(
                f"ZERO {SUCCESS_OUTCOME} outcomes in this window - "
                "nothing has succeeded here"
            )

        if worst_fault_count:
            share = worst_fault_count / total
            if share >= FAULT_SHARE_ALARM or worst_fault_count >= FAULT_STREAK_ALARM:
                if success_count == 0:
                    run = worst_fault_count
                else:
                    run = self._trailing_run(entries, worst_fault_name)
                alarms.append(
                    f"the panel has not answered in {run} attempts; "
                    "this is an outage, not caution"
                )

        summary = ", ".join(parts)
        if alarms:
            summary += " - " + "; ".join(alarms)
        return summary

    @staticmethod
    def _is_fault(outcome: str) -> bool:
        """Decide whether an outcome names a failure rather than a refusal.

        Args:
            outcome: The outcome string to classify.

        Returns:
            True if the outcome contains any known fault marker.
        """
        lowered = outcome.lower()
        return any(marker in lowered for marker in DEFAULT_FAULT_OUTCOMES)

    @staticmethod
    def _trailing_run(entries: tuple[Entry, ...], outcome: str) -> int:
        """Count how many entries end the window with the same outcome.

        Args:
            entries: Window entries in file order.
            outcome: The outcome whose trailing streak to measure.

        Returns:
            The length of the trailing run of ``outcome``.
        """
        run = 0
        for entry in reversed(entries):
            if entry.outcome != outcome:
                break
            run += 1
        return run
