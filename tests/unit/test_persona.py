from dataclasses import replace

from disguisebrowser.persona import (
    GPU_PRESETS,
    Persona,
    Screen,
    Window,
    detect_languages,
    detect_timezone,
)


def test_detect_languages_from_posix_locale() -> None:
    assert detect_languages({"LANG": "ru_RU.UTF-8"}) == ("ru-RU", "ru", "en-US", "en")
    assert detect_languages({"LANG": "en_US.UTF-8"}) == ("en-US", "en")
    assert detect_languages({"LANGUAGE": "de_DE:en", "LANG": "de_DE.UTF-8"}) == (
        "de-DE", "de", "en", "en-US",
    )  # fmt: skip
    assert detect_languages({"LANG": "C.UTF-8"}) == ("en-US", "en")
    assert detect_languages({}) == ("en-US", "en")


def test_detect_timezone_prefers_tz_env() -> None:
    assert detect_timezone({"TZ": "Europe/Berlin"}) == "Europe/Berlin"
    assert detect_timezone({"TZ": ":Asia/Tokyo"}) == "Asia/Tokyo"


def test_validate_accepts_sane_persona() -> None:
    persona = Persona(languages=("ru-RU", "ru"), timezone="Europe/Moscow")
    assert persona.validate() == []


def test_validate_rejects_bad_values() -> None:
    persona = Persona(languages=("ru_RU",), timezone="Mars/Olympus", screen=Screen(100, 100))
    problems = persona.validate()
    assert any("BCP-47" in p for p in problems)
    assert any("timezone" in p for p in problems)
    assert any("plausible" in p for p in problems)


def test_validate_rejects_window_outside_screen() -> None:
    persona = Persona(window=Window(2000, 1000, 0, 0))
    assert any("does not fit" in p for p in persona.validate())


def test_proxy_parsing() -> None:
    assert Persona(proxy="socks5://1.2.3.4:1080").proxy_parts() == (
        "socks5", "1.2.3.4", 1080, None, None,
    )  # fmt: skip
    assert Persona(proxy="http://u:p@proxy:8080").proxy_parts() == (
        "http", "proxy", 8080, "u", "p",
    )  # fmt: skip
    assert any("scheme" in p for p in Persona(proxy="ftp://x:1").validate())
    assert any("scheme://" in p for p in Persona(proxy="http://nohost").validate())


def test_effective_window_fits_screen_and_is_deterministic_with_seed() -> None:
    persona = Persona(seed=123, screen=Screen(1920, 1080))
    first = persona.effective_window()
    second = replace(persona).effective_window()
    assert first == second
    assert first.fits(persona.screen)
    for seed in range(40):
        window = Persona(seed=seed, screen=Screen(1366, 768)).effective_window()
        assert window.fits(Screen(1366, 768)), (seed, window)


def test_gpu_presets_look_like_mesa_strings() -> None:
    for name, gpu in GPU_PRESETS.items():
        assert gpu.vendor in {"AMD", "Intel"}, name
        assert gpu.renderer and gpu.gl_version == "4.6"
