"""Checks that a measured claim is backed by the results it cites.

Two layers. The code check (``unsupported_numbers``, below) is exact and always on;
the semantic check (``claim_targets``, further down) picks the claims that survive
it for a TypeSafe judgment of supports / contradicts / says_nothing, whose policy
lives in ``decision.py``.

Code check: a measured claim's numbers must come from the results it cites.

A finding labelled *measured* promises that the query results it cites show it.
Whether the cited result exists is checked in ``answers.py``; this module checks
the other half: every number in the statement must appear in the cited results.
A model that adds two counts, rounds, or recalls a figure from the narrative
produces a number no query returned, and a number nobody can find in the evidence
is not measured -- it is a hypothesis, however plausible.

What counts as "appears in the cited results":

* a numeric cell of a cited row, or a number inside a text cell (a pid, a line id);
* the result's own totals (``total_matches``, ``row_count``, ``returned``, ``limit``);
* the call's filters and arguments (a window bound or limit the claim restates).

Not checked, because they are labels rather than measurements: ids such as ``R2``,
``F1``, ``req-...`` or ``node-5`` (a token with letters around its digits). Dates,
times and addresses are matched as literal text, since splitting ``12.505`` or
``10.11.10.1`` into numbers would make them meaningless.

A percentage matches a cell holding the same value (``96.8``) or the same fraction
(``0.968``) at the precision the claim states. The model computing a share from two
counts therefore fails, which is the point: shares come from the database or from
code (``priority.py``), never from the model.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

# Matched as literal text, longest forms first.
_LITERAL_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?"  # date[ time]
    r"|\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"  # time
    r"|\d+(?:\.\d+){2,}"  # address or version
)
# An identifier: a word with a digit inside it (R2, F1, req-0a1b, node-5).
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z][\w-]*\d[\w-]*\b")
_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|\d+(?:\.\d+)?%?")
_SCALAR_KEYS = ("total_matches", "row_count", "returned", "limit")


def _decimal(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", "").rstrip("%"))
    except InvalidOperation:
        return None


def _scalars(value: Any) -> Iterable[Any]:
    """Every leaf of a row, filter or argument structure."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _scalars(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _scalars(item)
    else:
        yield value


def _same_share(value: Decimal, percent: Decimal, places: int) -> bool:
    """Whether a cell equals a stated percentage, as a percent or as a fraction."""
    try:
        return round(value, places) == percent or round(value * 100, places) == percent
    except InvalidOperation:  # a value too large to round at this precision
        return False


class _Haystack:
    """The numbers and text of the cited results."""

    def __init__(self, records: Sequence[dict]) -> None:
        self.numbers: set[Decimal] = set()
        texts: list[str] = []
        for record in records:
            leaves = [
                *_scalars(record.get("rows") or []),
                *_scalars(record.get("filters") or {}),
                *_scalars(record.get("arguments") or {}),
                *(record.get(key) for key in _SCALAR_KEYS),
            ]
            for leaf in leaves:
                self._add(leaf, texts)
        self.text = "\n".join(texts).replace("T", " ")

    def _add(self, leaf: Any, texts: list[str]) -> None:
        if leaf is None or isinstance(leaf, bool):
            return
        if isinstance(leaf, (int, float, Decimal)):
            number = _decimal(str(leaf))
            if number is not None and number.is_finite():
                # Magnitude only: a claim says "1.5 seconds after", the row -1.5.
                self.numbers.add(abs(number))
            return
        text = str(leaf)
        texts.append(text)
        for token in _NUMBER_RE.findall(text):
            number = _decimal(token)
            if number is not None:
                self.numbers.add(number)

    def has_literal(self, literal: str) -> bool:
        return literal.replace("T", " ") in self.text

    def has_number(self, token: str) -> bool:
        number = _decimal(token)
        if number is None:
            return True  # not a number after all; nothing to check
        if not token.endswith("%"):
            return number in self.numbers
        places = max(-number.as_tuple().exponent, 0)
        return any(_same_share(value, number, places) for value in self.numbers)


def unsupported_numbers(statement: str, records: Sequence[dict]) -> list[str]:
    """Numbers (and dates, times, addresses) in ``statement`` that no record shows.

    Empty when every figure is backed, so a statement without numbers passes.
    Order follows the statement; each distinct figure is reported once.
    """
    haystack = _Haystack([r for r in records if isinstance(r, dict)])
    missing: list[str] = []

    def note(token: str) -> None:
        if token not in missing:
            missing.append(token)

    remainder = statement or ""
    for literal in _LITERAL_RE.findall(remainder):
        if not haystack.has_literal(literal):
            note(literal)
    remainder = _LITERAL_RE.sub(" ", remainder)
    remainder = _IDENTIFIER_RE.sub(" ", remainder)
    for token in _NUMBER_RE.findall(remainder):
        if not haystack.has_number(token):
            note(token)
    return missing


