"""Which decisions an agent may never take on its principal's behalf.

This is the safety core. The rest of the package is about *whether* the
principal needs to be asked; this module is about the decisions where the
answer is always yes, however confident anyone is.

The matching is deliberately blunt and deliberately over-inclusive, because
the two errors are not symmetric:

* a **false positive** costs one round-trip - the agent waits, the principal
  says "go", work continues;
* a **false negative** means the agent spent money, published to a live
  account, or deleted something *while believing it had consent*.

So the stems here are wide (``publish\\w*``, not ``publish(ed|ing)``). That
width is a design decision, and it was paid for: a production deployment of
this idea matched two inflections of "publish" but not the noun form, and the
request "may I go ahead with the publication?" walked straight through the
deterministic layer. It was caught only because the probabilistic layer
downstream happened to abstain that day. In a fail-closed design, being saved
by the layer that is allowed to be wrong is not being saved.

Patterns are given in English and Portuguese, because the deployment this came
from is bilingual. Where a single stem already spans both languages it is used
once rather than twice, which is less to keep in sync.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

__all__ = ["Zone", "ZoneSet", "DEFAULT_ZONES", "default_zones", "scan"]


@dataclass(frozen=True)
class Zone:
    """A named class of decision reserved for the principal.

    ``why`` is not documentation garnish: it is what gets shown to the
    principal and written to the journal when a request is held. A zone whose
    reason cannot be stated in one sentence is usually two zones.
    """

    name: str
    why: str
    patterns: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.patterns:
            raise ValueError(f"zone {self.name!r} has no patterns; it would never match")
        for p in self.patterns:
            try:
                re.compile(p)
            except re.error as exc:  # pragma: no cover - guards construction
                raise ValueError(
                    f"zone {self.name!r} has an invalid pattern {p!r}: {exc}"
                ) from exc

    def matches(self, text: str) -> str | None:
        """Return the pattern that fired, or ``None``.

        Returning the *pattern* rather than a bool is what makes a hold
        auditable later. "Held by zone spend" is a claim; "held by zone spend,
        pattern ``\\bbuy\\w*\\b``, in this sentence" is evidence - and evidence is
        what lets someone tune a zone instead of switching it off.
        """
        for p in self.patterns:
            if re.search(p, text, re.IGNORECASE):
                return p
        return None


@dataclass(frozen=True)
class ZoneSet:
    """An ordered collection of reserved zones."""

    zones: tuple[Zone, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        names = [z.name for z in self.zones]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate zone names: {dupes}")

    def __iter__(self):
        return iter(self.zones)

    def __len__(self) -> int:
        return len(self.zones)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(z.name for z in self.zones)

    def first_match(self, text: str) -> tuple[Zone, str] | None:
        """The first reserved zone the text falls into, with the pattern that fired."""
        for z in self.zones:
            hit = z.matches(text)
            if hit is not None:
                return z, hit
        return None

    def with_zone(self, zone: Zone) -> "ZoneSet":
        return ZoneSet(self.zones + (zone,))

    def without(self, *names: str) -> "ZoneSet":
        """Drop zones by name.

        Deployments legitimately differ: an agent with no budget and no
        publishing rights has no use for those zones, and a zone that can never
        apply only trains the reader to skim past holds. Removing one is an
        explicit, greppable act. Silently never matching is not.

        Unknown names raise rather than being ignored, because a typo in a
        removal list is indistinguishable from a zone that quietly stopped
        being enforced.
        """
        drop = set(names)
        unknown = sorted(drop - set(self.names))
        if unknown:
            raise KeyError(f"no such zone(s): {unknown}")
        return ZoneSet(tuple(z for z in self.zones if z.name not in drop))

    @classmethod
    def of(cls, zones: Iterable[Zone]) -> "ZoneSet":
        return cls(tuple(zones))


# The shipped set: decisions people do not want an autonomous process making
# for them - not because the process is likely to be wrong, but because being
# wrong is unrecoverable.
DEFAULT_ZONES = ZoneSet.of([
    Zone(
        name="spend",
        why="money leaves an account and does not come back",
        patterns=(
            r"\bspend\w*\b", r"\bpay(ing|ment|s|ed)?\b", r"\bbuy\w*\b", r"\bpurchas\w*\b",
            r"\bbill(ing|ed)?\b", r"\bcharge[ds]?\b", r"\bsubscrib\w*\b",
            r"\bupgrade\s+(the\s+)?plan\b", r"\bcredits?\b", r"\bquota\s+increase\b",
            # "run payroll" move mais dinheiro do que quase tudo que esta zona
            # pega, e nao casava com nada: \bpay(ing|ment|s|ed)?\b exige que a
            # palavra ACABE ali.
            r"\bpayroll\b", r"\bpaycheck\w*\b", r"\bfolha\s+de\s+pagamento\b",
            r"\brent\w*\b", r"\bprovision\w*\b", r"\bgpu\b", r"\binstance\b", r"\bcluster\b",
            r"\bgast\w*\b", r"\bpagar\b", r"\bpagamento\w*\b", r"\bpago\b", r"\bcompra\w*\b", r"\bcusto\b",
        ),
    ),
    Zone(
        name="publish",
        why="content reaches an audience and may be cached or indexed even if deleted",
        patterns=(
            r"\bpublish\w*\b", r"\bpublica\w*\b", r"\bpost\w*\b", r"\bdeploy\w*\b",
            r"\brelease\w*\b", r"\bgo\s+live\b", r"\bmerge\s+to\s+(main|master)\b",
            r"\bsend\s+(the\s+)?(email|message|dm|invite|reply)\b", r"\btweet\w*\b",
            r"\bupload\w*\b", r"\bsubir\s+(no|pro|para)\b", r"\blan[cç]a\w*\b",
        ),
    ),
    Zone(
        name="destroy",
        why="data is removed and a backup may not exist",
        patterns=(
            r"\bdelet\w*\b", r"\bdestroy\w*\b", r"\bdrop\s+(table|database|schema)\b",
            r"\bpurge\w*\b", r"\bwipe\w*\b",
            r"\bformat\s+(the\s+)?(disk|drive|volume)\b",
            r"\bforce[- ]push\b", r"\breset\s+--hard\b", r"\brm\s+-[rf]{1,2}\b",
            r"\bterminate\w*\b", r"\brevoke\w*\b",
            r"\bapag\w*\b", r"\bexclu\w*\b", r"\bremov\w*\b",
        ),
    ),
    Zone(
        name="identity",
        why="credentials and identity proofs are the principal's to hold, never the agent's",
        patterns=(
            # Plurais: "rotate all passwords" e "trocar as senhas" sao a forma
            # mais comum do pedido, e \bpassword\b exigia o singular exato.
            r"\bpasswords?\b", r"\bpasswd\b", r"\bcredential\w*\b", r"\bapi[- ]?keys?\b",
            r"\bsecret\b", r"\btoken\b", r"\b2fa\b", r"\bmfa\b", r"\botp\b", r"\bkyc\b",
            r"\bidentity\s+verification\b", r"\b(log|sign)\s*in\s+as\b",
            r"\bsenhas?\b", r"\bcredenci\w*\b",
        ),
    ),
    Zone(
        name="taste",
        why="an aesthetic call is personal preference, and preference cannot be inferred from precedent",
        patterns=(
            r"\bwhich\s+(one|version|option|design|variant|thumbnail)\b",
            # One stem for both languages: it spans the English inflections of
            # "prefer" and the Portuguese ones, which share the same root.
            r"\bpref(e|i)r\w*\b", r"\blook[s]?\s+better\b", r"\b(ficou|est[aá]|parece)\s+melhor\b",
            r"\bapprov\w*\b", r"\baprova\w*\b", r"\bsign[- ]off\b",
            r"\baesthetic\w*\b", r"\best[eé]tic\w*\b",
        ),
    ),
    Zone(
        name="direction",
        why="a change of goal, price or strategy is a business decision, not an inference",
        patterns=(
            r"\bpivot\w*\b",
            r"\bchange\s+(the\s+)?(strategy|direction|goal|scope|plan)\b",
            r"\bpric(e|ing)\b", r"\bpre[cç]o\b", r"\bcontract\w*\b",
            r"\bhir(e|ing)\b", r"\bfir(e|ing)\s+\w+\b",
            r"\blegal\b", r"\bcompliance\s+risk\b",
            r"\bdecis[aã]o\s+de\s+neg[oó]cio\b",
            # Escolher arquitetura ou trocar de plataforma e decisao de rumo: tem
            # custo e travamento de longo prazo, e nao e detalhe de implementacao.
            r"\bmigrat\w*\b", r"\bmigra[rc]\w*\b",
            r"\barchitecture\b", r"\barquitetura\b",
        ),
    ),
])


def default_zones() -> ZoneSet:
    """The shipped zone set.

    A function rather than a bare re-export, so callers have an obvious place
    to wrap and nothing is tempted to reach for a module-level singleton. The
    dataclasses are frozen, but the habit outlives the enforcement.
    """
    return DEFAULT_ZONES


def scan(text: str, zones: ZoneSet | None = None) -> Sequence[tuple[str, str]]:
    """Every zone the text touches, as ``(zone_name, pattern)`` pairs.

    The decision path uses :meth:`ZoneSet.first_match`, which stops at the
    first hit because one reserved zone is enough to hold. This walks all of
    them, which is what you want when tuning a zone set against real traffic
    and asking "how often would this have held, and for which reason".
    """
    zs = default_zones() if zones is None else zones
    return tuple((z.name, hit) for z in zs for hit in [z.matches(text)] if hit is not None)
