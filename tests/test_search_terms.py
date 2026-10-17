"""Query tokenization for lexical search.

These exist because of a bug that made every multi-word search silently return
nothing on any corpus without embeddings.

The inverted index stores *tokens*. Handing it a whole phrase looks for a single
token spelled like that phrase, which never exists. Measured against the built
OTel slice: ``order`` matched 58,199 rows, ``failed to place order`` matched
**0**. On OpenStack the vector branch hid the problem; on a lexical-only corpus
it was total.
"""

from __future__ import annotations

from backend.db.query import MAX_SEARCH_TERMS, search_terms


def test_a_phrase_becomes_its_words():
    """The actual bug: a phrase has to be split to match anything."""
    assert search_terms("failed to place order") == ("failed", "place", "order")


def test_a_single_word_is_preserved():
    assert search_terms("timeout") == ("timeout",)


def test_json_punctuation_separates_words_rather_than_vanishing():
    """Log bodies are largely JSON, so this is the common case.

    An earlier version stripped punctuation instead of splitting on it and
    produced ``detailsfailed`` -- a token that matches nothing.
    """
    terms = search_terms('"details":"failed to prepare order"')
    assert terms == ("details", "failed", "prepare", "order")
    assert "detailsfailed" not in terms


def test_underscores_and_slashes_separate_words():
    assert search_terms("connection_refused") == ("connection", "refused")
    assert "HTTP" in search_terms("HTTP/1.1 503")


def test_internal_hyphens_are_kept_as_one_word():
    """``service-unavailable`` is one token in the index, not two."""
    assert "service-unavailable" in search_terms("503 service-unavailable")


def test_digits_are_searchable():
    """Status and error codes are often the most selective thing available."""
    assert "503" in search_terms("HTTP 503")
    assert "13" in search_terms("grpc code 13")


def test_filler_words_are_dropped():
    assert search_terms("the order was for a") == ("order",)


def test_a_query_of_pure_filler_yields_nothing():
    """Deliberately empty rather than falling back to the raw text.

    A fallback would hand the index an unmatchable phrase and report "no
    results", which reads as evidence of absence. Refusing says what is true:
    the question had nothing searchable in it.
    """
    assert search_terms("is it the") == ()
    assert search_terms("why") == ()


def test_words_that_could_appear_in_a_message_are_never_dropped():
    """The noise list must not eat real log vocabulary."""
    for word in ("error", "failed", "timeout", "refused", "null", "retry"):
        assert search_terms(f"the {word}") == (word,)


def test_single_characters_are_dropped():
    """One-character tokens match a large share of the corpus."""
    assert search_terms("a b c") == ()
    assert search_terms("x timeout") == ("timeout",)


def test_duplicate_words_are_matched_once():
    """Repeats would add index work and skew BM25 without adding recall."""
    assert search_terms("order order ORDER") == ("order",)


def test_case_is_preserved_but_deduplication_ignores_it():
    terms = search_terms("Order order")
    assert terms == ("Order",)


def test_term_count_is_capped():
    """A pasted stack trace must not become hundreds of index sub-queries."""
    terms = search_terms(" ".join(f"word{n}" for n in range(100)))
    assert len(terms) == MAX_SEARCH_TERMS


def test_empty_input_is_empty():
    assert search_terms("") == ()
    assert search_terms("   ") == ()
