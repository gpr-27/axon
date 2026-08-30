import os
import time
import httpx
from anthropic import Anthropic

# ─── Config (Edit prompt, interval, or keys directly here) ────────────────────
API_KEY = os.environ.get("AXON_API_KEY") or os.environ.get("AGENTROUTER_API_KEY") or "sk-placeholder"
OPENAI_URL = "https://agentrouter.org/v1/chat/completions"
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

anthropic_client = Anthropic(auth_token=API_KEY, base_url="https://agentrouter.org", timeout=TIMEOUT)


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
    print(f"Starting continuous test for 5 models (Prompt: '{PROMPT}')...")
    round_num = 1
    while True:
        print(f"\n--- [Round #{round_num}] {time.strftime('%H:%M:%S')} ---")
        for model in MODELS:
            ok, lat, info = test_model(model)
            status = "\033[92m● WORKING\033[0m" if ok else "\033[91m✖ FAILED \033[0m"
            print(f"{model:<20} | {status} | {lat:>6.0f} ms | {info}")
        round_num += 1
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
