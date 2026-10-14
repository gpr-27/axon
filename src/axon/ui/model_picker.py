"""
Tabbed model picker: one tab per linked provider (Tab / ← → to switch), models listed with
pricing; a final "+ Connect" tab opens the provider connector.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from axon import platform as plat
from axon.providers.catalog import ProviderPreset, get_linked_providers, get_models_for_provider, get_preset_by_id
from axon.ui.picker import SelectItem, Selector, pick
from axon.ui.provider_picker import (
    CUSTOM_MODEL_OPTION,
    RANDOM_MODEL_OPTION,
    apply_provider,
    choose_model_option,
    find_key,
    run_provider_picker,
    same_endpoint,
)
from axon.ui.theme import GOLD, MINT, RST

if TYPE_CHECKING:
    from axon.agent.loop import Agent

_HAS_TERMIOS = plat.HAS_TERMIOS  # back-compat flag
_CONNECT = "__connect__"


def _price_hint(model: str) -> str:
    try:
        from axon.providers import PRICING
        p = PRICING.get(model)
    except Exception:
        p = None
    if not p:
        return ""
    cache = f" · ${p['cache_read']:.2f} cached" if "cache_read" in p else ""
    return f"${p['input']:.2f} in · ${p['output']:.2f} out /1M{cache}"


def _linked(agent: Agent) -> list[ProviderPreset]:
    linked = list(get_linked_providers(agent))
    if not linked:
        ar = get_preset_by_id("agentrouter")
        linked = [ar] if ar else []
    return linked


def run_model_picker(agent: Agent) -> bool:
    """Interactive tabbed model picker. Returns True if the model/provider changed."""
    if not plat.is_interactive():
        return _fallback_model_picker(agent)
    presets = _linked(agent)
    cur_url = agent.settings.base_url
    tab = next((i for i, p in enumerate(presets) if same_endpoint(p.base_url, cur_url)), 0)

    def items_for_tab(i: int) -> list[SelectItem]:
        if i >= len(presets):
            return [SelectItem(label="＋ Connect a new provider (API key & endpoint)…", value=_CONNECT)]
        p = presets[i]
        active_tab = same_endpoint(p.base_url, cur_url)
        out = []
        for m in get_models_for_provider(p, agent):
            is_cur = active_tab and m == agent.settings.model
            out.append(SelectItem(label=m, value=(p, m), hint=("active · " if is_cur else "") + _price_hint(m),
                                  badge=f"{MINT}●{RST}" if is_cur else " "))
        out.append(SelectItem(label=CUSTOM_MODEL_OPTION, value=(p, CUSTOM_MODEL_OPTION), badge=" "))
        out.append(SelectItem(label=RANDOM_MODEL_OPTION, value=(p, RANDOM_MODEL_OPTION), badge=" "))
        return out

    tabs = [("● " if same_endpoint(p.base_url, cur_url) else "") + p.name for p in presets] + ["＋ Connect"]
    sel = Selector(title="Switch model", subtitle=f"{GOLD}{agent.settings.model}{RST}", tabs=tabs, tab_index=tab,
                   items_for_tab=items_for_tab, filterable=True, placeholder="type to filter models", max_visible=12,
                   fullscreen=True, footer="↑↓ move · tab/shift-tab provider · enter select · esc cancel")
    # Start on the active model when visible.
    for i, it in enumerate(sel.items):
        if isinstance(it.value, tuple) and it.value[1] == agent.settings.model:
            sel.index = i
            break
    chosen = sel.run()
    if not isinstance(chosen, SelectItem):
        return False
    if chosen.value == _CONNECT:
        return run_provider_picker(agent)
    preset, option = chosen.value
    return _apply_model_selection(agent, preset, option)


def _apply_model_selection(agent: Agent, preset: ProviderPreset, selected_option: str) -> bool:
    """Apply preset + chosen option (model, custom or random) to the agent."""
    model = choose_model_option(preset, selected_option)
    if not model:
        print(f"\n  (Active model unchanged: {agent.settings.model})\n")
        return False
    key: str | None = None
    if preset.requires_key:
        key = find_key(preset)
        if not key and same_endpoint(preset.base_url, agent.settings.base_url):
            cur = agent.settings.api_key
            key = cur.get_secret_value() if hasattr(cur, "get_secret_value") else str(cur or "")
    return apply_provider(agent, preset, model, key)


def _fallback_model_picker(agent: Agent) -> bool:
    """Line-mode picker: step 1 provider, step 2 model."""
    linked = _linked(agent)
    cur_url = agent.settings.base_url
    options = [f"✓ {p.name} ({p.base_url}){' [Active]' if same_endpoint(p.base_url, cur_url) else ''}" for p in linked]
    connect = "➕ Connect a New Provider..."
    options.append(connect)
    chosen = pick(options, title="Switch Active Model · Select Provider")
    if not chosen:
        return False
    if chosen == connect:
        return run_provider_picker(agent)
    preset = linked[options.index(chosen)]
    m_options = list(get_models_for_provider(preset, agent)) + [CUSTOM_MODEL_OPTION, RANDOM_MODEL_OPTION]
    model = pick(m_options, title=f"Select Model for {preset.name}",
                 current=agent.settings.model if agent.settings.model in m_options else None)
    if not model:
        return False
    return _apply_model_selection(agent, preset, model)
