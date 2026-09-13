# Provenance

**Author:** Eduardo de Souza Marques
**Licence:** MIT
**Repository:** https://github.com/edusouzaxGV/deputy

## Where this came from

This package is a rewrite of a mechanism that ran in the author's private automation stack:
a hook that fired when an agent ended a turn waiting for a decision, consulted three models
to predict what the principal would say, and — on consensus, and outside a set of forbidden
topics — injected that prediction so the agent could continue without waiting.

It ran for seven weeks and never once fired.

The decision log is what makes the story worth publishing rather than just fixing:

```
2026-07-24 .. 2026-09-12
46 opportunities   26 refusals (correct, forbidden topic)   20 "no consensus"   0 proceeds
```

Three independent faults, each sufficient on its own:

1. The credential lookup used a path from a different operating system, so the primary
   transport was never attempted.
2. All three hard-coded model identifiers had since been retired — two returned HTTP 410
   (end of life), one returned 403.
3. The fallback invoked a shell script through a process API that the host platform rejects
   outright.

Every one of those landed in a broad `except` that returned an empty string, and the caller
counted an empty string as "did not vote to proceed". That is indistinguishable from a
cautious dissent, so the log of a completely dead mechanism looked exactly like the log of a
working, conservative one.

The failure was found by auditing the mechanism's own log and noticing that a component with
a stated purpose had a zero in the column that measured that purpose.

## What is different here

- **Silence has its own name.** `LANES_SILENT` is not `DISSENT`. A predictor that fails to
  answer records the fault that made it fail, and voids the panel.
- **An `Opinion` cannot be constructed dishonestly.** A silent verdict with no recorded fault
  raises; a proceed verdict with empty text raises. The original bug is unrepresentable in
  the type.
- **The deterministic layer holds.** Reserved zones are checked before any predictor, so the
  answer never depends on a network call, and consensus can never unlock a reserved decision.
- **The health line exists.** `deputy health` states, in one sentence, when a window contains
  no successes and a run of faults — and exits non-zero so a scheduler can alert on it.
- **Failing closed means failing closed.** A streak ledger that cannot be read back refuses,
  instead of reporting zero and granting a fresh budget.

## What was left behind

Nothing of the original deployment ships here beyond the model of the failure. Specifically
excluded: every vendor, product and service identifier; the credential lookup; the transport
code; the surrounding automation, its file layout and its domain vocabulary; and all personal
and machine-specific paths. The forbidden-topic list was rewritten from scratch as general
categories with stated reasons, rather than ported.

The result is the model, not the transliteration: what may be decided alone, how agreement is
established, how far autonomy may run before a human must speak again, and how the whole
thing reports on itself.

## AI assistance

Developed with substantial AI assistance. The architecture, the failure analysis, the
ordering of the decision ladder and the operational decisions are the author's; a
model-assisted workflow was used for implementation and test scaffolding, under review.

Two of the modules (`journal.py`, `streak.py`) were first drafted by a cheaper model against
a written specification, then reviewed and corrected. That review was not a formality: the
draft of `streak.py` returned a zero count for a corrupt ledger — which reads as a fresh
autonomy budget — and the specification was at fault for asking that it "never raise". The
shipped version refuses instead. The test suite was drafted the same way, and reviewing it
surfaced a case where the *generated test* was wrong and the implementation right: it
expected `DISSENT` from a panel containing a silent lane, and the package correctly returns
`LANES_SILENT`.

Recording this here because a claim of authorship should say what the method actually was.

## Testing

The suite runs with no network, no real clock and no browser. Clocks and ledgers are
injected. The only tests that touch a disk are the ledger and journal round-trips, which use
pytest's `tmp_path`.

`python -m pytest -q`
