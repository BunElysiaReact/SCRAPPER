#!/usr/bin/env python3
"""
DeepSeek Chat via SCRAPPER
────────────────────────────────────────────────────────
INSTALL:  pip install curl_cffi
USAGE:    python3 deepseek_chat.py "your message"
          python3 deepseek_chat.py          ← test message
────────────────────────────────────────────────────────
"""

import json, sys, uuid, re, hashlib, time
from curl_cffi import requests

SCRAPPER_API  = "http://localhost:8080"
TARGET_DOMAIN = "chat.deepseek.com"
DEFAULT_MSG   = "Hello! What is 2+2?"
DEFAULT_MODEL = "deepseek_v3"   # deepseek_r1  |  deepseek_v3_0324


# ── SCRAPPER ──────────────────────────────────────────────────────────────────
def scrapper_get(path):
    try:
        r = requests.get(f"{SCRAPPER_API}{path}", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:
        print("\n[!] SCRAPPER not reachable — run: scrapper-start\n")
        sys.exit(1)


# ── PoW solver (DeepSeekHashV1 = sha3_256 brute-force) ───────────────────────
def solve_pow(challenge: dict) -> str:
    import base64
    c          = challenge["challenge"]
    salt       = challenge["salt"]
    difficulty = challenge["difficulty"]
    target     = (2 ** 256) // difficulty

    print(f"[*] Solving PoW (difficulty={difficulty})...", end=" ", flush=True)
    t0 = time.time()

    for answer in range(100_000_000):
        attempt = f"{c}{salt}{answer}"
        digest  = hashlib.new("sha3_256", attempt.encode()).digest()
        if int.from_bytes(digest, "big") < target:
            print(f"solved (answer={answer}, {time.time()-t0:.2f}s)")
            payload = json.dumps({
                "algorithm":   challenge["algorithm"],
                "challenge":   c,
                "salt":        salt,
                "answer":      answer,
                "signature":   challenge["signature"],
                "target_path": challenge["target_path"],
            }, separators=(",", ":"))
            return base64.b64encode(payload.encode()).decode()

    raise RuntimeError("PoW: failed to find answer in 100M attempts")


# ── Session ───────────────────────────────────────────────────────────────────
def build_session():
    print("[*] Fetching session from SCRAPPER...")
    cookies = scrapper_get(f"/api/v1/session/cookies?domain={TARGET_DOMAIN}")
    fp      = scrapper_get(f"/api/v1/fingerprint?domain={TARGET_DOMAIN}")
    recent  = scrapper_get(f"/api/v1/requests/recent?limit=300&domain={TARGET_DOMAIN}")

    if not cookies:
        print(f"\n[!] No cookies. Log into {TARGET_DOMAIN} and retry.\n"); sys.exit(1)

    bearer = None
    for req in reversed(recent):
        auth = req.get("headers", {}).get("authorization", "")
        if auth.startswith("Bearer "):
            bearer = auth.split(" ", 1)[1]; break
    if not bearer:
        print("\n[!] No bearer token. Browse DeepSeek more then retry.\n"); sys.exit(1)

    device_id = None
    for req in recent:
        m = re.search(r'did=([a-f0-9\-]{36})', req.get("url", ""))
        if m:
            device_id = m.group(1); break
    if not device_id:
        device_id = str(uuid.uuid4())

    ua = (fp.get("userAgent") if isinstance(fp, dict) else None) or \
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"

    session = requests.Session(impersonate="chrome120")
    for c in cookies:
        domain = c.get("domain", TARGET_DOMAIN).lstrip(".")
        session.cookies.set(c["name"], c["value"], domain=domain, path=c.get("path", "/"))

    session.headers.update({
        "User-Agent":               ua,
        "accept":                   "*/*",
        "authorization":            f"Bearer {bearer}",
        "content-type":             "application/json",
        "sec-ch-ua":                '"Not:A-Brand";v="99", "Brave";v="145", "Chromium";v="145"',
        "sec-ch-ua-mobile":         "?0",
        "sec-ch-ua-platform":       '"Linux"',
        "x-app-version":            "20241129.1",
        "x-client-locale":          "en_US",
        "x-client-platform":        "web",
        "x-client-timezone-offset": "10800",
        "x-client-version":         "1.7.0",
        "Origin":                   "https://chat.deepseek.com",
        "Referer":                  "https://chat.deepseek.com/",
    })

    print(f"[*] Bearer   : {bearer[:30]}...")
    print(f"[*] device_id: {device_id}")
    print(f"[*] Cookies  : {len(cookies)}")
    return session, device_id


# ── API helpers ───────────────────────────────────────────────────────────────
def get_user(session):
    r = session.get("https://chat.deepseek.com/api/v0/users/current")
    r.raise_for_status()
    return r.json()["data"]["biz_data"]


def create_chat_session(session):
    r = session.post(
        "https://chat.deepseek.com/api/v0/chat_session/create",
        json={"character_id": None}
    )
    if r.status_code != 200:
        print(f"[!] create_session {r.status_code}: {r.text[:200]}"); sys.exit(1)
    return r.json()["data"]["biz_data"]["id"]


def get_pow_challenge(session):
    r = session.post(
        "https://chat.deepseek.com/api/v0/chat/create_pow_challenge",
        json={"target_path": "/api/v0/chat/completion"}
    )
    r.raise_for_status()
    return r.json()["data"]["biz_data"]["challenge"]


def get_hif_token(session):
    """Fetch the x-hif-leim token required by the completion endpoint."""
    try:
        r = session.get("https://hif-leim.deepseek.com/query", timeout=5)
        if r.status_code == 200:
            val = r.json()["data"]["biz_data"]["value"]
            print(f"[*] hif-leim: {val[:20]}...")
            return val
    except Exception as e:
        print(f"[!] hif-leim fetch failed: {e}")
    return None


def send_message(session, chat_session_id, message, model):
    # Step 1: fetch hif-leim token
    hif_token = get_hif_token(session)
    if hif_token:
        session.headers["x-hif-leim"] = hif_token

    # Step 2: get & solve PoW
    challenge    = get_pow_challenge(session)
    pow_response = solve_pow(challenge)
    session.headers["x-ds-pow-response"] = pow_response

    payload = {
        "chat_session_id":   chat_session_id,
        "parent_message_id": None,
        "prompt":            message,
        "ref_file_ids":      [],
        "thinking_enabled":  "r1" in model,
        "search_enabled":    False,
        "preempt":           False,
    }

    r = session.post(
        "https://chat.deepseek.com/api/v0/chat/completion",
        json=payload,
        stream=True
    )

    session.headers.pop("x-ds-pow-response", None)
    session.headers.pop("x-hif-leim", None)

    if r.status_code == 401:
        print("\n[!] 401 — session expired. Log in again.\n"); return
    if r.status_code == 403:
        print(f"\n[!] 403: {r.text[:300]}\n"); return
    if r.status_code != 200:
        print(f"\n[!] {r.status_code}: {r.text[:300]}\n"); return

    # Stream SSE
    for raw in r.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw

        if line == "data: [DONE]":
            break
        if not line.startswith("data:"):
            continue

        try:
            data = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue

        choices = data.get("choices", [])
        if not choices:
            continue

        delta = choices[0].get("delta", {})

        thinking = delta.get("reasoning_content", "")
        if thinking:
            print(f"\033[2m{thinking}\033[0m", end="", flush=True)

        content = delta.get("content", "")
        if content:
            print(content, end="", flush=True)

        if choices[0].get("finish_reason") == "stop":
            break

    print()


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    message = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_MSG

    print("\n" + "─" * 52)
    print("  DeepSeek Chat via SCRAPPER")
    print("─" * 52)

    session, device_id = build_session()

    print("\n[*] Verifying user...")
    user = get_user(session)
    print(f"[*] Logged in as: {user.get('email', '?')}")

    print("[*] Creating chat session...")
    chat_id = create_chat_session(session)
    print(f"[*] Session ID: {chat_id}")
    session.headers["Referer"] = f"https://chat.deepseek.com/a/chat/s/{chat_id}"

    print(f"[*] Model: {DEFAULT_MODEL}")
    print(f"\n[You]      : {message}")
    print(f"[DeepSeek] : ", end="", flush=True)

    send_message(session, chat_id, message, DEFAULT_MODEL)

    print("\n" + "─" * 52 + "\n")
