"""
Tabbed model picker: one tab per linked provider (Tab / ← → to switch), models listed with
pricing; a final "+ Connect" tab opens the provider connector.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from axon import platform as plat
from axon.providers.catalog import (
    ProviderPreset, discover_models, get_linked_providers, get_models_for_provider, get_preset_by_id, prefetch_models,
)
from axon.ui.picker import SelectItem, Selector, pick
from axon.ui.provider_picker import (
    CUSTOM_MODEL_OPTION,
    RANDOM_MODEL_OPTION,
    apply_provider,
    choose_model_option,
    find_key,
    is_active_preset,
    run_provider_picker,
)
from axon.ui.theme import GOLD, MINT, ROSE, RST

if TYPE_CHECKING:
    from axon.agent.loop import Agent

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


def _lifecycle_hint(model: str) -> str:
    """' · deprecated · retires 2027-01-15' for a model the provider marks deprecated, retired or scheduled to retire."""
    try:
        from axon.providers.model_caps import lifecycle_note
        note = lifecycle_note(model)
    except Exception:
        note = ""
    return f" · {ROSE}{note}{RST}" if note else ""


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
    prefetch_models(presets, agent)     # pick up models released since the built-in list was written
    tab = next((i for i, p in enumerate(presets) if is_active_preset(agent.settings, p)), 0)

    def items_for_tab(i: int) -> list[SelectItem]:
        if i >= len(presets):
            return [SelectItem(label="＋ Connect a new provider (API key & endpoint)…", value=_CONNECT)]
        p = presets[i]
        active_tab = is_active_preset(agent.settings, p)
        out = []
        for m in get_models_for_provider(p, agent):
            is_cur = active_tab and m == agent.settings.model
            out.append(SelectItem(label=m, value=(p, m), hint=("active · " if is_cur else "") + _price_hint(m) + _lifecycle_hint(m),
                                  badge=f"{MINT}●{RST}" if is_cur else " "))
        out.append(SelectItem(label=CUSTOM_MODEL_OPTION, value=(p, CUSTOM_MODEL_OPTION), badge=" "))
        out.append(SelectItem(label=RANDOM_MODEL_OPTION, value=(p, RANDOM_MODEL_OPTION), badge=" "))
        return out

    armed: list[tuple[str, str] | None] = [None]    # the model a first Delete press is waiting to confirm

    def on_key(sel: Selector, key: str) -> bool:
        if key in ("ctrl-r", "f5") and sel.tab_index < len(presets):
            p = presets[sel.tab_index]
            sel.flash("Refreshing the model list…", GOLD)
            sel.redraw()
            found = discover_models(p, agent, force=True)
            sel.set_items(items_for_tab(sel.tab_index))
            sel.flash(f"{len(found)} models listed by {p.name}" if found else f"{p.name} did not return a model list (using the saved one)",
                      MINT if found else ROSE)
            return True
        if key not in ("delete", "ctrl-d"):
            armed[0] = None
            return False
        it = sel.current()
        if sel.tab_index >= len(presets) or it is None or not isinstance(it.value, tuple):
            return True
        p, m = presets[sel.tab_index], it.value[1]
        if m in (CUSTOM_MODEL_OPTION, RANDOM_MODEL_OPTION):
            return True
        if is_active_preset(agent.settings, p) and m == agent.settings.model:
            sel.flash("That is the active model; switch to another one first.", ROSE)
        elif armed[0] != (p.id, m):
            armed[0] = (p.id, m)
            sel.flash(f"Press Delete again to remove {m}", GOLD)
        else:
            armed[0] = None
            from axon.providers.endpoints import delete_model
            try:
                how = delete_model(p.id, m)
            except Exception as e:
                sel.flash(f"Could not update the config: {e}", ROSE)
                return True
            sel.set_items(items_for_tab(sel.tab_index))
            sel.flash(f"Removed {m}" if how else f"{m} was already removed", MINT)
        return True

    tabs = [("● " if is_active_preset(agent.settings, p) else "") + p.name for p in presets] + ["＋ Connect"]
    sel = Selector(title="Switch model", subtitle=f"{GOLD}{agent.settings.model}{RST}", tabs=tabs, tab_index=tab,
                   items_for_tab=items_for_tab, filterable=True, placeholder="type to filter models", max_visible=12,
                   fullscreen=True, on_key=on_key,
                   footer="↑↓ move · tab/shift-tab provider · enter select · del remove · ctrl-r refresh · esc cancel")
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
        if not key and is_active_preset(agent.settings, preset):
            cur = agent.settings.api_key
            key = cur.get_secret_value() if hasattr(cur, "get_secret_value") else str(cur or "")
    return apply_provider(agent, preset, model, key)


def _fallback_model_picker(agent: Agent) -> bool:
    """Line-mode picker: step 1 provider, step 2 model."""
    linked = _linked(agent)
    options = [f"✓ {p.name} ({p.base_url}){' [Active]' if is_active_preset(agent.settings, p) else ''}" for p in linked]
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
