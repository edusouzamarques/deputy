# deputy

[![CI](https://github.com/edusouzamarques/deputy/actions/workflows/ci.yml/badge.svg)](https://github.com/edusouzamarques/deputy/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](./pyproject.toml)

**Let an agent continue without you — but only inside an envelope you can read, and never
without leaving a trace.**

Zero runtime dependencies. The decision is a pure function: no network, no clock, no
filesystem, no model vendor.

---

## The failure this prevents

An agent that stops to ask about everything is not autonomous. An agent that never stops is
not safe. So people build a middle thing: consult a few models, ask them to predict what the
principal would say, and proceed if they agree.

Here is the decision log of one such mechanism, running in production for seven weeks:

```
46 opportunities   26 correct refusals   20 "no consensus"   0 proceeds
```

It reads like a cautious system. It was a dead one.

Every one of those twenty "no consensus" records held three empty strings. Two of the three
model identifiers had reached end of life months earlier; the third returned 403. The
fallback path invoked a shell script in a way the host platform rejects outright. Each
failure landed in a broad `except` that returned `""`, and an empty answer was counted as
"did not vote to proceed" — which is arithmetically identical to a cautious dissent.

So a completely broken mechanism produced a log indistinguishable from a working one, and
nobody looked for seven weeks.

**The lesson is not "handle your errors". It is that silence and dissent must never share a
name.** A predictor that fails to answer is a broken dependency. A predictor that answers
"wait" is the system working. Collapse the two and you lose the ability to tell an outage
from prudence, exactly when it matters.

---

## What it does

```bash
$ deputy check --text "Finished the package. Shall I write the README?" \
    --opinion a="go ahead" \
    --opinion b="write it and show me when it's done" \
    --opinion c="yes"
outcome: proceed
reason:  unanimous across 3 predictors
instruction: write it and show me when it's done
```

The same request, with the panel unreachable:

```bash
$ deputy check --text "Finished the package. Shall I write the README?" \
    --fault a="HTTP 410 end of life" \
    --fault b="HTTP 403 authorization failed" \
    --fault c="OSError: not a valid Win32 application"
outcome: lanes_silent
reason:  3/3 predictors gave no answer (a: HTTP 410 end of life, b: HTTP 403 authorization
         failed, c: OSError: not a valid Win32 application)
```

Not "no consensus". **`lanes_silent`** — a distinct outcome, with the fault of each lane
named, so a dashboard or a cron can alert on it.

And a request no amount of agreement can unlock:

```bash
$ deputy check --text "All ready. May I publish to the channel?" \
    --opinion a="sure" --opinion b="sure" --opinion c="sure"
outcome: reserved_zone
reason:  reserved for the principal (publish: content reaches an audience and may be
         cached or indexed even if deleted)
zone:    publish
```

---

## The ladder

Four rungs, in this order. **Each may only refuse.** Nothing downstream reopens a door an
earlier rung closed, which is what lets you reason about the whole thing by reading four
functions.

| # | Rung | Why it sits here |
|---|---|---|
| 1 | Is the agent even waiting? | A turn that finished cleanly needs no permission. Keeps the failure-prone steps off the common path. |
| 2 | Is this a reserved zone? | Deterministic, and checked **before** any predictor. No amount of agreement makes it acceptable to spend or publish on someone's behalf — and this answer must not depend on a network call succeeding. |
| 3 | Is there streak budget left? | Each self-authorisation is a small extrapolation; *N* chained is a large one. Checked before consulting, because a panel you ask anyway is a panel you will eventually believe anyway. |
| 4 | Does the panel agree, having actually answered? | Last, and the only probabilistic rung. It can only narrow what the deterministic rungs already allowed. |

### Reserved zones

Six ship by default: `spend`, `publish`, `destroy`, `identity`, `taste`, `direction`. Each
carries a one-sentence reason that is shown to the principal and written to the journal.

The matching is deliberately over-inclusive, because the errors are not symmetric: a false
positive costs one round-trip, a false negative means money left the account while the agent
believed it had consent. That width was paid for — an earlier version matched two
inflections of *publish* but not the noun form, and "may I go ahead with the publication?"
walked through. It was caught only because the probabilistic layer happened to abstain that
day. Being saved by the layer that is allowed to be wrong is not being saved.

Zones are data. Remove one explicitly:

```python
from deputy import default_zones
zones = default_zones().without("spend")   # an agent with no budget
```

`without("typo")` raises, because a typo in a removal list is indistinguishable from a zone
that quietly stopped being enforced.

### The streak

A cap on consecutive self-authorisations, reset the moment the principal really speaks — the
anchor is a digest of their last message, so re-reading the same instruction does not hand
out fresh budget.

`cap=0` means *never proceed alone*, and says so. It does not mean unlimited.

A ledger that cannot be read back refuses rather than reporting zero. A corrupt file that
reads as "count 0" would grant a full budget precisely when the bookkeeping is known to be
broken.

### The journal

Every run appends exactly one line, holds included. A run that leaves no trace is how a dead
mechanism stays invisible.

```bash
$ deputy health
46 decisions, 0 proceeds, 26 held, 20 lanes_silent - ZERO proceed outcomes in this window -
nothing has succeeded here; the panel has not answered in 20 attempts; this is an outage,
not caution
```

`deputy health` exits 1 when it finds an outage, so a cron can alert without parsing prose.
That one line, had it existed, would have caught the original failure on day one instead of
day forty-nine.

---

## Install

Not on PyPI yet. Install from the repository:

```bash
pip install git+https://github.com/edusouzamarques/deputy
```

Or from a checkout:

```bash
git clone https://github.com/edusouzamarques/deputy
pip install -e "deputy[test]"
```

## Library use

```python
from deputy import Situation, decide, read_verdict

opinions = [read_verdict(name, answer) for name, answer in panel_answers.items()]
decision = decide(Situation(agent_text=closing_text, opinions=opinions, cap=4))

if decision.may_proceed:
    continue_with(decision.instruction)
else:
    ask_the_human(decision.reason)
```

`decide` takes a `Situation` and returns a `Decision`. Gathering the situation — transcripts,
ledgers, subprocesses — is the caller's job, and that is the point: every rule above is
testable with a literal.

## Design: the pure core

`zones`, `quorum`, `streak` (its pure functions) and `decide` touch nothing. `journal`,
`streak`'s ledger and `cli` are the only modules that read a clock or a filesystem, and the
test suite injects both.

Predictors are a protocol, not an integration. This package never names a model vendor, has
no HTTP client, and cannot be made to prefer one provider — whatever produces an opinion is
the caller's business.

## Limitations, stated plainly

- Zone matching is keyword-based. It reads *intent stated in text*; it cannot inspect what a
  tool call will actually do. It is a backstop for a stated request, not a sandbox.
- Predictors predicting a principal is an inference from precedent. The streak cap exists
  because that inference degrades as it compounds, and the reserved zones exist because some
  decisions should never rest on it at all.
- There is no notion of *partial* consent. A request is either inside the envelope or it
  waits.

## Licence

MIT — see [LICENSE](./LICENSE). Provenance and authorship: [PROVENANCE.md](./PROVENANCE.md).
