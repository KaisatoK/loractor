"""Code-side check that a measured claim's numbers come from the results it cites.

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
