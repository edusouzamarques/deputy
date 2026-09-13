import pytest

from deputy import (
    decide,
    Situation,
    Outcome,
    StreakState,
    Opinion,
    Verdict,
    ZoneSet,
    Zone,
)
from deputy.decide import is_waiting
from deputy.quorum import QuorumOutcome, tally, read_verdict


def make_streak(count=0, anchor=None):
    """Return a streak state; shared helper keeps tests focused on the behaviour under test."""
    return StreakState(count=count, anchor=anchor)


def make_proceed(text="answer", source="analyst"):
    """Build a valid PROCEED opinion so tests read as intent, not constructor noise."""
    return Opinion(source=source, verdict=Verdict.PROCEED, text=text, fault="")


def make_hold(text="wait a moment", source="skeptic"):
    """Build a HOLD opinion used to simulate human caution in quorum tests."""
    return Opinion(source=source, verdict=Verdict.HOLD, text=text, fault="")


def make_silent(fault="no response", source="lane"):
    """Build a SILENT opinion with an explicit fault so silence is diagnostic, not empty."""
    return Opinion(source=source, verdict=Verdict.SILENT, text="", fault=fault)


def make_situation(text="posso seguir?", opinions=None, streak=None, zones=None, quorum_minimum=2):
    """Central situation factory so each test states only what varies.

    The default text is a question on purpose. An agent that is *not* asking
    needs no authorisation, so the ladder exits at its first rung and every
    later rung - zones, streak, quorum - goes untested. With a non-question
    default, a suite can look green while never once exercising the parts that
    do the deciding.
    """
    return Situation(
        agent_text=text,
        opinions=opinions if opinions is not None else [],
        streak=streak if streak is not None else make_streak(),
        cap=4,
        quorum_minimum=quorum_minimum,
        zones=zones,
    )


@pytest.fixture
def reserved_zones():
    """Provide a reserved-zone set so PROCEED never depends on ambient environment."""
    zone = Zone(name="publish", patterns=("publicar", "tweet",), why="held for review")
    return ZoneSet(zones=[zone])


def test_reserved_zone_beats_consensus(reserved_zones):
    """Reserved zones must stay locked even when every predictor agrees; human consent is the point."""
    opinions = [make_proceed("a"), make_proceed("b"), make_proceed("c")]
    decision = decide(make_situation("posso publicar no canal?", opinions=opinions, zones=reserved_zones))
    assert decision.outcome is Outcome.RESERVED_ZONE
    assert decision.may_proceed is False


def test_reserved_zone_checked_before_streak(reserved_zones):
    """Reserved zones outrank streak exhaustion so policy cannot be bypassed by repetitive retries."""
    exhausted = make_streak(count=5)
    opinions = [make_proceed("a"), make_proceed("b")]
    decision = decide(
        make_situation("posso publicar no canal?", opinions=opinions, streak=exhausted, zones=reserved_zones)
    )
    assert decision.outcome is Outcome.RESERVED_ZONE


@pytest.mark.parametrize(
    "opinions,expected",
    [
        ([make_silent("lane1 failure"), make_silent("lane2 failure"), make_silent("lane3 failure")], Outcome.LANES_SILENT),
        ([make_hold("wait"), make_hold("hold"), make_hold("stop")], Outcome.DISSENT),
    ],
)
def test_silence_is_not_dissent(opinions, expected):
    """Three silences must not collapse into the same outcome as three holds; availability is a distinct failure."""
    decision = decide(make_situation("status?", opinions=opinions))
    assert decision.outcome is expected


def test_silent_requires_fault():
    """SILENT without a fault loses the diagnostic story; failures must be explainable."""
    with pytest.raises(ValueError):
        Opinion(source="lane", verdict=Verdict.SILENT, text="", fault="")


def test_proceed_requires_text():
    """A PROCEED verdict without an answer is a lie; the instruction must always be usable."""
    with pytest.raises(ValueError):
        Opinion(source="lane", verdict=Verdict.PROCEED, text="", fault="")


def test_read_verdict_empty_raw_defaults_to_silent():
    """Empty predictor output means the lane failed, not that we should wait silently."""
    opinion = read_verdict("lane1", "")
    assert opinion.verdict is Verdict.SILENT
    assert opinion.fault


def test_read_verdict_wait_phrase_becomes_hold():
    """Human hedges like `wait` are caution signals and must map to HOLD, not PROCEED."""
    opinion = read_verdict("lane1", "wait, I want to see it first")
    assert opinion.verdict is Verdict.HOLD


def test_tally_undersized_panel():
    """Quorum cannot be judged from a single voice; the system must call the panel undersized."""
    result = tally([make_proceed("yes")], minimum=2)
    assert result.outcome is QuorumOutcome.UNDERSIZED


