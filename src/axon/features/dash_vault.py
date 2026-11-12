"""Dashboard Vault panel: routes under ``/api/vault`` and the Settings tab that uses them.

Metadata only goes out (name, type, bound server, dates, archived). A value comes in through ``set`` / ``rotate`` and is
never returned, logged or echoed in an error. The routes run after the Host, cookie and Origin checks of the HTTP
layer and are refused on the phone listener (``/api/vault`` is in dash_phone.BLOCKED_PREFIXES). Migration and the
backend choice stay in the terminal (/vault migrate, /vault backend).
"""
from __future__ import annotations

from typing import Any

from axon.features import vault
from axon.features.dash_common import DashError


def _s(body: dict[str, Any], key: str, limit: int = 4096) -> str:
    v = body.get(key, "")
    return v.strip()[:limit] if isinstance(v, str) else ""


def get(srv: Any, route: str, qs: dict[str, str]) -> Any | None:
    if route != "/api/vault":
        return None
    s = vault.status()
    return {"backend": s["backend"], "label": s["label"], "protected": s["protected"], "warning": s["warning"],
            "items": vault.list_items(), "plaintext": [{"kind": p["kind"], "name": p["name"]} for p in s["plaintext"]],
            "types": list(vault.TYPES)}


def post(srv: Any, route: str, body: dict[str, Any]) -> Any | None:
    if not route.startswith("/api/vault/"):
        return None
    action = route[len("/api/vault/"):]
    name = _s(body, "name", 128)
    try:
        if action == "set":
            m = vault.set_secret(name, _s(body, "value"), type=_s(body, "type", 32) or "generic", bound=_s(body, "bound") or None)
            return {"ok": True, "item": m}
        if action == "rotate":
            return {"ok": True, "item": vault.rotate(name, _s(body, "value"))}
        if action == "archive":
            return {"ok": True, "item": vault.archive(name, body.get("archived") is not False)}
        if action == "delete":
            return {"ok": vault.delete(name)}
        if action == "validate":
            return vault.validate_oauth(name)
    except vault.VaultError as e:
        raise DashError(str(e), 400)
    return None


JS_VAULT = r"""
// ---------- vault tab (Settings) ----------
async function vaultTab(body) {
  let d; try { d = await api('/api/vault'); } catch (e) { body.append(errorBox(e.message)); return; }
  const when = t => t ? new Date(t * 1000).toLocaleString() : '-';
  body.append(h('div', { class: 'notice ' + (d.protected ? 'ok' : 'warn') }, h('b', null, 'Backend: ' + d.label + '. '), d.warning || 'Values are stored outside Axon\'s own files and are never shown again.'));
  if (d.plaintext.length) body.append(h('div', { class: 'notice warn' }, d.plaintext.length + ' secret(s) still live in plaintext files (' + d.plaintext.map(p => p.name).join(', ') + '). Run /vault migrate in the terminal to move them.'));
  const reload = () => { body.replaceChildren(); return vaultTab(body); };
  const act = async (route, payload, msg) => { try { await api(route, payload); toast(msg); } catch (e) { toast(e.message); } reload(); };
  const field = (ph, type) => h('input', { type: type || 'text', placeholder: ph, autocomplete: 'off', spellcheck: 'false', style: 'font-size:16px;min-width:0;flex:1 1 12rem' });
  const name = field('name, e.g. env:OPENAI_API_KEY'), val = field('value (write-only)', 'password'), bound = field('bound server URL (MCP only)');
  const type = h('select', { style: 'font-size:16px' }, ...d.types.map(t => h('option', { value: t }, t)));
  body.append(h('form', { style: 'display:flex;flex-wrap:wrap;gap:8px;margin:12px 0', onsubmit: ev => { ev.preventDefault(); act('/api/vault/set', { name: name.value, value: val.value, type: type.value, bound: bound.value }, 'Saved'); val.value = ''; } },
    name, type, val, bound, h('button', { class: 'mini', type: 'submit' }, 'Add')));
  if (!d.items.length) body.append(h('div', { class: 'sub' }, 'The vault is empty.'));
  for (const i of d.items) {
    body.append(h('div', { class: 'card', style: 'padding:10px 12px;margin:6px 0;display:flex;flex-wrap:wrap;gap:8px;align-items:center' },
      h('div', { style: 'flex:1 1 14rem;min-width:0;overflow-wrap:anywhere' }, h('b', null, i.name), h('div', { class: 'sub' }, i.type + (i.bound ? ' · bound ' + i.bound : '') + ' · updated ' + when(i.updated) + ' · used ' + when(i.last_used) + (i.archived ? ' · archived' : ''))),
      h('button', { class: 'mini', type: 'button', onclick: () => { const v = prompt('New value for ' + i.name + ' (write-only)'); if (v) act('/api/vault/rotate', { name: i.name, value: v }, 'Rotated'); } }, 'Rotate'),
      h('button', { class: 'mini', type: 'button', onclick: () => act('/api/vault/archive', { name: i.name, archived: !i.archived }, i.archived ? 'Restored' : 'Archived') }, i.archived ? 'Restore' : 'Archive'),
      i.type === 'mcp_oauth' ? h('button', { class: 'mini', type: 'button', onclick: async () => { try { const r = await api('/api/vault/validate', { name: i.name }); toast(r.result); } catch (e) { toast(e.message); } } }, 'Validate') : null));
  }
}
"""
