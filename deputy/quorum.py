"""Agreement among independent predictors, and the failure that looks like it.

A quorum here is not a vote count. It is a claim that several independent
opinions were *obtained* and *agreed*, and the whole value of the mechanism
rests on the first half of that claim being true.

The bug this module exists to make impossible
---------------------------------------------
A production deployment of this idea consulted three models and required
consensus before letting an agent proceed without its principal. Its decision
log, over seven weeks:

    46 opportunities - 26 correct refusals - 20 "no consensus" - 0 proceeds

"No consensus" reads like caution. It was not. Every one of those twenty
records held three empty strings: the model identifiers had reached end of
life months earlier, and the fallback path invoked a shell script in a way the
host platform rejects. Each failure landed in a broad ``except`` that returned
``""``. An empty answer was then counted as "did not vote to proceed", which is
arithmetically identical to a cautious dissent - so a completely dead
mechanism produced a log indistinguishable from a working, conservative one,
and nobody looked for seven weeks.

The lesson is not "handle errors". It is that **silence and dissent must never
share a name**. A predictor that fails to answer is a broken dependency; a
predictor that answers "wait" is the system working. Collapsing the two makes
an outage invisible precisely when it matters.

So:

* :class:`Opinion` distinguishes PROCEED, HOLD and SILENT, and SILENT carries
  the reason it was silent;
* any SILENT predictor voids the quorum with its own outcome name,
  ``LANES_SILENT``, which is greppable and alertable;
* consensus is unanimous among those consulted, not a majority - a single HOLD
  is enough to wait, because waiting is cheap and being wrong is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

__all__ = [
    "Verdict",
    "Opinion",
    "QuorumOutcome",
    "QuorumResult",
    "tally",
    "read_verdict",
]


class Verdict(str, Enum):
    """What one predictor concluded."""

    PROCEED = "proceed"
    HOLD = "hold"
    #: No usable answer. This is a fault in the predictor, not an opinion
    #: about the request, and it is never counted as either side.
    SILENT = "silent"


class QuorumOutcome(str, Enum):
    """What the panel as a whole produced."""

    #: Everyone consulted answered, and everyone said proceed.
    CONSENSUS_PROCEED = "consensus_proceed"
    #: Everyone answered; at least one said hold.
    DISSENT = "dissent"
    #: At least one predictor produced no usable answer. Distinct from DISSENT
    #: on purpose - this one means "go fix your lanes", not "the panel was
    #: careful". Conflating them is the bug this package is named after.
    LANES_SILENT = "lanes_silent"
    #: Fewer predictors were configured than the panel requires.
    UNDERSIZED = "undersized"


@dataclass(frozen=True)
class Opinion:
    """One predictor's answer, or its failure to give one."""

    source: str
    verdict: Verdict
    text: str = ""
    #: Why the predictor was silent - a timeout, a dead model identifier, a
    #: launcher that the platform refused. Required when silent, because
    #: "silent for an unknown reason" is how the original outage stayed
    #: invisible; the log has to say *which* dependency broke.
    fault: str = ""

    def __post_init__(self) -> None:
        if self.verdict is Verdict.SILENT and not self.fault:
            raise ValueError(
                f"predictor {self.source!r} is SILENT with no fault recorded; "
                "an unexplained silence is indistinguishable from caution, "
                "which is the failure this type exists to prevent"
            )
        if self.verdict is not Verdict.SILENT and self.fault:
            raise ValueError(
                f"predictor {self.source!r} answered {self.verdict.value} but also "
                "recorded a fault; a fault means no usable answer"
            )
        if self.verdict is Verdict.PROCEED and not self.text.strip():
            raise ValueError(
                f"predictor {self.source!r} voted PROCEED with empty text; the "
                "predicted instruction is the payload, and an empty one would "
                "be injected downstream as if the principal had said nothing"
            )

    @property
    def answered(self) -> bool:
        return self.verdict is not Verdict.SILENT


