from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sys
from pathlib import Path
from typing import Sequence, TextIO

from .decide import Decision, Outcome, Situation, decide, is_waiting
from .journal import Entry, Journal
from .quorum import Opinion, Verdict, read_verdict, tally
from .streak import DEFAULT_CAP, StreakLedger, StreakState, may_proceed, next_state, record_proceed
from .zones import default_zones, scan

EXIT_PROCEED = 0
EXIT_HELD = 1
EXIT_USAGE = 2

JOURNAL_NAME = "journal.jsonl"
STREAK_NAME = "streak.json"


def _split_assignment(raw: str) -> tuple[str, str] | None:
    """Split a ``SOURCE=TEXT`` argument on the first equals sign.

    Args:
        raw: The raw argument string.

    Returns:
        A tuple of the source and the remaining text, or None when the
        string has no usable assignment form.
    """
    if "=" not in raw:
        return None
    source, _, text = raw.partition("=")
    source = source.strip()
    return (source, text) if source else None


def _parse_opinions(raw: list[str] | None) -> list[Opinion]:
    """Convert repeated ``--opinion SOURCE=TEXT`` values into Opinion objects.

    Args:
        raw: The raw argument strings, or None when none were given.

    Returns:
        A list of Opinion objects carrying a non-fault verdict.

    Raises:
        ValueError: When an argument does not have the expected form.
    """
    opinions: list[Opinion] = []
    for item in raw or []:
        split = _split_assignment(item)
        if split is None:
            raise ValueError(f"invalid opinion {item!r}; expected SOURCE=TEXT")
        source, text = split
        # read_verdict monta a Opinion inteira, incluindo a regra que importa:
        # texto vazio vira SILENT com uma falha declarada, nunca HOLD. Montar a
        # Opinion aqui a mao contornaria exatamente essa regra.
        opinions.append(read_verdict(source, text))
    return opinions


def _parse_faults(raw: list[str] | None) -> list[Opinion]:
    """Convert repeated ``--fault SOURCE=REASON`` values into fault opinions.

    Args:
        raw: The raw argument strings, or None when none were given.

    Returns:
        A list of Opinion objects flagged as faults.

    Raises:
        ValueError: When an argument does not have the expected form.
    """
    faults: list[Opinion] = []
    for item in raw or []:
        split = _split_assignment(item)
        if split is None:
            raise ValueError(f"invalid fault {item!r}; expected SOURCE=REASON")
        source, reason = split
        faults.append(Opinion(source=source, verdict=Verdict.SILENT, fault=reason))
    return faults


def _resolve_journal_path(args: argparse.Namespace) -> Path:
    """Resolve the journal path for a command.

    Args:
        args: The parsed command-line arguments.

    Returns:
        The explicit --journal path, or a default file under --home.
    """
    if args.journal is not None:
        return Path(args.journal)
    return Path(args.home) / JOURNAL_NAME


def _resolve_streak_path(args: argparse.Namespace) -> Path:
    """Resolve the streak ledger path for a command.

    Args:
        args: The parsed command-line arguments.

    Returns:
        The explicit --streak path, or a default file under --home.
    """
    if args.streak is not None:
        return Path(args.streak)
    return Path(args.home) / STREAK_NAME


def _now_iso() -> str:
    """Timestamp for a journal entry, in UTC."""
    return datetime.now(timezone.utc).isoformat()


