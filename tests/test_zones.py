"""Tests for the deputy zones danger detection system.

These tests verify that the zone-based pattern matching correctly identifies
risky operations in both English and Portuguese. This matters because deputy
agents must block dangerous commands (spending, publishing, destroying) in
the user's native language to prevent irreversible actions.
"""

import pytest

from deputy.zones import (
    Zone,
    ZoneSet,
    DEFAULT_ZONES,
    default_zones,
    scan,
)


class TestZoneValidation:
    """Zone construction must enforce validity to prevent runtime matching failures."""

    def test_zone_with_empty_patterns_raises_value_error(self):
        """Verify that creating a Zone without patterns raises ValueError.

        Preventing empty patterns matters because a pattern-less Zone would be
        useless for matching yet would silently consume namespace in ZoneSets,
        indicating a configuration error.
        """
        with pytest.raises(ValueError):
            Zone(name="broken", why="no patterns", patterns=())

    def test_zone_with_invalid_regex_raises_value_error(self):
        """Verify that invalid regex patterns are rejected at construction time.

        Early regex validation matters because pattern errors discovered at
        match-time would crash the agent during critical operations.
        """
        with pytest.raises(ValueError):
            Zone(name="bad", why="invalid regex", patterns=("[unclosed",))


class TestZoneMatching:
    """Zone matching must be precise and informative."""

    def test_matches_returns_firing_pattern(self):
        """Verify matches() returns the specific pattern that fired.

        Returning the matched pattern matters for audit logging; knowing which
        specific keyword triggered a zone helps refine the pattern lists.
        """
        zone = Zone(
            name="test",
            why="test zone",
            patterns=(r"deploy", r"push"),
        )
        result = zone.matches("I will deploy now")
        assert result == r"deploy"
        assert result is not True

    def test_matching_is_case_insensitive(self):
        """Verify zone matching ignores case variations.

        Case insensitivity matters because users type commands in unpredictable
        case ('DEPLOY', 'Deploy'), and safety matching must not be evadable
        by capitalization tricks.
        """
        zone = Zone(
            name="case",
            why="test case",
            patterns=("deploy",),
        )
        assert zone.matches("DEPLOY the code") is not None
        assert zone.matches("DePlOy the code") is not None


class TestZoneSetIntegrity:
    """ZoneSet must maintain uniqueness and immutability guarantees."""

    def test_duplicate_names_raise_value_error(self):
        """Verify that duplicate zone names are rejected.

        Preventing duplicate names matters because zone names are identifiers
        in logs and configurations; duplicates would create ambiguity about
        which rule fired.
        """
        zone1 = Zone(name="dup", why="first", patterns=("a",))
        zone2 = Zone(name="dup", why="second", patterns=("b",))
        with pytest.raises(ValueError):
            ZoneSet((zone1, zone2))

    def test_without_removes_by_name_and_preserves_original(self):
        """Verify without() removes zones and is immutable.

        Immutable removal matters because zone sets are shared across threads;
        mutating shared state during configuration would cause race conditions.
        """
        zone_a = Zone(name="keep", why="a", patterns=("alpha",))
        zone_b = Zone(name="remove", why="b", patterns=("beta",))
        original = ZoneSet((zone_a, zone_b))

        filtered = original.without("remove")
        assert "remove" not in filtered.names
        assert "remove" in original.names
        assert len(original.names) == 2

    def test_without_unknown_name_raises_key_error(self):
        """Verify removing a non-existent zone raises KeyError.

        Explicit failure on unknown names matters because silently ignoring
        typos in exclusion lists would leave dangerous zones active.
        """
        zone = Zone(name="exists", why="a", patterns=("alpha",))
        zset = ZoneSet((zone,))
        with pytest.raises(KeyError):
            zset.without("nonexistent")


