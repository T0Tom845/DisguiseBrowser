import pytest

from disguisebrowser.cdp.policy import RULES, QuietCdpPolicy
from disguisebrowser.errors import QuietCdpViolationError


def test_runtime_enable_is_forbidden() -> None:
    policy = QuietCdpPolicy()
    with pytest.raises(QuietCdpViolationError, match=r"Runtime\.enable"):
        policy.check("Runtime.enable")


@pytest.mark.parametrize(
    "method",
    ["Console.enable", "Page.setBypassCSP", "Emulation.setDeviceMetricsOverride",
     "Emulation.setAutomationOverride", "Emulation.setTimezoneOverride"],
)  # fmt: skip
def test_unconditionally_forbidden(method: str) -> None:
    with pytest.raises(QuietCdpViolationError):
        QuietCdpPolicy().check(method, {})


def test_everyday_commands_pass() -> None:
    policy = QuietCdpPolicy()
    everyday = (
        "Page.navigate",
        "Runtime.evaluate",
        "Runtime.callFunctionOn",
        "Page.createIsolatedWorld",
        "Input.dispatchMouseEvent",
        "Network.getCookies",
    )
    for method in everyday:
        policy.check(method, {})


def test_user_agent_override_requires_metadata() -> None:
    policy = QuietCdpPolicy()
    with pytest.raises(QuietCdpViolationError, match="Client Hints"):
        policy.check("Network.setUserAgentOverride", {"userAgent": "x"})
    policy.check(
        "Network.setUserAgentOverride",
        {"userAgent": "x", "userAgentMetadata": {"brands": [], "platform": "Linux"}},
    )


def test_init_scripts_only_in_isolated_worlds() -> None:
    policy = QuietCdpPolicy()
    with pytest.raises(QuietCdpViolationError):
        policy.check("Page.addScriptToEvaluateOnNewDocument", {"source": "1"})
    policy.check("Page.addScriptToEvaluateOnNewDocument", {"source": "1", "worldName": "w"})


def test_binding_only_in_isolated_worlds() -> None:
    policy = QuietCdpPolicy()
    with pytest.raises(QuietCdpViolationError):
        policy.check("Runtime.addBinding", {"name": "fn"})
    policy.check("Runtime.addBinding", {"name": "fn", "executionContextName": "w"})


def test_unsafe_and_allow_list_disable_enforcement() -> None:
    QuietCdpPolicy(unsafe=True).check("Runtime.enable")
    QuietCdpPolicy(allow=frozenset({"Runtime.enable"})).check("Runtime.enable")
    with pytest.raises(QuietCdpViolationError):
        QuietCdpPolicy(allow=frozenset({"Runtime.enable"})).check("Console.enable")


def test_every_rule_has_a_reason() -> None:
    assert all(rule.reason for rule in RULES)
    assert len({rule.method for rule in RULES}) == len(RULES)