def cmd_check(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Run a check on an agent's closing text.

    Builds the situation, decides the outcome, prints a human summary,
    appends exactly one journal entry, and advances the streak only when
    the outcome is PROCEED.

    Args:
        args: The parsed command-line arguments.
        stdout: The stream for normal output.
        stderr: The stream for diagnostics.

    Returns:
        EXIT_PROCEED when proceeding, EXIT_HELD for any other outcome, or
        EXIT_USAGE when an opinion or fault argument is malformed.
    """
    try:
        opinions = _parse_opinions(args.opinion) + _parse_faults(args.fault)
    except ValueError as exc:
        print(f"usage error: {exc}", file=stderr)
        return EXIT_USAGE

    cap = args.cap if args.cap is not None else DEFAULT_CAP
    minimum = args.min if args.min is not None else 2

    ledger = StreakLedger(_resolve_streak_path(args))
    # next_state antes de decidir: se o humano falou de novo, o contador zera e
    # o orcamento de autonomia recomeca. O rascunho passava o OUTCOME aqui, o
    # que ancoraria o contador num valor do proprio agente - autoancoragem, e o
    # teto deixaria de significar qualquer coisa.
    streak = next_state(ledger.load(), args.principal or "")

    situation = Situation(
        agent_text=args.text,
        principal_text=args.principal or "",
        opinions=opinions,
        streak=streak,
        cap=cap,
        quorum_minimum=minimum,
    )
    decision: Decision = decide(situation)

    print("outcome: " + decision.outcome.value, file=stdout)
    print("reason:  " + decision.reason, file=stdout)
    if decision.zone:
        print("zone:    " + decision.zone, file=stdout)

    if decision.may_proceed:
        streak = record_proceed(streak)
        # A instrucao so aparece quando ha autorizacao. Imprimi-la num hold
        # convidaria alguem a copiar e colar o que a banca *teria* dito.
        if decision.instruction:
            print("instruction: " + decision.instruction, file=stdout)
    ledger.save(streak)

    fields = decision.as_entry_fields()
    Journal(path=_resolve_journal_path(args)).append(
        Entry(
            ts=_now_iso(),
            outcome=str(fields["outcome"]),
            reason=str(fields["reason"]),
            zone=str(fields["zone"]),
            instruction=str(fields["instruction"]),
            streak=streak.count,
            sources=tuple(op.source for op in opinions),
        )
    )

    return EXIT_PROCEED if decision.outcome == Outcome.PROCEED else EXIT_HELD


def cmd_zones(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """List the known zones and their rationale.

    Args:
        args: The parsed command-line arguments; --text narrows the list
            to the zones that the text touches.
        stdout: The stream for normal output.
        stderr: The stream for diagnostics.

    Returns:
        EXIT_PROCEED on success.
    """
    zones = default_zones()
    text = args.text if args.text is not None else ""
    for zone in zones:
        touches = scan(text, zone) if text else []
        if text and not touches:
            continue
        why = f" [{', '.join(touches)}]" if touches else ""
        print(f"{zone.name}: {zone.why}{why}", file=stdout)
    return EXIT_PROCEED


def cmd_journal(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Print recent journal entries, one readable line each.

    Args:
        args: The parsed command-line arguments.
        stdout: The stream for normal output.
        stderr: The stream for diagnostics.

    Returns:
        EXIT_PROCEED on success.
    """
    journal = Journal(path=_resolve_journal_path(args))
    tail = args.tail if args.tail is not None else 10
    counts = journal.counts()
    print(f"total: {sum(counts.values()) if isinstance(counts, dict) else counts}", file=stdout)
    for entry in journal.tail(n=tail):
        zone = f" zone={entry.zone}" if entry.zone else ""
        print(f"{entry.ts} {entry.outcome} streak={entry.streak}{zone} :: {entry.reason}", file=stdout)
    return EXIT_PROCEED


def cmd_health(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Print the journal health summary.

    Args:
        args: The parsed command-line arguments.
        stdout: The stream for the health summary.
        stderr: The stream for diagnostics.

    Returns:
        EXIT_HELD when the summary contains the word "outage" so a cron
        job can alert on a dead panel, otherwise EXIT_PROCEED.
    """
    journal = Journal(path=_resolve_journal_path(args))
    window = args.window if args.window is not None else 20
    summary = journal.health(window)
    print(summary, file=stdout)
    return EXIT_HELD if "outage" in summary.lower() else EXIT_PROCEED


def cmd_streak(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Show the streak state, or reset it with --reset.

    Args:
        args: The parsed command-line arguments.
        stdout: The stream for normal output.
        stderr: The stream for diagnostics.

    Returns:
        EXIT_PROCEED on success.
    """
    ledger = StreakLedger(path=_resolve_streak_path(args))
    state = ledger.load()
    if args.reset:
        state = StreakState(count=0, anchor=None, trusted=True)
        ledger.save(state)
        print("streak reset.", file=stdout)
    print(
        json.dumps(
            {"count": state.count, "anchor": state.anchor, "trusted": state.trusted},
            indent=2,
        ),
        file=stdout,
    )
    return EXIT_PROCEED


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the deputy command line.

    Returns:
        A configured ArgumentParser with every subcommand registered.
    """
    parser = argparse.ArgumentParser(prog="deputy")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_paths(p: argparse.ArgumentParser) -> None:
        """Attach the shared path options to a subparser.

        Args:
            p: The subparser to extend.
        """
        p.add_argument("--home", default=".", help="defaults root for --journal and --streak")
        p.add_argument("--journal", default=None, help="path to the journal file")
        p.add_argument("--streak", default=None, help="path to the streak ledger file")

    check = sub.add_parser("check", help="evaluate an agent closing text")
    add_paths(check)
    check.add_argument("--text", required=True, help="agent closing text")
    check.add_argument("--principal", default=None, help="last human message")
    check.add_argument("--opinion", action="append", default=None, metavar="SOURCE=TEXT", help="panel opinion (repeatable)")
    check.add_argument("--fault", action="append", default=None, metavar="SOURCE=REASON", help="panel fault (repeatable)")
    check.add_argument("--cap", type=int, default=None, help="streak cap (default DEFAULT_CAP)")
    check.add_argument("--min", dest="min", type=int, default=None, help="minimum quorum")
    check.set_defaults(func=cmd_check)

    zones = sub.add_parser("zones", help="list zones and why they exist")
    add_paths(zones)
    zones.add_argument("--text", default=None, help="show only zones touched by this text")
    zones.set_defaults(func=cmd_zones)

    journal = sub.add_parser("journal", help="print recent journal entries")
    add_paths(journal)
    journal.add_argument("--tail", type=int, default=None, help="how many recent entries")
    journal.set_defaults(func=cmd_journal)

    health = sub.add_parser("health", help="print journal health; exit 1 on outage")
    add_paths(health)
    health.add_argument("--window", type=int, default=None, help="health window size")
    health.set_defaults(func=cmd_health)

    streak = sub.add_parser("streak", help="show or reset the streak ledger")
    add_paths(streak)
    streak.add_argument("--reset", action="store_true", help="write a fresh trusted state")
    streak.set_defaults(func=cmd_streak)

    return parser


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """Entry point for the deputy command line.

    Args:
        argv: Argument list without the program name; defaults to sys.argv.
        stdout: Writable stream for normal output; defaults to sys.stdout.
        stderr: Writable stream for diagnostics; defaults to sys.stderr.

    Returns:
        An exit code: 0 for proceed/success, 1 for a held outcome or an
        un-healthy journal, 2 for usage errors.
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args([str(a) for a in argv] if argv is not None else None)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    return int(args.func(args, out, err))


if __name__ == "__main__":
    sys.exit(main())
