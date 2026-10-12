import pytest

from disguisebrowser.input import keys


def test_letters_digits_and_shift_variants() -> None:
    a = keys.definition_for_char("a")
    assert a is not None and (a.code, a.key_code, a.shift) == ("KeyA", 65, False)
    big_a = keys.definition_for_char("A")
    assert big_a is not None and (big_a.code, big_a.shift, big_a.text) == ("KeyA", True, "A")
    one = keys.definition_for_char("1")
    bang = keys.definition_for_char("!")
    assert one is not None and bang is not None
    assert one.code == bang.code == "Digit1" and bang.shift


def test_punctuation_and_whitespace() -> None:
    assert keys.definition_for_char("?") is not None and keys.definition_for_char("?").shift  # type: ignore[union-attr]
    assert keys.definition_for_char("/") is not None
    assert keys.definition_for_char(" ") is keys.NAMED_KEYS["Space"]
    assert keys.definition_for_char("\n") is keys.NAMED_KEYS["Enter"]


def test_non_us_characters_are_not_typeable_by_key() -> None:
    assert keys.definition_for_char("ф") is None
    assert keys.definition_for_char("€") is None


def test_jcuken_maps_cyrillic_onto_us_physical_keys() -> None:
    ef = keys.definition_for_char("ф", layout="us,ru")
    assert ef is not None
    assert (ef.code, ef.key_code, ef.shift, ef.group, ef.text) == ("KeyA", 65, False, 1, "ф")
    capital = keys.definition_for_char("Ф", layout="us,ru")
    assert capital is not None and capital.shift and capital.code == "KeyA" and capital.group == 1
    numero = keys.definition_for_char("№", layout="us,ru")
    assert numero is not None and (numero.code, numero.shift, numero.group) == ("Digit3", True, 1)
    # ASCII stays on the US group even when ЙЦУКЕН is available.
    assert keys.definition_for_char("q", layout="us,ru").group == 0  # type: ignore[union-attr]
    assert keys.definition_for_char("€", layout="us,ru") is None


def test_every_jcuken_letter_has_an_x11_keysym() -> None:
    for char in "йцукенгшщзхъфывапролджэячсмитьбюё":
        definition = keys.definition_for_char(char, layout="ru")
        assert definition is not None, char
        assert keys.level0_keysym(definition) == keys.CYRILLIC_KEYSYMS[char]


def test_named_keys() -> None:
    assert keys.definition_for_name("Enter").text == "\r"
    assert keys.definition_for_name("x").code == "KeyX"
    with pytest.raises(ValueError, match="unknown key"):
        keys.definition_for_name("Hyperspace")
