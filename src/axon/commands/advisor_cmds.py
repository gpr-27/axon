"""/advisor <model|off|status>: the stronger model the main model can consult (tool consult_advisor), any provider."""
from __future__ import annotations

from typing import Any

from axon.commands.common import err, info, kv, ok, update_global_config, update_settings
from axon.commands.registry import CommandResult, command
from axon.ui.theme import BOLD, RST, SLATE, WHITE

CAT = "Model & Modes"
_OFF = {"off", "none", "disable", "disabled", "no", "-"}


def advisor_models(agent: Any) -> list[str]:
    """Every model of every linked provider (the advisor may belong to another provider than the main model)."""
    out: list[str] = []
    try:
        from axon.providers.catalog import get_linked_providers, get_models_for_provider
        for p in get_linked_providers(agent):
            out += [m for m in get_models_for_provider(p, agent) if m not in out]
    except Exception:
        pass
    return out


def set_advisor(agent: Any, model: str | None, persist: bool = True) -> None:
    from axon.tools.advisor_tool import set_active
    update_settings(agent, advisor_model=model or None)
    set_active(model)
    if persist:
        update_global_config({"advisor_model": model or ""})


def status_lines(agent: Any) -> list[str]:
    from axon.tools.advisor_tool import counters
    s = agent.settings
    model = getattr(s, "advisor_model", None)
    if not model:
        return ["Advisor: off. /advisor <model> turns it on (any provider's model)."]
    st = counters(agent)
    return [f"Advisor: {model}",
            f"Calls: {st['turn']}/{s.advisor_max_per_turn} this turn, {st['session']}/{s.advisor_max_per_session} this session",
            "Its cost is the 'advisor' row of the usage table."]


@command("advisor", category=CAT, description="Advisor model: the main model can consult a stronger one (/advisor <model|off|status>)",
         usage="/advisor                pick the advisor model\n/advisor <model>        any model Axon can route, any provider\n"
               "/advisor off            turn it off\n/advisor status         model and call counts")
def handle_advisor(agent: Any, arg: str) -> CommandResult:
    a = arg.strip()
    if a.lower() == "status":
        for ln in status_lines(agent):
            info(ln)
        return CommandResult(handled=True)
    if a.lower() in _OFF:
        set_advisor(agent, None)
        ok("Advisor off")
        return CommandResult(handled=True)
    if a:
        set_advisor(agent, a)
        ok(f"Advisor: {BOLD}{WHITE}{a}{RST} {SLATE}(consult_advisor is available to the model){RST}")
        return CommandResult(handled=True)
    from axon.ui.picker import pick
    models = advisor_models(agent)
    cur = getattr(agent.settings, "advisor_model", None)
    chosen = pick(["off"] + models, title="Advisor model", current=cur or "off")
    if not chosen:
        return CommandResult(handled=True)
    if chosen == "off":
        set_advisor(agent, None)
        ok("Advisor off")
    else:
        set_advisor(agent, chosen)
        ok(f"Advisor: {BOLD}{WHITE}{chosen}{RST}")
    return CommandResult(handled=True)
