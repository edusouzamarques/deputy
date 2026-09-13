"""Defects found by an independent audit, each pinned so it cannot come back.

Every test here failed before its fix. They are kept in one file, apart from
the behavioural suite, because their value is historical as much as technical:
each one is a thing that looked right, read right in review, and was wrong.
"""

from __future__ import annotations

import pytest

from deputy.journal import Entry, Journal
from deputy.quorum import Verdict, read_verdict
from deputy.zones import default_zones


@pytest.mark.parametrize(
    "answer",
    [
        "please do not proceed",
        "I'd suggest we wait for the principal",
        "Let's hold off until tomorrow",
        "not yet, check with him first",
        "Honestly, I would ask him before doing that",
    ],
)
def test_a_refusal_stated_past_the_first_word_is_still_a_refusal(answer):
    """Hold detection used to match only a PREFIX of the first 24 characters.

    So a predictor writing "please do not proceed" - an unambiguous refusal -
    was read as PROCEED, and its vote counted toward authorising the very
    action it was objecting to. Of all the ways this package could be wrong,
    counting an explicit no as a yes is the worst, because the panel then
    reports unanimity it does not have.
    """
    assert read_verdict("panel", answer).verdict is Verdict.HOLD


@pytest.mark.parametrize(
    "answer",
    ["go ahead", "yes, write it", "escreve o README e me traz pronto", "ship it"],
)
def test_a_plain_yes_is_still_a_yes(answer):
    """The fix widened where a refusal may appear; it must not swallow assent.

    Widening the search from a prefix to the opening window makes false HOLDs
    possible. That direction is acceptable - it costs a round-trip - but only
    if ordinary approvals still read as approvals, or the panel never agrees
    about anything and the mechanism is useless in the other direction.
    """
    assert read_verdict("panel", answer).verdict is Verdict.PROCEED


def test_the_worst_fault_is_chosen_deterministically(tmp_path):
    """The health summary ranked fault types by count alone.

    On a tie the winner fell out of dict insertion order, so the same journal
    could name a different culprit on two runs - and a diagnostic that changes
    its mind about what broke is one nobody trusts twice.
    """
    journal = Journal(path=tmp_path / "j.ndjson")
    for i in range(3):
        journal.append(Entry(ts=f"2026-09-0{i + 1}T10:00:00+00:00", outcome="timeout", reason="x"))
    for i in range(3):
        journal.append(Entry(ts=f"2026-09-0{i + 4}T10:00:00+00:00", outcome="lanes_silent", reason="x"))

    assert journal.health(50) == journal.health(50)


def test_payroll_is_spending(tmp_path):
    """`pay` and `payment` were covered; the words people actually use were not.

    "Run payroll" moves more money than most things the spend zone catches,
    and matched nothing at all.
    """
    hit = default_zones().first_match("Shall I run payroll this month?")
    assert hit is not None and hit[0].name == "spend"


@pytest.mark.parametrize(
    "sentence,zone",
    [
        ("Efetue o pagamento da fatura", "spend"),
        ("Lancar a nova versao do pacote", "publish"),
        ("Remove all user data permanently", "destroy"),
        ("Atualizar as credenciais de acesso", "identity"),
        ("Eu prefiro o design azul", "taste"),
        ("este layout ficou melhor", "taste"),
        ("We should migrate to Kubernetes", "direction"),
        ("Vamos alterar a arquitetura do sistema", "direction"),
    ],
)
def test_inflections_the_first_pattern_set_missed(sentence, zone):
    """Each of these fell through every zone in the first hand-written set.

    They are the ordinary way a request gets phrased, not exotic input:
    "pagamento" where the pattern had only "pagar", "credenciais" where it had
    only "credencial", a verb whose Portuguese root vowel changes with person.
    A reserved zone that only recognises the dictionary form of its own verb
    is a zone that holds nothing.
    """
    hit = default_zones().first_match(sentence)
    assert hit is not None and hit[0].name == zone


@pytest.mark.parametrize(
    "sentence",
    [
        "Finished: 14 repos live, CI green.",
        "Pronto: 14 repos no ar, CI verde.",
        "Tests pass and the build is clean.",
    ],
)
def test_a_progress_report_touches_no_zone(sentence):
    """The counterweight to the test above.

    Widening patterns to catch real phrasing is how a zone set starts holding
    everything, at which point the operator switches it off and is protected by
    nothing. A plain statement of what was done must stay clear of all six.
    """
    assert default_zones().first_match(sentence) is None


@pytest.mark.parametrize(
    "sentence",
    [
        "Shall I rotate all passwords?",
        "Posso trocar as senhas do cofre?",
        "Should I revoke the api keys?",
    ],
)
def test_credentials_in_the_plural_are_still_credentials(sentence):
    """`\bpassword\b` and `\bsenha\b` required the exact singular.

    Nobody rotates one password. The plural is how the request is actually
    phrased, and it fell through the identity zone entirely.
    """
    hit = default_zones().first_match(sentence)
    assert hit is not None and hit[0].name in {"identity", "destroy"}


def test_the_situation_does_not_claim_to_anchor_anything():
    """`principal_text` documented itself as "used to anchor the streak".

    Nothing in `decide` reads it - anchoring happens in `streak.next_state`,
    before the streak ever reaches the Situation. A field that describes work
    it does not do sends the next reader looking for code that is not there.
    """
    import inspect

    # `from deputy import decide` gives the FUNCTION - the package re-exports
    # it - so the module has to be imported by its full path.
    from deputy.decide import decide as decide_fn

    assert "principal_text" not in inspect.getsource(decide_fn)
