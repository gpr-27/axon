import os
import sys
import time
from pathlib import Path
import httpx
from anthropic import Anthropic

# ─── Load .env if present ─────────────────────────────────────────────────────
search_dirs = [Path.cwd(), Path(__file__).resolve().parent, Path.home() / ".axon"]
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
        except Exception:
            pass

API_KEY = os.environ.get("AXON_API_KEY") or os.environ.get("AGENTROUTER_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY") or "sk-placeholder"
BASE_URL = os.environ.get("AXON_BASE_URL", "https://agentrouter.org").rstrip("/")
OPENAI_URL = f"{BASE_URL}/v1/chat/completions"
ANTHROPIC_MODELS = {"claude-opus-5", "claude-opus-4-8"}
MODELS = ["deepseek-v4-flash", "gpt-5.6-sol", "glm-5.3", "claude-opus-5", "claude-opus-4-8"]

PROMPT = "Say 'OK' and state your model name."
INTERVAL_SECONDS = 5
TIMEOUT = 10.0

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
                reply = msg.get("content") or msg.get("reasoning_content") or "OK"
                return True, elapsed, reply.strip().replace("\n", " ")[:60]
            else:
                err = r.json().get("error", {}).get("message", f"HTTP {r.status_code}")
                return False, elapsed, err[:60]
    except Exception as e:
        return False, (time.time() - t0) * 1000, str(e)[:60]


def main():
    if API_KEY == "sk-placeholder" or not API_KEY:
        print("\033[93m⚠️ Warning: No AXON_API_KEY found in environment or .env file.\033[0m", flush=True)
        print("Please configure your API key in .env or export AXON_API_KEY before running.\n", flush=True)

    max_rounds = 2
    for arg in sys.argv[1:]:
        if arg in ("--continuous", "-c"):
            max_rounds = 0
        elif arg.isdigit():
            max_rounds = int(arg)

    rounds_label = "2 rounds" if max_rounds == 2 else (f"{max_rounds} rounds" if max_rounds > 0 else "Continuous")
    print(f"⚡ Testing connectivity for {len(MODELS)} models ({rounds_label} · Base: {BASE_URL})...\n", flush=True)
    round_num = 1
    while True:
        round_header = f"--- [Round #{round_num} of {max_rounds}] {time.strftime('%H:%M:%S')} ---" if max_rounds > 0 else f"--- [Round #{round_num}] {time.strftime('%H:%M:%S')} ---"
        print(round_header, flush=True)
        for model in MODELS:
            ok, lat, info = test_model(model)
            status = "\033[92m● WORKING\033[0m" if ok else "\033[91m✖ FAILED \033[0m"
            print(f"{model:<20} | {status} | {lat:>6.0f} ms | {info}", flush=True)
        
        if max_rounds > 0 and round_num >= max_rounds:
            print("\n✓ Model verification complete (2 rounds finished).", flush=True)
            break
        round_num += 1
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")

