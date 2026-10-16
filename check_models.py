"""
Model connectivity checker: streams a tiny prompt to each model through Axon's real provider layer
(same routing, auth, retries and error messages as the app) and prints a latency table.

Usage:
    python check_models.py [--once | -c | N] [--sort file|fail|latency|name] [--timeout SECONDS] [-o results.txt]
    AXON_AVAILABLE_MODELS="gpt-5,anthropic/claude-sonnet-4-5" python check_models.py --once
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent  # repository root
if (_ROOT / "src" / "axon").is_dir() and str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

PROMPT = "Say 'OK' and state your model name."
INTERVAL_SECONDS = 5
DEFAULT_TIMEOUT_SECONDS = 30.0  # per model; a server that never answers is reported, not waited on
# Used only when Axon's catalog can't be imported; normally the provider's live model list is used.
FALLBACK_MODELS = ["deepseek-v4-flash", "claude-opus-5-5", "gpt-6.1-sol", "gpt-6-astra"]


def _catalog_models(base_url: str) -> tuple[list[str], str]:
    """The models Axon offers for the provider at ``base_url`` (same list as /model), and its name."""
    try:
        from axon.providers.catalog import find_preset_by_url, get_preset_by_id
        preset = find_preset_by_url(base_url) or get_preset_by_id("agentrouter")
        if preset is not None and preset.models:
            return list(preset.models), preset.name
    except Exception:
        pass
    return list(FALLBACK_MODELS), "AgentRouter"


def _load_env_files() -> Path | None:
    """Load KEY=value pairs (provider keys and AXON_*) from ./.env, the script dir and ~/.axon/.env."""
    loaded: Path | None = None
    for folder in (Path.cwd(), _ROOT, Path.home() / ".axon"):
        env_file = folder / ".env"
        if not env_file.is_file():
            continue
        try:
            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:]
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if (k.startswith("AXON_") or k.endswith("_API_KEY") or k.startswith("AZURE_OPENAI_")) and k not in os.environ:
                    os.environ[k] = v
            loaded = loaded or env_file
        except OSError:
            pass
    return loaded


loaded_env_file = _load_env_files()
BASE_URL = os.environ.get("AXON_BASE_URL", "https://agentrouter.org").rstrip("/")
API_KEY = os.environ.get("AXON_API_KEY") or os.environ.get("AGENTROUTER_API_KEY") or ""
_file_models = os.environ.get("AXON_AVAILABLE_MODELS")
DEFAULT_MODELS, PROVIDER_NAME = _catalog_models(BASE_URL)
FILE_MODELS = [m.strip() for m in _file_models.split(",") if m.strip()] if _file_models else list(DEFAULT_MODELS)
MODELS = list(FILE_MODELS)


def _console_setup() -> bool:
    """Enable ANSI on Windows consoles when possible. Returns True if colors should be used."""
    try:
        from axon.platform import setup_console  # type: ignore[attr-defined]
        setup_console()
    except Exception:
        if os.name == "nt":
            os.system("")  # enables VT processing on Windows 10+ consoles
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


USE_COLOR = _console_setup()


def _supports(text: str) -> bool:
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


OK_MARK = "● WORKING" if _supports("●✖") else "* WORKING"
FAIL_MARK = "✖ FAILED " if _supports("●✖") else "x FAILED "


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def out(text: str = "") -> None:
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(enc, errors="replace").decode(enc, errors="replace"), flush=True)


def _extract_clean_error(err_str: str) -> str:
    """Human-readable message from an HTTP/JSON error string."""
    match = re.search(r"['\"]message['\"]:\s*['\"]([^'\"]+)['\"]", err_str)
    if match:
        return match.group(1).strip()
    first = err_str.strip().splitlines()[0] if err_str.strip() else err_str
    return first.strip()


def test_model(model: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> tuple[bool, float, str]:
    """Stream PROMPT to `model`. Returns (ok, latency_ms, reply_or_error).

    The call runs in a worker thread so a server that accepts the request but never replies
    is reported as a timeout after `timeout` seconds instead of blocking for minutes.
    """
    box: dict[str, object] = {}
    holder: dict[str, object] = {}

    def work() -> None:
        from pydantic import SecretStr
        from axon.config import Settings
        from axon.providers.registry import provider_for

        t0 = time.time()
        try:
            settings = Settings(base_url=BASE_URL, api_key=SecretStr(API_KEY), model=model, thinking=False)
            provider = provider_for(model, settings, failover=False)
            holder["provider"] = provider
            for _ in provider.stream(
                model=model,
                system=[],
                messages=[{"role": "user", "content": PROMPT}],
                tools=[],
                max_tokens=64,
                effort=None,
                thinking=False,
            ):
                pass
            turn = provider.finalize()
            reply = (turn.text or "OK").strip().replace("\n", " ")
            box["result"] = (True, (time.time() - t0) * 1000, reply[:60])
        except BaseException as e:  # noqa: BLE001 - report any failure as data
            box["result"] = (False, (time.time() - t0) * 1000, _extract_clean_error(str(e))[:60])

    t0 = time.time()
    worker = threading.Thread(target=work, name=f"check-{model}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        cancel = getattr(holder.get("provider"), "cancel", None)
        if callable(cancel):
            try:
                cancel(worker.ident)
            except Exception:
                pass
        return False, (time.time() - t0) * 1000, f"no reply after {timeout:.0f}s (server accepted the request but never answered)"
    return box.get("result", (False, (time.time() - t0) * 1000, "no result"))  # type: ignore[return-value]


def run_round(models: list[str], timeout: float) -> list[tuple[str, bool, float, str]]:
    """Test every model at once; total wait is at most `timeout`, not timeout x models."""
    slots: dict[str, tuple[bool, float, str]] = {}

    def one(m: str) -> None:
        try:
            slots[m] = test_model(m, timeout)
        except BaseException as e:  # noqa: BLE001
            slots[m] = (False, 0.0, _extract_clean_error(str(e))[:60])

    threads = [threading.Thread(target=one, args=(m,), daemon=True) for m in models]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [(m, *slots.get(m, (False, 0.0, "no result"))) for m in models]


def sort_results(results: list[tuple[str, bool, float, str]], sort_by: str) -> list[tuple[str, bool, float, str]]:
    """Sort by 'file' (config order), 'fail' (failures first), 'latency' or 'name'."""
    if sort_by == "fail":
        return sorted(results, key=lambda x: (1 if x[1] else 0, x[2]))
    if sort_by == "name":
        return sorted(results, key=lambda x: x[0].lower())
    if sort_by == "latency":
        return sorted(results, key=lambda x: (0 if x[1] else 1, x[2]))
    if sort_by == "file":
        def file_order(item: tuple[str, bool, float, str]) -> int:
            try:
                return FILE_MODELS.index(item[0])
            except ValueError:
                return 999
        return sorted(results, key=file_order)
    return results


def _parse_args(args: list[str]) -> tuple[int, str, str | None, float]:
    max_rounds, sort_mode, output_file, timeout = 2, "file", None, DEFAULT_TIMEOUT_SECONDS
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--continuous", "-c"):
            max_rounds = 0
        elif arg in ("--once", "-1"):
            max_rounds = 1
        elif arg.isdigit():
            max_rounds = int(arg)
        elif arg in ("--sort-by-file", "--sort-file", "--file-order"):
            sort_mode = "file"
        elif arg in ("--sort-by-fail", "--sort-fail", "--fail-first"):
            sort_mode = "fail"
        elif arg.startswith("--sort="):
            sort_mode = arg.split("=", 1)[1].strip().lower()
        elif arg == "--sort" and i + 1 < len(args):
            i += 1
            sort_mode = args[i].strip().lower()
        elif arg.startswith("--timeout="):
            timeout = max(1.0, float(arg.split("=", 1)[1]))
        elif arg == "--timeout" and i + 1 < len(args):
            i += 1
            timeout = max(1.0, float(args[i]))
        elif arg.startswith(("--output=", "--file=")):
            output_file = arg.split("=", 1)[1].strip()
        elif arg in ("-o", "--output", "--file") and i + 1 < len(args):
            i += 1
            output_file = args[i].strip()
        i += 1
    return max_rounds, sort_mode, output_file, timeout


def main() -> None:
    if not API_KEY:
        out(_c("93", "Warning: no AXON_API_KEY found; models on other providers use their own *_API_KEY variables."))
    max_rounds, sort_mode, output_file, timeout = _parse_args(sys.argv[1:])
    rounds_label = "Continuous" if max_rounds == 0 else f"{max_rounds} round{'s' if max_rounds != 1 else ''}"
    src = f"from {loaded_env_file.name}" if loaded_env_file else "default list"
    out(f"Testing connectivity for {len(MODELS)} models ({rounds_label} - Sort: {sort_mode} - Base: {BASE_URL}) [{src}]...")
    out(f"All models are tested at once; each gets {timeout:.0f}s (change with --timeout SECONDS).\n")

    round_num = 1
    log: list[str] = []
    while True:
        header = (f"--- [Round #{round_num}{f' of {max_rounds}' if max_rounds else ''}] {time.strftime('%H:%M:%S')} "
                  f"· provider {PROVIDER_NAME} ({BASE_URL}) (Sorted by: {sort_mode}) ---")
        out(header)
        log.append(header)
        results = run_round(MODELS, timeout)
        ordered = sort_results(results, sort_mode)  # type: ignore[arg-type]
        for model, ok, lat, info in ordered:
            mark = OK_MARK if ok else FAIL_MARK
            out(f"{model:<28} | {_c('92' if ok else '91', mark)} | {lat:>6.0f} ms | {info}")
            log.append(f"{model:<28} | {mark} | {lat:>6.0f} ms | {info}")
        working = sum(1 for _, ok, _, _ in ordered if ok)
        summary = f"   Summary: {working} working, {len(ordered) - working} failed / quota-exhausted"
        out(f"\n{summary}\n")
        log.append(summary + "\n")
        if max_rounds and round_num >= max_rounds:
            done = f"Model verification complete ({max_rounds} round{'s' if max_rounds != 1 else ''} finished)."
            out(done)
            if output_file:
                log.append(done)
                try:
                    Path(output_file).write_text("\n".join(log) + "\n", encoding="utf-8")
                    out(f"Saved results to {output_file}")
                except OSError as e:
                    out(f"Failed to write to {output_file}: {e}")
            break
        round_num += 1
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        out("\nStopped.")
