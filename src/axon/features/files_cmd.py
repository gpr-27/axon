"""`/files` and `axon files`: the attachments Axon keeps once and reuses.

    /files                  list: name, size, how often it was sent, and its file id (official Anthropic host) or local copy
    /files list             the same
    /files delete <id|name|hash-prefix>   delete one (the uploaded file too, when it has an id)
    /files clear            delete every uploaded file and forget every cached attachment

Large images, PDFs and text files (256 KB and up, 500 MB at most) are uploaded once to the Files API on the official
Anthropic host and referenced by id afterwards. On other providers the attachment stays inline; Axon only keeps one
local copy per content hash. Files uploaded in a session are deleted when it ends (AXON_FILES_KEEP=1 keeps them).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from axon.providers import files_api as fa

FULL_USAGE = (__doc__ or "").strip()


def _human(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB" if n >= 1_048_576 else f"{max(1, n // 1024)} KB"


def run_files(settings: Any, arg: str, *, index: fa.Index | None = None, transport: Any = None,
              emit: Callable[[str], None] = print) -> int:
    parts = (arg or "").split()
    sub, rest = (parts[0], parts[1:]) if parts else ("list", [])
    idx = index or fa.Index()
    from axon.features.batch_run import native_credentials
    cred = native_credentials(settings)
    sess = fa.FileSession(cred[0], cred[1], transport=transport, index=idx) if cred else fa.FileSession("", "", index=idx)
    data = idx.load()
    if sub in ("-h", "--help", "help"):
        emit(FULL_USAGE)
        return 0
    if sub == "list":
        if not data:
            emit("No cached attachments yet.")
        for e in sorted(data.values(), key=lambda x: x.get("first_seen", ""), reverse=True):
            where = f"file id {e['file_id']} · {fa.NOTE}" if e.get("file_id") else (
                f"local copy · {fa.LOCAL_NOTE}" if e.get("local_copy") else "not uploaded")
            emit(f"  {e['sha'][:10]}  {e['name']}  {e['mime']}  {_human(int(e.get('size', 0)))}  sent {e.get('uses', 0)}x  {where}")
        return 0
    if sub == "delete":
        if not rest:
            emit("files: give an id, a name or a hash prefix (`/files list`).")
            return 1
        q = rest[0]
        hits = [s for s, e in data.items() if q in (e.get("file_id"), e.get("name")) or s.startswith(q)]
        if not hits:
            emit(f"files: nothing matches {q!r}.")
            return 1
        for s in hits:
            _drop(sess, idx, data, s)
        idx.save(data)
        emit(f"Deleted {len(hits)} attachment(s).")
        return 0
    if sub == "clear":
        n = len(data)
        for s in list(data):
            _drop(sess, idx, data, s)
        idx.save(data)
        emit(f"Cleared {n} attachment(s).")
        return 0
    emit(f"files: unknown subcommand {sub!r}.\n{FULL_USAGE}")
    return 1


def _drop(sess: fa.FileSession, idx: fa.Index, data: dict[str, dict[str, Any]], sha: str) -> None:
    e = data.pop(sha, {})
    fid = e.get("file_id")
    if fid and sess.native:
        try:
            sess.client().delete(fid)
        except Exception:                                   # noqa: BLE001
            pass
    idx.blob(sha).unlink(missing_ok=True)


def handle_files(agent: Any, arg: str) -> bool:
    run_files(agent.settings, arg)
    return True


def files_main(argv: list[str]) -> int:
    from axon.config import Settings
    settings = Settings.load({"workspace": Path.cwd()}, require_api_key=False)
    return run_files(settings, " ".join(argv))


try:
    from axon.features import FeatureCommand
    COMMANDS = [FeatureCommand(name="/files", handler=handle_files, category="Features", usage=FULL_USAGE,
                               description="Attachments kept once: Files API ids on Anthropic, a local cache elsewhere")]
except Exception:                                          # pragma: no cover
    COMMANDS = []

CLI_SUBCOMMANDS = {"files": files_main}
__all__ = ["COMMANDS", "CLI_SUBCOMMANDS", "handle_files", "files_main", "run_files"]