@dataclass(frozen=True)
class QuorumResult:
    """The panel's outcome, with everything needed to explain it later."""

    outcome: QuorumOutcome
    opinions: tuple[Opinion, ...]
    instruction: str = ""

    @property
    def may_proceed(self) -> bool:
        return self.outcome is QuorumOutcome.CONSENSUS_PROCEED

    @property
    def silent(self) -> tuple[Opinion, ...]:
        return tuple(o for o in self.opinions if not o.answered)

    @property
    def answered(self) -> tuple[Opinion, ...]:
        return tuple(o for o in self.opinions if o.answered)

    def explain(self) -> str:
        """One line fit for a log or a terminal."""
        if self.outcome is QuorumOutcome.UNDERSIZED:
            return f"undersized panel: {len(self.opinions)} predictor(s) configured"
        if self.outcome is QuorumOutcome.LANES_SILENT:
            faults = ", ".join(f"{o.source}: {o.fault}" for o in self.silent)
            return f"{len(self.silent)}/{len(self.opinions)} predictors gave no answer ({faults})"
        if self.outcome is QuorumOutcome.DISSENT:
            holds = ", ".join(o.source for o in self.opinions if o.verdict is Verdict.HOLD)
            return f"held by {holds}"
        return f"unanimous across {len(self.opinions)} predictors"


def _pick_instruction(proceeds: Sequence[Opinion]) -> str:
    """The instruction to carry forward when the panel agrees.

    The median-length answer, not the longest. Choosing the longest rewards
    whichever model was most verbose, and reasoning models will happily emit
    several paragraphs of deliberation before the one-line answer - in the
    original deployment the first real proceed injected the model's entire
    chain of thought as if it were the principal speaking. The median is a
    cheap, order-stable way to skip both the monosyllable and the essay.
    """
    ordered = sorted(proceeds, key=lambda o: (len(o.text), o.source))
    return ordered[len(ordered) // 2].text.strip()


def tally(opinions: Sequence[Opinion], *, minimum: int = 2) -> QuorumResult:
    """Combine opinions into a single outcome.

    ``minimum`` is how many predictors must be *configured*. It is not a
    majority threshold: agreement must be unanimous among everyone consulted.
    A panel of three where one says hold waits, because the cost of waiting is
    one round-trip and the cost of proceeding wrongly is unbounded.
    """
    ops = tuple(opinions)
    if len(ops) < minimum:
        return QuorumResult(QuorumOutcome.UNDERSIZED, ops)

    if any(not o.answered for o in ops):
        return QuorumResult(QuorumOutcome.LANES_SILENT, ops)

    if any(o.verdict is Verdict.HOLD for o in ops):
        return QuorumResult(QuorumOutcome.DISSENT, ops)

    return QuorumResult(
        QuorumOutcome.CONSENSUS_PROCEED, ops, _pick_instruction(ops)
    )


#: Ways a predictor says no. Matched as whole words ANYWHERE in the opening of
#: the answer, not as a prefix.
#:
#: Prefix matching on a 24-character head was the first attempt, and it read
#: "please do not proceed", "I'd suggest we wait" and "let's hold off until
#: tomorrow" as PROCEED - an explicit refusal counted as an authorisation,
#: which is the worst direction for this particular mistake to run. Searching
#: the opening instead will sometimes read "go ahead, no need to wait" as a
#: hold; that costs one round-trip, and is the direction to err in.
_HOLD_WORDS = (
    "wait", "hold", "hold on", "hold off", "stop", "ask", "don't", "do not",
    "not yet", "check with", "espera", "esperar", "aguarda", "aguardar",
    "n[aã]o\\s+publique", "pergunta",
)
#: How much of the answer counts as "the opening". A refusal is stated early;
#: a mention of waiting in the fourth paragraph is usually narration.
_HOLD_WINDOW = 160


def read_verdict(source: str, raw: str, *, fault: str = "") -> Opinion:
    """Turn a predictor's raw output into an :class:`Opinion`.

    Empty output is SILENT, never HOLD. That single line is the whole fix: the
    original code ran the equivalent of ``if not text or text == "wait"`` and
    so filed a broken lane under the same heading as a careful one.

    A caller that already knows the lane failed - a non-zero exit, a timeout -
    should pass ``fault`` and not rely on emptiness to carry the meaning.
    """
    if fault:
        return Opinion(source=source, verdict=Verdict.SILENT, fault=fault)

    text = (raw or "").strip()
    if not text:
        return Opinion(
            source=source,
            verdict=Verdict.SILENT,
            fault="empty output (no exit status reported)",
        )

    head = text.lower()[:_HOLD_WINDOW]
    if any(re.search(r"\b" + w + r"\b", head) for w in _HOLD_WORDS):
        return Opinion(source=source, verdict=Verdict.HOLD, text=text)

    return Opinion(source=source, verdict=Verdict.PROCEED, text=text)
