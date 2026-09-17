import os
import re
import sys
import time
from pathlib import Path
import httpx
from anthropic import Anthropic

# ─── Load .env if present ─────────────────────────────────────────────────────
search_dirs = [Path.cwd(), Path(__file__).resolve().parent, Path.home() / ".axon"]
loaded_env_file = None

for folder in search_dirs:
    env_file = folder / ".env"
    if env_file.exists() and env_file.is_file():
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if (k.startswith("AXON_") or k in ("AGENTROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")) and k not in os.environ:
                            os.environ[k] = v
            if not loaded_env_file:
                loaded_env_file = env_file
        except Exception:
            pass

API_KEY = os.environ.get("AXON_API_KEY") or os.environ.get("AGENTROUTER_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY") or "sk-placeholder"
BASE_URL = os.environ.get("AXON_BASE_URL", "https://agentrouter.org").rstrip("/")
OPENAI_URL = f"{BASE_URL}/v1/chat/completions"

# ─── Model Registry & File-Loaded List ─────────────────────────────────────────
DEFAULT_MODELS = [
    "deepseek-v4-flash",
    "gpt-5.6-sol",
    "gpt-6-astra",
    "claude-opus-5",
    "claude-opus-4-8",
]

# Load models from .env if AXON_AVAILABLE_MODELS is specified, otherwise use default
file_models_raw = os.environ.get("AXON_AVAILABLE_MODELS")
if file_models_raw:
    FILE_MODELS = [m.strip() for m in file_models_raw.split(",") if m.strip()]
else:
    FILE_MODELS = list(DEFAULT_MODELS)

MODELS = list(FILE_MODELS)
ANTHROPIC_MODELS = {m for m in MODELS if "claude" in m.lower()}

PROMPT = "Say 'OK' and state your model name."
INTERVAL_SECONDS = 5
TIMEOUT = 20.0

HEADERS = {
    "authorization": f"Bearer {API_KEY}",
    "content-type": "application/json",
    "user-agent": "Anthropic/Python 1.0.0",
    "x-stainless-lang": "python",
    "x-stainless-os": "MacOS",
    "x-stainless-arch": "arm64",
    "x-stainless-runtime": "CPython",
}

anthropic_client = Anthropic(auth_token=API_KEY, base_url=BASE_URL, timeout=TIMEOUT)


def _extract_clean_error(err_str: str) -> str:
    """Extract human-readable error messages from HTTP or JSON response exceptions."""
    match = re.search(r"['\"]message['\"]:\s*['\"]([^'\"]+)['\"]", err_str)
    if match:
        return match.group(1).strip()
    return err_str.strip()


def test_model(model: str) -> tuple[bool, float, str]:
    t0 = time.time()
    try:
        if model in ANTHROPIC_MODELS:
            res = anthropic_client.messages.create(
                model=model,
                max_tokens=20,
                messages=[{"role": "user", "content": PROMPT}],
            )
            reply = "".join(b.text for b in res.content if hasattr(b, "text")).strip()
            return True, (time.time() - t0) * 1000, reply or "OK"
        else:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": PROMPT}],
                "max_tokens": 20,
            }
            r = httpx.post(OPENAI_URL, headers=HEADERS, json=payload, timeout=TIMEOUT)
            elapsed = (time.time() - t0) * 1000
            if r.status_code == 200:
                msg = r.json().get("choices", [{}])[0].get("message", {})
                reply = (msg.get("content") or "").strip()
                if not reply:
                    reply = "OK"
                return True, elapsed, reply.replace("\n", " ")[:60]
            else:
                try:
                    data = r.json()
                    err = data.get("error", {}).get("message") or data.get("message") or f"HTTP {r.status_code}"
                except Exception:
                    err = f"HTTP {r.status_code}: {r.text[:60]}"
                return False, elapsed, str(err)[:60]
    except Exception as e:
        clean = _extract_clean_error(str(e))
        return False, (time.time() - t0) * 1000, clean[:60]


