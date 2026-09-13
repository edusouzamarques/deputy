import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# NOTE: This value is not derived from theory or measurement. It is a starting
# guess that should be tuned by inspecting journal data on how rapidly chained
# self-authorisations drift from what the principal would actually endorse.
DEFAULT_CAP: int = 4

_WHITESPACE_RUN = re.compile(r"\s+")


@dataclass(frozen=True)
class StreakState:
    """Immutable snapshot of the self-authorisation streak.

    Attributes:
        count: Number of consecutive self-authorisations since the principal
            last sent a real message. Zero means the principal spoke most
            recently, so the streak is fully available.
        anchor: Stable digest (SHA-256 hex of the normalised message) of the
            principal's last real message. Used to detect when a genuinely new
            instruction has arrived and the streak must reset.
    """

    count: int
    anchor: str
    #: False quando o estado nao pode ser lido do disco (arquivo corrompido,
    #: truncado, ilegivel). Um estado NAO CONFIAVEL nunca autoriza: antes, um
    #: ledger corrompido devolvia count=0, que e exatamente um ORCAMENTO NOVO -
    #: falha pro lado aberto num mecanismo cuja unica funcao e limitar
    #: autonomia. Quem quiser retomar apaga o arquivo de proposito, e isso fica
    #: sendo um ato explicito em vez de um acidente.
    trusted: bool = True

    def to_mapping(self) -> dict[str, Any]:
        """Serialise this state to a plain JSON-compatible mapping.

        Returns:
            A dictionary with ``count`` and ``anchor`` keys, suitable for
            ``json.dumps``.
        """
        return {"count": self.count, "anchor": self.anchor}

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "StreakState":
        """Build a state from a mapping, falling back to defaults on bad data.

        This constructor never raises: any missing or malformed field causes
        the whole state to fall back to a fresh zero-count state, because a
        partially corrupt record must not be trusted to grant or deny budget.

        Args:
            mapping: A mapping that may contain ``count`` (int) and ``anchor``
                (str) keys.

        Returns:
            A valid ``StreakState``; either faithfully reconstructed from the
            mapping, or a fresh ``StreakState(count=0, anchor="")``.
        """
        try:
            count = mapping["count"]
            anchor = mapping["anchor"]
        except (KeyError, TypeError):
            return cls(count=0, anchor="")

        # Reject booleans explicitly: bool is a subclass of int in Python,
        # and ``True`` would otherwise silently become count=1.
        if isinstance(count, bool) or not isinstance(count, int):
            return cls(count=0, anchor="")
        if not isinstance(anchor, str):
            return cls(count=0, anchor="")
        if count < 0:
            return cls(count=0, anchor="")

        return cls(count=count, anchor=anchor)


class StreakLedger:
    """Persistent storage for a ``StreakState`` as a JSON file.

    All failures while loading are absorbed and yield a fresh state; a ledger
    that cannot be trusted must never be allowed to masquerade as a specific
    streak, but it may safely read as "the principal just spoke" because that
    is the conservative starting point for a fresh session.
    """

    def __init__(self, path: Path) -> None:
        """Initialise the ledger for the given file path.

        The file is not touched until ``save`` is called.

        Args:
            path: Location of the JSON file holding the persisted state.
        """
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """The file path this ledger reads from and writes to."""
        return self._path

    def load(self) -> StreakState:
        """Load the persisted state, never raising.

        A missing file, unreadable file, invalid JSON, or JSON of the wrong
        shape all produce a fresh ``StreakState(count=0, anchor="")``.

        Returns:
            The persisted state, or a fresh zero-count state on any failure.
        """
        if not self._path.exists():
            # Ausente != corrompido. Primeira execucao comeca limpa e confiavel.
            return StreakState(count=0, anchor="")
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return StreakState(count=0, anchor="", trusted=False)

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return StreakState(count=0, anchor="", trusted=False)

        if not isinstance(data, Mapping):
            return StreakState(count=0, anchor="", trusted=False)

        return StreakState.from_mapping(data)

    def save(self, state: StreakState) -> None:
        """Atomically persist the given state.

        The state is written to a temporary file in the same directory as the
        target and then moved into place with ``os.replace``. A crash mid-write
        therefore can leave only either the previous complete file or a
        complete new file — never a truncated one that would read as count 0
        and silently grant a fresh self-authorisation budget.

        Args:
            state: The state to persist.

        Raises:
            OSError: If the directory cannot be written or the file cannot be
                replaced. This is allowed to propagate: failing to save is
                loud, while failing to load is quiet, because a lost write
                must be noticed rather than silently dropped.
        """
        payload = json.dumps(state.to_mapping(), ensure_ascii=False, indent=2)
        payload_bytes = payload.encode("utf-8")

        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            prefix=self._path.name + ".", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                tmp_file.write(payload_bytes)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            # Clean up the orphaned temp file on any failure, including
            # keyboard interrupts, before re-raising.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def _normalise_message(message: str) -> str:
    """Normalise a principal message before hashing.

    Leading and trailing whitespace is stripped and every internal run of
    whitespace collapses to a single space, so that incidental formatting
    differences do not look like a new instruction.

    Args:
        message: The raw principal message.

    Returns:
        The normalised message text.
    """
    return _WHITESPACE_RUN.sub(" ", message.strip())