# Only thousands-grouped figures are checked in prose. A stated total is what a
# reader takes as measured, and a list marker ("1.") or a window length ("30")
# never looks like one, so the check stays quiet where the narrative legitimately
# says things no query returned.
_GROUPED_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")


def unsupported_narrative_numbers(text: str, records: Sequence[dict]) -> list[str]:
    """Thousands-grouped totals in the narrative that no cited result shows.

    The exact check (:func:`unsupported_numbers`) runs on findings, which are
    structured and easy to verify. The narrative is prose, where a bare number
    (a window length, a count of datasets, a list marker) would make the check
    noisy -- but a grouped total such as "988,783" reads as measured, so a model
    that added counts up instead of quoting one is caught here.
    """
    haystack = _Haystack([r for r in records if isinstance(r, dict)])
    missing: list[str] = []
    for token in _GROUPED_NUMBER_RE.findall(text or ""):
        if token not in missing and not haystack.has_number(token):
            missing.append(token)
    return missing


# --- which claims go to the semantic check -----------------------------------

# Starting values. The classifier request holds one question per claim and shows
# each claim only the rows it cites, so these bound its size.
MAX_CHECKED_CLAIMS = 8
MAX_CLAIM_ROWS = 20


@dataclass(frozen=True)
class ClaimTarget:
    """One measured finding that survived the code check and goes to Jev."""

    index: int
    """Position in the answer's ``findings`` list (the ledger renders by it)."""

    finding_id: str
    statement: str
    records: tuple[dict, ...]
    """The cited results, in citation order."""

    @property
    def key(self) -> str:
        """Question id and state key; never shown to the model."""
        return f"claim_{self.index}"


def _read(answer: Any, name: str) -> list:
    if isinstance(answer, dict):
        return answer.get(name) or []
    return getattr(answer, name, None) or []


def _field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def claim_targets(
    answer: Any,
    valid_ids: set[str],
    records: Sequence[dict],
    limit: int = MAX_CHECKED_CLAIMS,
) -> list[ClaimTarget]:
    """The findings worth a semantic check, in ledger order.

    Only a ``measured`` finding is checked, and only when it cites at least one
    existing result and every number in it is shown by what it cites: a claim
    that already failed the code check is a hypothesis, and asking a model to
    re-judge it would only spend a question. Without records there is nothing to
    check against. Findings beyond ``limit`` are left unchecked.
    """
    by_id = {r.get("id"): r for r in records if isinstance(r, dict)}
    if not by_id:
        return []
    targets: list[ClaimTarget] = []
    for index, finding in enumerate(_read(answer, "findings")):
        if (_field(finding, "kind") or "hypothesis") != "measured":
            continue
        statement = (_field(finding, "statement") or "").strip()
        cited = [
            rid for rid in _field(finding, "result_ids") or [] if rid in valid_ids
        ]
        if not statement or not cited:
            continue
        cited_records = tuple(by_id[rid] for rid in cited if rid in by_id)
        if not cited_records or unsupported_numbers(statement, cited_records):
            continue
        targets.append(
            ClaimTarget(index, _field(finding, "id") or f"#{index + 1}", statement, cited_records)
        )
        if len(targets) >= limit:
            break
    return targets


def relevant_rows(
    statement: str, rows: Sequence[dict], limit: int = MAX_CLAIM_ROWS
) -> list[dict]:
    """At most ``limit`` rows, preferring those the statement talks about.

    A claim cites a result that can hold a hundred rows, but is about a few of
    them. A row scores one point per cell the statement repeats (a name, a number);
    the best-scoring rows are kept in their original order. With no scoring row
    the first ``limit`` are kept. Cutting by position alone would hide the one
    row that settles a claim and make it look unsupported.
    """
    rows = [row for row in rows if isinstance(row, dict)]
    if len(rows) <= limit:
        return rows
    text = (statement or "").lower()
    numbers = {
        number for token in _NUMBER_RE.findall(statement or "")
        if (number := _decimal(token)) is not None
    }

    def score(row: dict) -> int:
        points = 0
        for leaf in _scalars(row):
            if leaf is None or isinstance(leaf, bool):
                continue
            if isinstance(leaf, (int, float, Decimal)):
                number = _decimal(str(leaf))
                points += number is not None and abs(number) in numbers
            else:
                cell = str(leaf).strip().lower()
                points += len(cell) >= 3 and cell in text
        return points

    scored = [(score(row), position) for position, row in enumerate(rows)]
    best = sorted(scored, key=lambda item: (-item[0], item[1]))[:limit]
    if best[0][0] == 0:
        return rows[:limit]
    return [rows[position] for _, position in sorted(best, key=lambda item: item[1])]
