from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.corpus import CorpusCase, load_all_cases


@pytest.fixture(scope="session")
def corpus_cases() -> list[CorpusCase]:
    cases_dir = Path(__file__).parent / "fixtures" / "cases"
    return load_all_cases(cases_dir)