def test_consensus_instruction_is_median_length():
    """The chosen instruction on PROCEED should be the median-length answer so we do not always pick the loudest."""
    short = make_proceed("ok", source="a")
    medium = make_proceed("ship it with tests", source="b")
    long = make_proceed("ship it with tests but write docs too and mention risks", source="c")
    result = tally([short, medium, long], minimum=2)
    assert result.instruction == "ship it with tests"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("posso publicar no canal?", True),
        ("Pronto: 14 repos no ar.", False),
    ],
)
def test_is_waiting_detects_pending(text, expected):
    """Waiting detection must read intent, not punctuation; Portuguese updates count too."""
    assert is_waiting(text) is expected


@pytest.mark.parametrize(
    "builder,expected",
    [
        (lambda: decide(make_situation("ok?", opinions=[make_proceed("a"), make_proceed("b")])), Outcome.PROCEED),
        (lambda z: decide(make_situation("posso publicar no canal?", opinions=[make_proceed("a"), make_proceed("b")], zones=z)), Outcome.RESERVED_ZONE),
        (
            lambda: decide(
                make_situation("posso seguir?", opinions=[make_proceed("a"), make_proceed("b")], streak=make_streak(count=5))
            ),
            Outcome.STREAK_EXHAUSTED,
        ),
        (
            lambda: decide(make_situation("posso seguir?", opinions=[make_hold("no"), make_proceed("ok"), make_hold("wait")]))
        ,
            Outcome.DISSENT,
        ),
        (
            lambda: decide(make_situation("posso seguir?", opinions=[make_silent(), make_silent(), make_silent()])),
            Outcome.LANES_SILENT,
        ),
        (lambda: decide(make_situation("posso seguir?", opinions=[make_proceed("solo")], quorum_minimum=2)), Outcome.UNDERSIZED_PANEL),
    ],
)
def test_decide_exhaustive_outcomes(builder, expected, reserved_zones):
    """Every Outcome enum member must be reachable; regressions cannot hide an unreachable branch."""
    if expected is Outcome.RESERVED_ZONE:
        decision = builder(reserved_zones)
    else:
        decision = builder()
    assert decision.outcome is expected


def test_zone_set_without_removes_zone(reserved_zones):
    """Removing a zone must actually free requests that were previously held back."""
    trimmed = reserved_zones.without("publish")
    decision = decide(
        make_situation("posso publicar no canal?", opinions=[make_proceed("a"), make_proceed("b")], zones=trimmed)
    )
    assert decision.outcome is not Outcome.RESERVED_ZONE


def test_zone_set_without_unknown_name_raises(reserved_zones):
    """Silent failure when removing a zone would hide config drift; unknown names must error."""
    with pytest.raises(KeyError):
        reserved_zones.without("does-not-exist")


def test_zone_requires_patterns():
    """A reserve rule with no patterns would catch everything by accident; the config must be explicit."""
    with pytest.raises(ValueError):
        Zone(name="catchall", patterns=(), why="hold")


@pytest.mark.parametrize(
    "source,fault",
    [("lane1", "timeout"), ("lane2", "rate limited")],
)
def test_silent_opinion_carries_fault(source, fault):
    """Each silence must carry its own fault so debugging can trace the failing lane."""
    opinion = make_silent(fault=fault, source=source)
    assert opinion.fault == fault


@pytest.mark.parametrize(
    "raw,expected_verdict",
    [
        ("ship it", Verdict.PROCEED),
        ("wait until monday", Verdict.HOLD),
        ("", Verdict.SILENT),
    ],
)
def test_read_verdict_variants(raw, expected_verdict):
    """Verdict parsing must be conservative: clear proceed stays, hedges hold, silence explains itself."""
    opinion = read_verdict("lane", raw)
    assert opinion.verdict is expected_verdict
    if expected_verdict is Verdict.SILENT:
        assert opinion.fault


@pytest.mark.parametrize("minimum", [2, 3])
def test_tally_requires_minimum(minimum):
    """Quorum must always respect its minimum so undersized panels cannot self-authorize."""
    result = tally([make_proceed("yes")], minimum=minimum)
    assert result.outcome is QuorumOutcome.UNDERSIZED


@pytest.mark.parametrize("count", [0, 1, 2])
def test_streak_not_exhausted(count):
    """The streak cap is a protective pause, not a permanent blockade; normal counts must pass."""
    opinions = [make_proceed("a"), make_proceed("b")]
    decision = decide(make_situation("posso seguir?", opinions=opinions, streak=make_streak(count=count)))
    assert decision.outcome is Outcome.PROCEED


@pytest.mark.parametrize(
    "zone_name,pattern,text",
    [
        ("deploy", ("deploy",), "should we deploy now?"),
        ("finance", ("wire transfer",), "wire transfer of 5k?"),
        ("social", ("tweet",), "can we tweet the milestone?"),
    ],
)
def test_reserved_zone_blocks_matching(zone_name, pattern, text):
    """Each reserved rule must match on its own patterns so unrelated text is not blocked."""
    zone = Zone(name=zone_name, patterns=tuple(pattern) if isinstance(pattern, tuple) else (pattern,), why="hold")
    zones = ZoneSet(zones=[zone])
    opinions = [make_proceed("a"), make_proceed("b")]
    decision = decide(make_situation(text, opinions=opinions, zones=zones))
    assert decision.outcome is Outcome.RESERVED_ZONE