def _anchor_for(message: str) -> str:
    """Compute the stable anchor digest for a principal message.

    Args:
        message: The raw principal message.

    Returns:
        Lowercase hex SHA-256 digest of the normalised message encoded as
        UTF-8.
    """
    normalised = _normalise_message(message)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def next_state(current: StreakState, principal_message: str) -> StreakState:
    """Advance the state in response to an incoming principal message.

    The message is normalised and hashed. If the digest differs from the
    current anchor, the principal has actually spoken: a real instruction
    re-anchors everything, so the streak resets to zero with the new anchor.
    If the digest matches, the message carries no new information and the
    state is returned unchanged.

    Args:
        current: The state prior to this message.
        principal_message: The raw text of the principal's message.

    Returns:
        A reset state if the message differs from the anchor, otherwise the
        unchanged ``current`` state.
    """
    new_anchor = _anchor_for(principal_message)
    if new_anchor != current.anchor:
        return StreakState(count=0, anchor=new_anchor)
    return current


def may_proceed(state: StreakState, cap: int) -> tuple[bool, str]:
    """Decide whether one more self-authorisation is within budget.

    A non-positive cap is not an unlimited budget; it is an explicit policy
    that the agent must never proceed without the principal.

    Args:
        state: The current streak state.
        cap: Maximum number of consecutive self-authorisations permitted since
            the principal last spoke. Values at or below zero forbid all
            self-authorisation.

    An untrusted state - one that could not be read back from disk - refuses,
    because a corrupt ledger that reads as "count 0" would hand out a fresh
    budget precisely when the bookkeeping is known to be broken.

    Returns:
        A tuple ``(allowed, reason)`` where ``allowed`` is whether the agent
        may proceed alone once more, and ``reason`` is a human-readable,
        specific explanation of the decision.
    """
    if not state.trusted:
        return False, (
            "the streak ledger could not be read back, so the number of "
            "self-authorisations already used is unknown; refusing rather than "
            "assuming a fresh budget"
        )
    if cap <= 0:
        return (
            False,
            "self-authorisation is disabled: the cap is "
            f"{cap}, so the agent must never proceed without its principal",
        )

    if state.count >= cap:
        return (
            False,
            f"{state.count} of {cap} consecutive self-authorisations "
            "already used since the principal last spoke",
        )

    remaining = cap - state.count
    plural = "s" if remaining != 1 else ""
    return (
        True,
        f"{state.count} of {cap} consecutive self-authorisations used "
        f"since the principal last spoke; {remaining} remaining{plural} "
        "before a real instruction is required",
    )


def record_proceed(state: StreakState) -> StreakState:
    """Record one self-authorisation, incrementing the streak count.

    This function does not check the cap; use ``may_proceed`` beforehand to
    decide whether proceeding is permitted at all.

    Args:
        state: The current streak state.

    Returns:
        A new state with the same anchor and the count increased by one.
    """
    return StreakState(count=state.count + 1, anchor=state.anchor)
