"""The decision itself: may this agent continue without its principal?

Everything here is pure. It takes a description of the situation and returns a
:class:`Decision`; it does not read a file, call a model, look at a clock or
touch a process. That is what makes the interesting part testable, and it is
also the only way the ordering below can be pinned by tests rather than by
hope - the ordering *is* the safety property.

The ladder, in order, and why the order is the design
----------------------------------------------------
1. **Is the agent even waiting?** A turn that finished cleanly needs no
   permission. Asking this first keeps the expensive and failure-prone steps
   off the common path.
2. **Is the request in a reserved zone?** Checked *before* any predictor is
   consulted, because no amount of agreement makes it acceptable to spend
   money or publish on someone's behalf. Deciding this deterministically also
   means the answer does not depend on a network call succeeding.
3. **Is there budget left in the streak?** Checked before consulting the
   panel: if the answer is no, consulting is wasted work, and - more
   importantly - a panel that is asked anyway will eventually be *believed*
   anyway.
4. **Does the panel agree, having actually answered?** Last, and the only
   probabilistic step. It can only ever *narrow* what the earlier steps
   allowed.

Each rung can only refuse. Nothing downstream re-opens a door that an earlier
rung closed, which is what lets you reason about this by reading four
functions instead of simulating the whole thing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from .quorum import Opinion, QuorumOutcome, QuorumResult, tally
from .streak import StreakState, may_proceed
from .zones import ZoneSet, default_zones

__all__ = [
    "Outcome", "Situation", "Decision", "decide", "is_waiting",
    "WAITING_MARKERS", "WAITING_PATTERNS",
]


class Outcome(str, Enum):
    """Why the decision came out the way it did.

    These strings are what land in the journal, so each one names a *distinct*
    cause. In particular ``LANES_SILENT`` is deliberately not folded into
    ``DISSENT``: the first means the machinery is broken, the second means it
    worked and counselled waiting. A system that files both under "no
    consensus" cannot tell an outage from caution, which is exactly how a dead
    mechanism once went unnoticed for seven weeks.
    """

    PROCEED = "proceed"
    NOT_WAITING = "not_waiting"
    RESERVED_ZONE = "reserved_zone"
    STREAK_EXHAUSTED = "streak_exhausted"
    DISSENT = "dissent"
    LANES_SILENT = "lanes_silent"
    UNDERSIZED_PANEL = "undersized_panel"


#: Phrases that mean the agent stopped to ask rather than stopped because it
#: was done.
#:
#: A miss here fails safe - the ladder exits at rung 1, nothing is authorised,
#: and the principal is asked exactly as they would have been without this
#: package. But it fails safe *silently*, and the journal then records
#: ``not_waiting`` for something that plainly was a question, which is the
#: kind of quietly-wrong telemetry that makes a mechanism impossible to tune.
#: The first version of this list held only fixed phrases - ``posso seguir``,
#: ``posso continuar`` - and so "posso publicar no canal?" and "qual tu
#: aprova?" both read as clean endings.
WAITING_MARKERS: tuple[str, ...] = (
    "shall i", "should i", "may i", "can i go ahead", "do you want me to",
    "let me know", "waiting for", "awaiting your", "confirm?", "ok to proceed",
    "aguardando", "me avisa", "confirma?", "dou o go",
)

#: The productive forms. These are patterns rather than fixed strings because
#: the question is open-class: it is ``posso <any verb>``, not a list of verbs
#: someone remembered to enumerate. Enumerating them is how the reserved-zone
#: check ends up never running on the exact requests it exists to catch.
WAITING_PATTERNS: tuple[str, ...] = (
    r"\bposso\s+\w+",                       # posso seguir / publicar / apagar / subir
    r"\bquer\s+que\s+eu\s+\w+",             # quer que eu faca
    r"\bqual\s+(tu|voc[êe]s?)\s+\w+",       # qual tu aprova / voce prefere
    r"\b(can|could|may)\s+i\s+\w+",         # can i publish / may i delete
    r"\bwhich\s+(one|version|option)\b",
    r"\bdo\s+you\s+want\b",
    # "awaiting approval" sem o "your": a lista fixa so tinha "awaiting your",
    # entao a forma mais comum de dizer que se esta esperando nao contava.
    r"\bawaiting\b",
    r"\?\s*$",                              # termina em pergunta, seja qual for
)


@dataclass(frozen=True)
class Situation:
    """Everything the decision depends on, gathered by the caller.

    Assembling this is the caller's job precisely because gathering it is the
    part that touches the world - transcripts, files, subprocesses. Keeping
    that outside means every rule below can be tested with a literal.
    """

    #: What the agent said at the end of its turn.
    agent_text: str
    #: The principal's last real message.
    #:
    #: Carried here for the journal and for callers that want the whole
    #: situation in one object. :func:`decide` does NOT read it: re-anchoring
    #: the streak is :func:`deputy.streak.next_state`'s job, and it has to
    #: happen *before* the streak reaches this dataclass. The field used to
    #: claim it was "used to anchor the streak", which was a description of
    #: something no code in this module does.
    principal_text: str = ""
    #: Current streak, normally loaded from a ledger.
    streak: StreakState = StreakState(count=0, anchor="")
    #: Panel answers. Empty means no panel was consulted.
    opinions: Sequence[Opinion] = ()
    #: Maximum consecutive self-authorisations.
    cap: int = 4
    #: How many predictors the panel requires.
    quorum_minimum: int = 2
    #: Reserved zones; the shipped set when omitted.
    zones: ZoneSet | None = None


@dataclass(frozen=True)
class Decision:
    """The verdict, plus everything needed to explain or audit it."""

    outcome: Outcome
    reason: str
    #: The instruction to inject when - and only when - proceeding.
    instruction: str = ""
    #: Reserved zone that held the request, when one did.
    zone: str = ""
    #: Panel result, when a panel was consulted.
    quorum: QuorumResult | None = None

    @property
    def may_proceed(self) -> bool:
        return self.outcome is Outcome.PROCEED

    def as_entry_fields(self) -> dict[str, object]:
        """The shape :mod:`deputy.journal` wants. Kept here so the two stay aligned."""
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "zone": self.zone,
            "instruction": self.instruction,
            "sources": tuple(o.source for o in (self.quorum.opinions if self.quorum else ())),
        }


def is_waiting(
    text: str,
    markers: Sequence[str] = WAITING_MARKERS,
    patterns: Sequence[str] = WAITING_PATTERNS,
) -> bool:
    """Whether the agent's closing text is asking for a decision.

    Only the last 600 characters are examined, and within that window a marker
    counts wherever it appears - there is no anchor at the end. So a turn that
    says "I wondered whether I should ask" three lines before finishing
    cleanly *will* read as waiting.

    That is a deliberate trade, not an oversight, and the earlier version of
    this docstring overstated it by claiming such narration was excluded. The
    cost of a false positive here is one wasted panel consultation; the cost of
    a false negative is that the reserved-zone check never runs on a request
    that was in fact asking. Erring toward "this is a question" is the cheap
    direction.

    Both a fixed-phrase list and a pattern list are consulted. The patterns
    carry the open-class forms - a question is ``posso <verb>`` for any verb,
    and no list of verbs stays complete.
    """
    tail = (text or "").strip().lower()[-600:]
    if any(m in tail for m in markers):
        return True
    return any(re.search(p, tail) for p in patterns)


def decide(situation: Situation) -> Decision:
    """Run the ladder. Each rung may only refuse."""
    zones = default_zones() if situation.zones is None else situation.zones

    # 1. Not waiting - nothing to authorise.
    if not is_waiting(situation.agent_text):
        return Decision(
            Outcome.NOT_WAITING,
            "the turn did not end asking for a decision",
        )

    # 2. Reserved zone - deterministic, and unappealable. Note this runs
    #    before the panel: consulting models about whether to spend the
    #    principal's money invites the answer to be treated as an input.
    hit = zones.first_match(situation.agent_text)
    if hit is not None:
        zone, pattern = hit
        return Decision(
            Outcome.RESERVED_ZONE,
            f"reserved for the principal ({zone.name}: {zone.why}); matched {pattern}",
            zone=zone.name,
        )

    # 3. Streak budget.
    allowed, why = may_proceed(situation.streak, situation.cap)
    if not allowed:
        return Decision(Outcome.STREAK_EXHAUSTED, why)

    # 4. The panel - the only probabilistic rung, and it comes last so it can
    #    only narrow what the deterministic rungs already permitted.
    result = tally(situation.opinions, minimum=situation.quorum_minimum)
    if result.outcome is QuorumOutcome.UNDERSIZED:
        return Decision(Outcome.UNDERSIZED_PANEL, result.explain(), quorum=result)
    if result.outcome is QuorumOutcome.LANES_SILENT:
        return Decision(Outcome.LANES_SILENT, result.explain(), quorum=result)
    if result.outcome is QuorumOutcome.DISSENT:
        return Decision(Outcome.DISSENT, result.explain(), quorum=result)

    return Decision(
        Outcome.PROCEED,
        result.explain(),
        instruction=result.instruction,
        quorum=result,
    )
