"""Finding the entities a question names."""

from tests.qa.helpers import PARTY, PENA, UENO, US
from wikipulse.qa.graph import EntityCandidate, lucene_terms
from wikipulse.qa.mentions import STOPWORDS, event_references, find_mentions, find_titles, fold

DEMOCRATS = EntityCandidate("Q29552", "Democratic Party", (), "organization", "Q29552")


def names(question: str, candidates: list[EntityCandidate]) -> list[str]:
    return [m.candidate.name for m in find_mentions(question, candidates)]


def test_fold_ignores_case_accents_and_punctuation() -> None:
    assert fold("Santiago  Peña's") == fold("santiago pena s") == "santiago pena s"


def test_names_are_found_with_or_without_accents_and_capitals() -> None:
    question = "how are ueno bank and Santiago Pena connected?"
    assert names(question, [UENO, PENA, US]) == ["Ueno Bank", "Santiago Peña"]


def test_aliases_count_and_each_entity_is_found_once() -> None:
    assert names("What did Peña say? Was Peña there?", [PENA]) == ["Santiago Peña"]


def test_the_longest_name_wins_where_names_overlap() -> None:
    assert names("What did the Democratic Party do?", [PARTY, DEMOCRATS]) == ["Democratic Party"]


def test_a_name_must_appear_as_whole_words() -> None:
    bank = EntityCandidate("k", "Bank", (), "other", None)
    assert names("Who runs the banking sector?", [bank]) == []


def test_short_names_count_only_when_written_exactly() -> None:
    assert names("Tell us about the election", [US]) == []
    assert names("What happened in the US today?", [US]) == ["United States"]


def test_page_titles_and_event_references() -> None:
    titles = ["2026 Paraguay banking crisis", "Banking"]
    assert find_titles("What is the 2026 Paraguay banking crisis?", titles) == titles[:1]
    assert event_references("Who is involved in event 341 and in run:12?") == ([341], ["run:12"])
    assert event_references("Who is involved in event #7?") == ([7], [])


def test_the_full_text_query_leaves_out_common_words() -> None:
    query = lucene_terms("How are Ueno Bank and Santiago Peña connected?", STOPWORDS)
    assert query == "ueno OR bank OR santiago OR peña"
