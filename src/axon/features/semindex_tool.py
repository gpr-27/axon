"""semantic_search tool, /index command and `axon index` subcommand (opt-in semantic code search, any embedding backend).

Off by default. Enable with ``/index --backend ollama --model nomic-embed-text`` or the ``semanticSearch`` block of the
user settings.json. Remote backends (OpenAI-compatible, Voyage) receive the text of indexed files and the search
queries; the first index asks for confirmation and remembers the host. The tool is advertised only while the feature is
enabled and an index exists."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, ClassVar

from axon.errors import ToolError
from axon.features import FeatureCommand
from axon.features import semindex as sx
from axon.features.semindex_config import BACKENDS, DEFAULTS, EmbedConfig, lookup_key, resolve
from axon.features.semindex_embed import EmbedError, Embedder
from axon.tools.base import Tool, ToolContext

_ACTIVE = {"on": False}
USAGE = ("/index                      build or update the index (background)\n"
         "/index --status             host, model, size, progress\n"
         "/index --rebuild            re-embed everything\n"
         "/index --cancel             stop a running index\n"
         "/index --backend ollama --model nomic-embed-text [--url U] [--key-env VAR]   set up and check the backend\n"
         "/index off [--delete]       disable (and delete the index)\n"
         "Add --yes to skip the confirmation. Remote backends receive file contents.")


def set_active(on: bool) -> None:
    _ACTIVE["on"] = bool(on)


def sync(agent: Any) -> None:
    """Advertise semantic_search only while enabled and an index exists."""
    try:
        s = agent.settings
        set_active(bool(getattr(s, "embedding_enabled", False)) and sx.index_exists(Path(s.workspace)))
    except Exception:
        set_active(False)


def make_embedder(settings: Any, cfg: EmbedConfig | None = None) -> Embedder:
    cfg = cfg or resolve(settings)
    return Embedder(cfg, lookup_key(cfg, getattr(settings, "workspace", None)))


class SemanticSearchTool(Tool):
    name: ClassVar[str] = "semantic_search"
    description: ClassVar[str] = (
        "Search the codebase by meaning with an embedding index: returns ranked path:line ranges with a short snippet. "
        "Use semantic_search for concept questions (\"where is retry handled\", \"how are sessions stored\") and Grep for exact strings or symbols."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What you are looking for, in natural language."},
            "k": {"type": "integer", "description": "Number of results (default 8, max 20)."},
            "path_glob": {"type": "string", "description": "Only files matching this glob, e.g. 'src/**/*.py'."},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    @property
    def hidden(self) -> bool:      # type: ignore[override]
        return not _ACTIVE["on"]

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        if is_error:
            return "semantic search unavailable"
        n = sum(1 for ln in str(output).splitlines() if ln[:1].isdigit() and ". " in ln[:5])
        return f"{n} results"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        s = ctx.settings
        if not getattr(s, "embedding_enabled", False):
            raise ToolError("Semantic search is off (/index sets it up). Use Grep.")
        ws = Path(ctx.workspace).resolve()
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("semantic_search needs a 'query'.")
        try:
            k = max(1, min(20, int(args.get("k") or 8)))
        except (TypeError, ValueError):
            k = 8
        cfg = resolve(s)
        if not sx.index_exists(ws):
            raise ToolError("No semantic index yet (run /index). Use Grep.")
        if not sx.has_consent(ws, cfg):
            raise ToolError(f"Sending to {cfg.host} is not confirmed for this project (run /index). Use Grep.")
        try:
            hits, info = sx.search(ws, cfg, make_embedder(s, cfg), query, k, str(args.get("path_glob") or "") or None)
        except EmbedError as e:
            raise ToolError(f"Semantic search failed: {e}. Use Grep instead.") from None
        if not hits:
            return "No results."
        out = []
        for i, h in enumerate(hits, 1):
            out.append(f"{i}. {h.path}:{h.start}-{h.end}  score {h.score:.3f}" + ("  (file changed since indexing)" if h.stale else ""))
            snip = sx.snippet(ws, h)
            if snip:
                out.append("\n".join("   " + ln for ln in snip.splitlines()))
        if info["refreshed"]:
            out.append(f"Note: re-indexed {len(info['refreshed'])} changed file(s) for this query.")
        if info["stale"]:
            out.append(f"Note: {len(info['stale'])} result file(s) changed since indexing, so line ranges may be off ({', '.join(info['stale'][:3])}); /index refreshes.")
        return "\n".join(out)


TOOLS = [SemanticSearchTool()]


# ─── command ─────────────────────────────────────────────────────────────────────────────────────────────

def _fmt_cost(plan: sx.Plan, cfg: EmbedConfig) -> str:
    c = plan.cost(cfg.backend)
    return f"~{plan.tokens:,} tokens" + (f", roughly ${c:.2f}" if c is not None and not cfg.is_local else "")


def status_lines(settings: Any) -> list[str]:
    cfg, ws = resolve(settings), Path(settings.workspace)
    st = sx.load_store(ws)
    pr = sx.progress_for(ws)
    lines = [f"Semantic search: {'on' if cfg.enabled else 'off'}",
             f"Backend: {cfg.backend}  model {cfg.model}  host {cfg.host}" + ("  (local)" if cfg.is_local else "  (REMOTE: file contents are sent there)"),
             f"Key variable: {cfg.key_env or '(none)'}" + ("" if not cfg.key_env else "  set" if lookup_key(cfg, ws) else "  NOT SET"),
             f"Index: {st.n} chunks in {len(st.files)} files, dim {st.dim or '-'}, limit {cfg.max_chunks}" + ("  (built with " + st.identity.replace("|", " / ") + ")" if st.identity and st.identity != cfg.identity else ""),
             f"Remote send confirmed: {'yes' if sx.has_consent(ws, cfg) else 'no'}"]
    if st.n:
        lines.append(f"Changed since indexing: {len(sx.stale_files(ws, st))} files")
    lines.append("Progress: " + pr.line())
    return lines


def _flag(args: list[str], name: str) -> str | None:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    for a in args:
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def _confirm_send(ws: Path, cfg: EmbedConfig, plan: sx.Plan, yes: bool, ask: Any) -> bool:
    if cfg.is_local:
        return True
    big = plan.chunks > sx.BIG_INDEX_CHUNKS
    if yes or (sx.has_consent(ws, cfg) and not big):
        sx.give_consent(ws, cfg)
        return True
    ok = ask(f"Send {plan.chunks:,} chunks from {len(plan.changed)} files ({_fmt_cost(plan, cfg)}) to {cfg.host}? File contents leave this machine.")
    if ok:
        sx.give_consent(ws, cfg)
    return bool(ok)


def run_index(settings: Any, args: list[str], say: Any, ask: Any, wait: bool = False, on_change: Any = None) -> bool:
    """Shared body of /index and `axon index`. ``say`` prints a line, ``ask`` is a y/n prompt."""
    ws = Path(settings.workspace).resolve()
    cfg = resolve(settings)
    yes = "--yes" in args or "-y" in args
    if "--status" in args or "status" in args:
        for ln in status_lines(settings):
            say(ln)
        return True
    pr = sx.progress_for(ws)
    if "--cancel" in args:
        pr.cancel.set()
        say("Cancelling the running index." if pr.running else "No index is running.")
        return True
    if not cfg.enabled:
        say("Semantic search is off. Set it up: /index --backend ollama --model nomic-embed-text")
        return False
    if pr.running:
        say(pr.line())
        return True
    plan = sx.make_plan(ws, cfg, None if "--rebuild" not in args else sx.Store(vecs=sx.array("f")))
    if plan.over_limit:
        say(f"The workspace would exceed the {cfg.max_chunks:,}-chunk limit (semanticSearch.maxChunks); narrow it with .axonignore.")
        return False
    if not plan.changed and not plan.removed and "--rebuild" not in args:
        say(f"Index is up to date ({plan.unchanged} files).")
        return True
    if not _confirm_send(ws, cfg, plan, yes, ask):
        say("Not indexed.")
        return False
    embedder = make_embedder(settings, cfg)
    done = lambda p: on_change and on_change()          # noqa: E731
    run = sx.start_background(ws, cfg, embedder, rebuild="--rebuild" in args, on_done=done)
    if run is None:
        say(pr.line())
        return True
    say(f"Indexing {plan.chunks:,} chunks from {len(plan.changed)} files in the background ({_fmt_cost(plan, cfg)}). /index --status shows progress.")
    if wait:
        import time
        last = ""
        while run.running:
            line = run.line()
            if line != last and sys.stdout.isatty():
                sys.stdout.write("\r\x1b[2K  " + line)
                sys.stdout.flush()
                last = line
            time.sleep(0.3)
        if last:
            sys.stdout.write("\r\x1b[2K")
        say(run.line())
        return not run.error
    return True


def _setup(agent: Any, args: list[str], say: Any) -> bool:
    from axon.commands.common import update_settings, update_settings_file
    cur = resolve(agent.settings)
    backend = (_flag(args, "--backend") or cur.backend).lower()
    if backend not in BACKENDS:
        say(f"Unknown backend '{backend}'. Choose one of: {', '.join(BACKENDS)}.")
        return False
    d = DEFAULTS[backend]
    same = backend == cur.backend
    changes = {"embedding_backend": backend,
               "embedding_model": _flag(args, "--model") or (agent.settings.embedding_model if same and agent.settings.embedding_model else d["model"]),
               "embedding_base_url": _flag(args, "--url") or (agent.settings.embedding_base_url if same and agent.settings.embedding_base_url else d["base_url"]),
               "embedding_key_env": _flag(args, "--key-env") or (agent.settings.embedding_key_env if same and agent.settings.embedding_key_env else d["key_env"])}
    new = agent.settings.model_copy(update={**changes, "embedding_enabled": True})
    cfg = resolve(new)
    if cfg.key_env and not lookup_key(cfg, Path(new.workspace)):
        say(f"Set the key first: export {cfg.key_env}=... (the key is never stored in settings).")
        return False
    try:
        dim = make_embedder(new, cfg).probe()
    except EmbedError as e:
        say(f"The {backend} backend at {cfg.host} did not answer: {e}")
        return False
    update_settings(agent, **changes, embedding_enabled=True)
    update_settings_file({"semanticSearch.enabled": True, "semanticSearch.backend": backend, "semanticSearch.model": cfg.model,
                          "semanticSearch.baseUrl": cfg.base_url, "semanticSearch.keyEnv": cfg.key_env or None})
    say(f"Backend ok: {backend} / {cfg.model} at {cfg.host}, vectors of {dim} dimensions."
        + ("" if cfg.is_local else f" Note: file contents will be sent to {cfg.host}."))
    say("Run /index to build the index.")
    return True


def handle_index(agent: Any, arg: str) -> Any:
    from axon.commands.common import confirm, err, info, ok, update_settings, update_settings_file
    from axon.commands.registry import CommandResult
    args = arg.split()
    ws = Path(agent.settings.workspace)
    say = lambda m: info(m)      # noqa: E731
    if args and args[0] == "off":
        update_settings(agent, embedding_enabled=False)
        update_settings_file({"semanticSearch.enabled": False})
        pr = sx.progress_for(ws)
        pr.cancel.set()
        if "--delete" in args or (sx.index_exists(ws) and confirm("Delete the stored index too?", default=False)):
            sx.delete_index(ws)
            ok("Semantic search off, index deleted.")
        else:
            ok("Semantic search off (the index is kept).")
        sync(agent)
        return CommandResult(handled=True)
    if "--backend" in args or "--model" in args or any(a.startswith(("--backend=", "--model=")) for a in args):
        if _setup(agent, args, say):
            sync(agent)
        return CommandResult(handled=True)
    run_index(agent.settings, args, say, lambda q: confirm(q, default=False), on_change=lambda: sync(agent))
    sync(agent)
    return CommandResult(handled=True)


def cli_index(argv: list[str]) -> int:
    """`axon index [--status|--rebuild|--yes]`: build or update the index in the foreground with one status line."""
    from axon.config import Settings
    settings = Settings.load(require_api_key=False)
    say = lambda m: print(m)    # noqa: E731

    def ask(q: str) -> bool:
        try:
            return input(f"{q} [y/N] ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False
    return 0 if run_index(settings, argv, say, ask, wait=True) else 1


COMMANDS = [
    FeatureCommand(name="/index", handler=handle_index, category="Context", usage=USAGE,
                   description="Semantic code search index (opt-in; Ollama, OpenAI-compatible or Voyage embeddings)"),
]
CLI_SUBCOMMANDS = {"index": cli_index}