@pytest.mark.parametrize(
    "text,expected",
    [
        ("may i proceed?", True),
        ("awaiting approval", True),
        ("here is the result", False),
    ],
)
def test_is_waiting_more_cases(text, expected):
    """Waiting detection should recognise asking language and reject simple updates."""
    assert is_waiting(text) is expected


@pytest.mark.parametrize(
    "opinions,minimum,expected_outcome",
    [
        ([make_proceed("yes"), make_proceed("yes")], 2, QuorumOutcome.CONSENSUS_PROCEED),
        # Um jurado MUDO junto de um que discorda NAO e dissenso: e apagao. O mudo
        # contamina a banca inteira, porque "nao respondeu" nao pode dividir
        # nome com "respondeu e pediu para esperar" - foi exatamente essa
        # confusao que escondeu um mecanismo morto por sete semanas.
        ([make_proceed("yes"), make_hold("wait"), make_silent()], 2,
         QuorumOutcome.LANES_SILENT),
        ([make_proceed("yes"), make_hold("wait")], 2, QuorumOutcome.DISSENT),
        ([make_proceed("yes")], 3, QuorumOutcome.UNDERSIZED),
    ],
)
def test_tally_outcomes(opinions, minimum, expected_outcome):
    """Tallying must classify the panel before any instruction is chosen."""
    result = tally(opinions, minimum=minimum)
    assert result.outcome is expected_outcome


def test_decide_proceed_may_proceed_true():
    """PROCEED must be the only outcome granting may_proceed so automation does not fire on partial signals."""
    opinions = [make_proceed("short"), make_proceed("longer answer")]
    decision = decide(make_situation("ship?", opinions=opinions))
    assert decision.outcome is Outcome.PROCEED
    assert decision.may_proceed is True
    assert decision.instruction


def test_decide_dissent_may_proceed_false():
    """DISSENT means the humans are split; the system must block, not pick a side."""
    opinions = [make_proceed("go"), make_hold("wait"), make_hold("hold")]
    decision = decide(make_situation("ship?", opinions=opinions))
    assert decision.outcome is Outcome.DISSENT
    assert decision.may_proceed is False


def test_decide_lanes_silent_reason_mentions_fault():
    """Silence outcomes should surface the recorded fault so operators know which lanes failed."""
    silent = make_silent("timeout", source="lane1")
    opinions = [silent, make_silent("rate limit", source="lane2"), make_silent("offline", source="lane3")]
    decision = decide(make_situation("ship?", opinions=opinions))
    assert decision.outcome is Outcome.LANES_SILENT
    assert "timeout" in decision.reason or "rate limit" in decision.reason


def test_decide_streak_exhausted_blocks_even_with_proceed():
    """Once the streak cap is hit the request must stop, so loops cannot hammer the human."""
    streak = make_streak(count=10)
    opinions = [make_proceed("a"), make_proceed("b")]
    decision = decide(make_situation("ship?", opinions=opinions, streak=streak))
    assert decision.outcome is Outcome.STREAK_EXHAUSTED
    assert decision.may_proceed is False


def test_decide_returns_quorum_details():
    """Decision metadata should expose the underlying quorum so callers can render explanations."""
    opinions = [make_proceed("a"), make_proceed("b")]
    decision = decide(make_situation("ship?", opinions=opinions))
    assert decision.quorum is not None
    assert hasattr(decision.quorum, "outcome")


def test_zoneset_without_returns_new_instance(reserved_zones):
    """Removing a zone should not mutate the original; configuration edits must be explicit."""
    trimmed = reserved_zones.without("publish")
    assert trimmed is not reserved_zones
    assert any(zone.name == "publish" for zone in reserved_zones.zones)


def test_zone_matching_is_case_insensitive():
    """Reserved zones must match even when the human capitalises the trigger word."""
    zone = Zone(name="tweet", patterns=("tweet",), why="hold")
    zones = ZoneSet(zones=[zone])
    decision = decide(
        make_situation("Should we TWEET the milestone?", opinions=[make_proceed("a"), make_proceed("b")], zones=zones)
    )
    assert decision.outcome is Outcome.RESERVED_ZONE


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "\n"],
)
def test_read_verdict_blank_is_silent(raw):
    """Whitespace-only predictor output is still silence; the fault message must always exist."""
    opinion = read_verdict("lane", raw)
    assert opinion.verdict is Verdict.SILENT
    assert opinion.fault


@pytest.mark.parametrize(
    "texts,expected_instruction",
    [
        (["short", "medium answer", "longest answer by far"], "medium answer"),
        (["a", "bbbb", "cc"], "cc"),
    ],
)
def test_instruction_median_variants(texts, expected_instruction):
    """Instruction choice must be deterministic median even when lengths are close."""
    opinions = [make_proceed(t, source=f"lane{i}") for i, t in enumerate(texts)]
    result = tally(opinions)
    assert result.instruction == expected_instruction