def sort_results(results: list[tuple[str, bool, float, str]], sort_by: str) -> list[tuple[str, bool, float, str]]:
    """
    Sort test results by chosen strategy:
      - 'file': Order as defined in the configuration file (.env)
      - 'fail': Failed tests first (or grouped by failure status), followed by working
      - 'latency': Lowest response latency first
      - 'name': Alphabetical model name
    """
    if sort_by == "fail":
        # Failed (ok=False) first, then by latency
        return sorted(results, key=lambda x: (1 if x[1] else 0, x[2]))
    elif sort_by == "name":
        return sorted(results, key=lambda x: x[0].lower())
    elif sort_by == "latency":
        return sorted(results, key=lambda x: (0 if x[1] else 1, x[2]))
    elif sort_by == "file":
        # Keep exact index in FILE_MODELS
        def file_order(item: tuple[str, bool, float, str]) -> int:
            try:
                return FILE_MODELS.index(item[0])
            except ValueError:
                return 999
        return sorted(results, key=file_order)
    return results


def main():
    if API_KEY == "sk-placeholder" or not API_KEY:
        print("\033[93m⚠️ Warning: No AXON_API_KEY found in environment or .env file.\033[0m", flush=True)
        print("Please configure your API key in .env or export AXON_API_KEY before running.\n", flush=True)

    max_rounds = 2
    sort_mode = "file"  # default sort by file (.env file ordering)
    output_file = None

    # Parse CLI arguments
    args = sys.argv[1:]
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
        elif arg.startswith("--output=") or arg.startswith("--file="):
            output_file = arg.split("=", 1)[1].strip()
        elif arg in ("-o", "--output", "--file") and i + 1 < len(args):
            i += 1
            output_file = args[i].strip()
        i += 1

    rounds_label = "1 round" if max_rounds == 1 else ("2 rounds" if max_rounds == 2 else (f"{max_rounds} rounds" if max_rounds > 0 else "Continuous"))
    file_src = f"from {loaded_env_file.name}" if loaded_env_file else "default list"
    print(f"⚡ Testing connectivity for {len(MODELS)} models ({rounds_label} · Sort: {sort_mode} · Base: {BASE_URL}) [{file_src}]...\n", flush=True)

    round_num = 1
    file_output_lines = []

    while True:
        round_header = f"--- [Round #{round_num} of {max_rounds}] {time.strftime('%H:%M:%S')} (Sorted by: {sort_mode}) ---" if max_rounds > 0 else f"--- [Round #{round_num}] {time.strftime('%H:%M:%S')} (Sorted by: {sort_mode}) ---"
        print(round_header, flush=True)
        if output_file:
            file_output_lines.append(round_header)

        # Collect raw results
        current_results: list[tuple[str, bool, float, str]] = []
        for model in MODELS:
            ok, lat, info = test_model(model)
            current_results.append((model, ok, lat, info))

        # Sort results based on sort_mode
        sorted_res = sort_results(current_results, sort_mode)

        for model, ok, lat, info in sorted_res:
            status = "\033[92m● WORKING\033[0m" if ok else "\033[91m✖ FAILED \033[0m"
            raw_status = "● WORKING" if ok else "✖ FAILED "
            line = f"{model:<20} | {status} | {lat:>6.0f} ms | {info}"
            plain_line = f"{model:<20} | {raw_status} | {lat:>6.0f} ms | {info}"
            print(line, flush=True)
            if output_file:
                file_output_lines.append(plain_line)

        working_count = sum(1 for _, ok, _, _ in sorted_res if ok)
        failed_count = len(sorted_res) - working_count
        summary_line = f"   └─ Summary: {working_count} working, {failed_count} failed / quota-exhausted"
        print(f"\n{summary_line}\n", flush=True)
        if output_file:
            file_output_lines.append(summary_line + "\n")

        if max_rounds > 0 and round_num >= max_rounds:
            comp_msg = f"✓ Model verification complete ({max_rounds} {'round' if max_rounds == 1 else 'rounds'} finished)."
            print(comp_msg, flush=True)
            if output_file:
                file_output_lines.append(comp_msg)
                try:
                    with open(output_file, "w", encoding="utf-8") as f_out:
                        f_out.write("\n".join(file_output_lines) + "\n")
                    print(f"✓ Saved results to {output_file}", flush=True)
                except Exception as e:
                    print(f"⚠️ Failed to write to {output_file}: {e}", flush=True)
            break

        round_num += 1
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
