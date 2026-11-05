"""
The page the browser shows after an MCP sign-in redirects back to Axon (http://localhost:<port>/callback).

One self-contained HTML document: inline CSS and an inline SVG of the Axon mark, no external requests, light and
dark mode. Every value from the URL is HTML-escaped, and the page removes the authorization code from the
address bar once it has loaded.
"""
from __future__ import annotations

from html import escape

# The three parts of the terminal banner logo (▲█▲ / █⚡█ / ▼█▼) drawn as one mark.
AXON_MARK_SVG = """\
<svg class="mark" viewBox="0 0 64 64" aria-hidden="true">
  <defs>
    <linearGradient id="ax" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#7c5cff"/><stop offset="0.55" stop-color="#22c3e6"/>
      <stop offset="1" stop-color="#2fd6a0"/>
    </linearGradient>
  </defs>
  <rect x="2" y="2" width="60" height="60" rx="16" fill="url(#ax)"/>
  <path d="M20 22 L32 12 L44 22 Z" fill="#fff" opacity=".95"/>
  <path d="M35 24 L26 35 H32 L29 44 L39 31 H33 L36 24 Z" fill="#fff"/>
  <path d="M20 42 L44 42 L32 52 Z" fill="#fff" opacity=".95"/>
</svg>"""

_STYLE = """\
:root { --bg:#f5f6fa; --card:#ffffff; --text:#151823; --muted:#5d6475; --line:#e4e7ef;
        --ok:#13a36f; --ok-bg:#e7f8f1; --bad:#d64545; --bad-bg:#fdecec; --code:#f0f2f7; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0d0f14; --card:#161922; --text:#eef0f6; --muted:#9aa2b5; --line:#262b38;
          --ok:#3ddc97; --ok-bg:#11261e; --bad:#ff7a7a; --bad-bg:#2a1414; --code:#1e2230; }
}
* { box-sizing:border-box; }
body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; padding:24px;
       background:var(--bg); color:var(--text);
       font:15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
.card { width:100%; max-width:440px; background:var(--card); border:1px solid var(--line); border-radius:20px;
        padding:32px 28px 26px; box-shadow:0 18px 50px rgba(0,0,0,.08); text-align:center; }
.mark { width:64px; height:64px; display:block; margin:0 auto 14px; }
.brand { font-weight:700; letter-spacing:.02em; margin:0 0 22px; color:var(--muted); font-size:13px;
         text-transform:uppercase; }
.badge { display:inline-flex; align-items:center; gap:8px; padding:6px 12px; border-radius:999px;
         font-weight:600; font-size:13px; margin-bottom:14px; }
.badge.ok { color:var(--ok); background:var(--ok-bg); }
.badge.bad { color:var(--bad); background:var(--bad-bg); }
h1 { font-size:21px; margin:0 0 8px; line-height:1.3; }
p { margin:0 0 6px; color:var(--muted); }
.server { color:var(--text); font-weight:600; }
.steps { text-align:left; margin:20px 0 0; padding:14px 16px; border:1px solid var(--line); border-radius:14px; }
.steps div { display:flex; gap:10px; padding:4px 0; color:var(--muted); }
.steps b { color:var(--text); font-weight:600; min-width:18px; }
code { background:var(--code); padding:1px 6px; border-radius:6px; font-size:13px; color:var(--text); }
.foot { margin-top:18px; font-size:12px; color:var(--muted); }
"""

# Removes ?code=…&state=… from the address bar so the one-time code is not left in view or in history.
_CLEAR_URL_SCRIPT = "<script>try{history.replaceState(null,'',location.pathname)}catch(e){}</script>"


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main class=\"card\">{AXON_MARK_SVG}<div class=\"brand\">Axon</div>{body}</main>"
        f"{_CLEAR_URL_SCRIPT}</body></html>"
    )


def success_page(server: str = "") -> str:
    """Shown when the provider sent back an authorization code."""
    name = f"<span class=\"server\">{escape(server)}</span>" if server else "the server"
    body = (
        "<div class=\"badge ok\">&#10003; Authorization received</div>"
        f"<h1>Connecting Axon to {name}</h1>"
        "<p>Axon is finishing the sign-in in your terminal.</p>"
        "<div class=\"steps\">"
        "<div><b>1</b><span>Go back to your terminal.</span></div>"
        "<div><b>2</b><span>Wait for <code>connected</code> and the number of tools found.</span></div>"
        "<div><b>3</b><span>Run <code>/mcp</code> to see the server and its tools.</span></div>"
        "</div>"
        "<div class=\"foot\">You can close this tab.</div>"
    )
    return _page(f"Axon · {server or 'MCP'} authorization received", body)


def failure_page(error: str, description: str = "", server: str = "") -> str:
    """Shown when the provider returned an error (or no code)."""
    name = f" to <span class=\"server\">{escape(server)}</span>" if server else ""
    detail = f"<p><code>{escape(error)}</code></p>"
    if description:
        detail += f"<p>{escape(description)}</p>"
    retry = f"/mcp login {server}" if server else "/mcp login <server>"
    body = (
        "<div class=\"badge bad\">&#10007; Sign-in failed</div>"
        f"<h1>Axon could not sign in{name}</h1>"
        f"{detail}"
        "<div class=\"steps\">"
        "<div><b>1</b><span>Go back to your terminal for the full message.</span></div>"
        f"<div><b>2</b><span>Try again with <code>{escape(retry)}</code>.</span></div>"
        "</div>"
        "<div class=\"foot\">You can close this tab.</div>"
    )
    return _page("Axon · sign-in failed", body)
