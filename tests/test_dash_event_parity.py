"""Every event the agent can emit must have a handler in the web page, so nothing falls to the raw-JSON fallback."""
import re

from axon.agent import trace
from axon.features import dash_js_chat, sdk

NOT_EVENTS = {"CacheTracker", "Step", "TurnTrace"}


def test_every_trace_event_has_a_web_case():
    names = [n for n, v in vars(trace).items() if isinstance(v, type) and v.__module__ == trace.__name__ and n not in NOT_EVENTS]
    assert names
    src = dash_js_chat.JS_CHAT if hasattr(dash_js_chat, "JS_CHAT") else "".join(v for v in vars(dash_js_chat).values() if isinstance(v, str))
    cases = set(re.findall(r"case '([a-z_]+)'", src))
    missing = []
    for n in names:
        kind = sdk._TYPE_NAMES.get(n) or sdk._snake(n)
        if kind == "subagent_step":
            kind = "subagent_step"
        if kind not in cases:
            missing.append((n, kind))
    assert not missing, f"events with no case in the web page: {missing}"
