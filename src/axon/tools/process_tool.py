"""
Process tool: list/find running processes, show listening TCP ports and system resource stats
on Windows, macOS and Linux.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, ClassVar

from axon import platform as plat
from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext


def _run(argv: list[str], timeout: float = 10.0) -> str:
    try:
        return plat.run_text(argv, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        raise ToolError(f"{argv[0]} failed: {e}") from e


def _listening_ports() -> str:
    if plat.IS_WINDOWS:
        out = _run(["netstat", "-ano", "-p", "TCP"])
        lines = [ln for ln in out.splitlines() if "LISTENING" in ln.upper()]
        return "Proto  Local Address          Foreign Address        State           PID\n" + "\n".join(lines[:60]) if lines else ""
    if shutil.which("lsof"):
        return _run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])
    if shutil.which("ss"):
        return _run(["ss", "-ltnp"])
    if shutil.which("netstat"):
        out = _run(["netstat", "-tlnp"])
        return "\n".join(ln for ln in out.splitlines() if "LISTEN" in ln.upper())
    raise ToolError("No port inspection utility found (lsof, ss or netstat).")


def _process_lines() -> tuple[str, list[str]]:
    if plat.IS_WINDOWS:
        out = _run(["tasklist", "/FO", "CSV", "/NH"])
        parsed: list[tuple[int, str]] = []
        for ln in out.splitlines():
            cols = [c.strip('"') for c in ln.split('","')]
            if len(cols) >= 5:
                mem_kb = int("".join(ch for ch in cols[4] if ch.isdigit()) or 0)
                parsed.append((mem_kb, f"{cols[1]:>7}  {mem_kb / 1024:>9.1f} MB  {cols[0]}"))
        parsed.sort(key=lambda t: -t[0])
        return f"{'PID':>7}  {'MEMORY':>12}  NAME", [row for _, row in parsed]
    if not shutil.which("ps"):
        raise ToolError("'ps' is not available.")
    out = _run(["ps", "-eo", "pid,pcpu,pmem,stat,etime,command"]).splitlines()
    header, rows = (out[0], out[1:]) if out else ("", [])

    def cpu(row: str) -> float:
        parts = row.split()
        try:
            return float(parts[1])
        except (IndexError, ValueError):
            return 0.0
    rows.sort(key=cpu, reverse=True)
    return header, rows


def _memory() -> tuple[int, int] | None:
    """(total, available) bytes."""
    if plat.IS_WINDOWS:
        import ctypes

        class MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMSTAT()
        st.dwLength = ctypes.sizeof(MEMSTAT)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):  # type: ignore[attr-defined]
            return int(st.ullTotalPhys), int(st.ullAvailPhys)
        return None
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            info = {ln.split(":")[0]: int(ln.split()[1]) * 1024 for ln in f if ln.split()[1:2] and ln.split()[1].isdigit()}
        return info.get("MemTotal", 0), info.get("MemAvailable", info.get("MemFree", 0))
    except OSError:
        pass
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        avail = None
        if shutil.which("vm_stat"):
            out = _run(["vm_stat"])
            page = 4096
            first = out.splitlines()[0] if out else ""
            if "page size of" in first:
                page = int("".join(ch for ch in first.split("page size of")[1] if ch.isdigit()) or 4096)
            pages = 0
            for ln in out.splitlines():
                if ln.startswith(("Pages free", "Pages inactive", "Pages speculative")):
                    pages += int(ln.split(":")[1].strip().rstrip("."))
            avail = pages * page
        return total, avail if avail is not None else 0
    except (ValueError, OSError, AttributeError):
        return None


def _gib(n: int) -> str:
    return f"{n / (1024 ** 3):.1f} GiB"


def _stats(ctx: ToolContext) -> str:
    lines = [f"CPUs        : {os.cpu_count() or '?'}"]
    if hasattr(os, "getloadavg"):
        try:
            l1, l5, l15 = os.getloadavg()
            lines.append(f"Load avg    : {l1:.2f} {l5:.2f} {l15:.2f} (1/5/15 min)")
        except OSError:
            pass
    mem = _memory()
    if mem and mem[0]:
        total, avail = mem
        used = total - avail
        lines.append(f"Memory      : {_gib(used)} used / {_gib(total)} ({100 * used / total:.0f}%)")
    try:
        du = shutil.disk_usage(str(ctx.workspace))
        lines.append(f"Disk (ws)   : {_gib(du.used)} used / {_gib(du.total)} ({_gib(du.free)} free)")
    except OSError:
        pass
    return "\n".join(lines)


class ProcessTool(Tool):
    name: ClassVar[str] = "Process"
    description: ClassVar[str] = (
        "Inspect the local machine: 'list' top processes by CPU, 'find' processes matching a pattern, "
        "'ports' listening TCP ports (e.g. dev servers on :3000/:8000) with owning PIDs, and 'stats' "
        "CPU/load/memory/disk usage. Read-only; works on Windows, macOS and Linux."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "ports", "find", "stats"],
                       "description": "list | find | ports | stats"},
            "pattern": {"type": "string", "description": "Case-insensitive text to match (for 'find' or to filter 'ports')"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        action = str(args.get("action") or "list").lower()
        pattern = str(args.get("pattern") or "").strip().lower()
        if action == "ports":
            out = _listening_ports()
            if pattern:
                out = "\n".join(ln for i, ln in enumerate(out.splitlines()) if i == 0 or pattern in ln.lower())
            return f"Listening TCP ports:\n{out}" if out.strip() else "No listening TCP ports found."
        if action in ("list", "find"):
            header, rows = _process_lines()
            if action == "find" or pattern:
                if not pattern:
                    raise ToolError("'find' requires a 'pattern'.")
                rows = [r for r in rows if pattern in r.lower()]
                if not rows:
                    return f"No processes found matching '{pattern}'."
                return f"{header}\n" + "\n".join(rows[:40])
            return f"{header}\n" + "\n".join(rows[:25])
        if action == "stats":
            return _stats(ctx)
        raise ToolError(f"Unknown action '{action}'. Use 'list', 'find', 'ports' or 'stats'.")