class TestZoneSetOrdering:
    """ZoneSet must respect ordering for priority-based matching."""

    def test_first_match_stops_at_first_zone(self):
        """Verify first_match returns only the first matching zone.

        First-match priority matters when overlapping patterns exist; the
        most specific or highest-priority zone should win deterministically.
        """
        zone_a = Zone(name="first", why="a", patterns=("deploy",))
        zone_b = Zone(name="second", why="b", patterns=("deploy",))
        zset = ZoneSet((zone_a, zone_b))

        result = zset.first_match("deploy now")
        assert result is not None
        zone, pattern = result
        assert zone.name == "first"

    def test_scan_returns_all_matching_zones(self):
        """Verify scan returns all matching zones, not just the first.

        Complete scanning matters for comprehensive risk assessment; knowing
        all triggered zones helps evaluate the overall danger level of text.
        """
        zone_a = Zone(name="a", why="a", patterns=("danger",))
        zone_b = Zone(name="b", why="b", patterns=("danger",))
        zset = ZoneSet((zone_a, zone_b))

        results = scan("danger ahead", zones=zset)
        assert len(results) == 2
        names = {r[0] for r in results}
        assert names == {"a", "b"}


class TestDefaultZones:
    """Default zones must cover real-world danger phrases in multiple languages."""

    @pytest.mark.parametrize(
        "sentence,expected_zone",
        [
            # Spend - English
            ("Please complete the purchase for $500", "spend"),
            ("I need to buy more server credits", "spend"),
            # Spend - Portuguese
            ("Preciso comprar mais créditos agora", "spend"),
            ("Efetue o pagamento da fatura", "spend"),
            # Publish - English
            ("Publish this to production immediately", "publish"),
            ("Deploy the release to PyPI", "publish"),
            # Publish - Portuguese
            ("Publicar no PyPI imediatamente", "publish"),
            ("Lançar a nova versão do pacote", "publish"),
            # Destroy - English
            ("Delete the production database now", "destroy"),
            ("Remove all user data permanently", "destroy"),
            # Destroy - Portuguese
            ("Excluir o banco de dados de produção", "destroy"),
            ("Apagar todos os registros agora", "destroy"),
            # Identity - English
            ("Change my password and revoke API keys", "identity"),
            ("Update the authentication credentials", "identity"),
            # Identity - Portuguese
            ("Alterar minha senha e revogar as chaves", "identity"),
            ("Atualizar as credenciais de acesso", "identity"),
            # Taste - English
            ("I prefer the blue design over the red one", "taste"),
            ("In my opinion, this layout looks better", "taste"),
            # Taste - Portuguese
            ("Eu prefiro o design azul ao invés do vermelho", "taste"),
            ("Na minha opinião, este layout ficou melhor", "taste"),
            # Direction - English
            ("We should migrate to Kubernetes instead of Docker Swarm", "direction"),
            ("Let's change the architecture to use microservices", "direction"),
            # Direction - Portuguese
            ("Devemos migrar para Kubernetes", "direction"),
            ("Vamos alterar a arquitetura do sistema", "direction"),
        ],
    )
    def test_default_zone_is_reachable(self, sentence, expected_zone):
        """Verify each default zone triggers on realistic sentences.

        Multilingual coverage matters because deputy agents operate globally;
        missing Portuguese or English danger phrases would allow irreversible
        operations to proceed unchecked.
        """
        results = scan(sentence, zones=default_zones())
        matched_names = {r[0] for r in results}
        assert expected_zone in matched_names, (
            f"Zone '{expected_zone}' did not match: {sentence}"
        )

    def test_plain_progress_report_matches_nothing(self):
        """Verify innocuous text does not trigger danger zones.

        Avoiding false positives matters because flagging every benign status
        update would make the zones useless due to alert fatigue.
        """
        sentence = "Finished: 14 repos live, CI green."
        results = scan(sentence, zones=default_zones())
        assert len(results) == 0, (
            f"Progress report unexpectedly matched: {results}"
        )
