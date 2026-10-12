import pytest

from disguisebrowser.persona import Persona
from disguisebrowser.warmup import WARMUP_URLS, choose_urls


def test_choose_urls_is_stable_for_a_seed() -> None:
    persona = Persona(seed=7)
    assert choose_urls(persona, count=4) == choose_urls(persona, count=4)
    assert len(choose_urls(persona, count=4)) == 4
    assert set(choose_urls(persona, count=len(WARMUP_URLS))) == set(WARMUP_URLS)


def test_choose_urls_rejects_an_empty_run() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        choose_urls(Persona(seed=1), count=0)


def test_different_seeds_can_diverge() -> None:
    first = choose_urls(Persona(seed=1), count=len(WARMUP_URLS))
    second = choose_urls(Persona(seed=2), count=len(WARMUP_URLS))
    assert first != second
