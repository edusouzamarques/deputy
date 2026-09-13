"""deputy - bounded delegation for autonomous agents.

An agent that must stop and ask about everything is not autonomous. An agent
that never stops is not safe. This package draws the line between the two and
makes the line auditable:

* :mod:`deputy.zones` - decisions reserved for the principal, always, no
  matter how confident anyone is;
* :mod:`deputy.quorum` - agreement among independent predictors, where a
  predictor that fails to answer is recorded as a broken dependency and never
  as a cautious vote;
* :mod:`deputy.streak` - a bound on consecutive self-authorisations, reset the
  moment the principal really speaks;
* :mod:`deputy.decide` - the pure ladder that combines them;
* :mod:`deputy.journal` - the append-only record that makes "this has never
  fired" a discoverable fact rather than a silence.

The design is not theoretical. It is a rewrite of a mechanism that ran in
production for seven weeks while completely dead, and whose log looked
reasonable the entire time. See PROVENANCE.md.

Author: Eduardo de Souza Marques. Licence: MIT.
"""

from .decide import Decision, Outcome, Situation, decide, is_waiting
from .journal import Entry, Journal, JournalCorrupt
from .quorum import Opinion, QuorumOutcome, QuorumResult, Verdict, read_verdict, tally
from .streak import StreakLedger, StreakState, may_proceed, next_state, record_proceed
from .zones import DEFAULT_ZONES, Zone, ZoneSet, default_zones, scan

__version__ = "0.1.0"

__all__ = [
    "Decision", "Outcome", "Situation", "decide", "is_waiting",
    "Entry", "Journal", "JournalCorrupt",
    "Opinion", "QuorumOutcome", "QuorumResult", "Verdict", "read_verdict", "tally",
    "StreakLedger", "StreakState", "may_proceed", "next_state", "record_proceed",
    "DEFAULT_ZONES", "Zone", "ZoneSet", "default_zones", "scan",
    "__version__",
]
