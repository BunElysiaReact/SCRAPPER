#!/usr/bin/env python3
"""
gemini_chat.py — Gemini web chat client via reverse-engineered StreamGenerate API
Uses real browser session cookies.

Usage:
    python3 gemini_chat.py "your message"
    python3 gemini_chat.py  # interactive REPL

Dependencies:
    pip install curl_cffi
"""

import sys
import re
import json
import uuid
import time
from urllib.parse import urlencode

try:
    from curl_cffi import requests as cf_requests
except ImportError:
    print("Missing dependency: pip install curl_cffi")
    sys.exit(1)

# ─── CREDENTIALS ────────────────────────────────────────────────────────────
# PASTE YOUR COOKIES HERE. __Secure-1PSID is the most critical one.
# You can get these from DevTools → Application → Cookies → gemini.google.com

COOKIES = {
    "__Secure-1PSID": "g.a0007Agsc8qBaL8yWr5l8X9BnL3OTrLfUjunG3LFpM-cwk2vHGNpeI6vlRQ1kkf0T9uLBRSgnQACgYKAUcSARUSFQHGX2Mi4cRkf-sIgxBYT1r-SQrd_BoVAUF8yKpe_p7xrpLez6BcO3J1FB930076",
    "SIDCC": "AKEyXzWZIpP10HgWVQBEWdBGy9uD5JOaaBvuZZGglEd47bVwmc2uHEqdGmwvrufB4BOPPOxv",
    "__Secure-1PSIDCC": "AKEyXzUgpvqyGd4Dy9VETbUVpyXHlfvM0LV0JSHeB2GazyYdfFtzSH3X8RWlyAfVXBZcHisi",
    "__Secure-3PSIDCC": "AKEyXzXBsbpG3B8CJmuMtZ72wZoMlWhCHLlQhBye9ian4F_CIAymF8g7VV_RPTYRRqLPEjSAJg",
    "SID": "g.a0007Agsc8qBaL8yWr5l8X9BnL3OTrLfUjunG3LFpM-cwk2vHGNpM0V7Zq-XqqugbS36bzOhUwACgYKAc0SARUSFQHGX2MixzxSWhWWMv-ldyrqUTiXWRoVAUF8yKqYcLG8TqTt98_dPsiSCPKJ0076",
}

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Not:A-Brand";v="99", "Brave";v="145", "Chromium";v="145"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}

# ─── SESSION CLASS ───────────────────────────────────────────────────────────

class GeminiSession:
    def __init__(self):
        self.session = cf_requests.Session(impersonate="chrome120")
        self.session.headers.update(BASE_HEADERS)
        self.session.cookies.update(COOKIES)

        self.bl = None          # build label
        self.f_sid = None       # session id
        self.at_token = None    # anti-CSRF token
        
        self.conv_id = None
        self.response_id = None
        self.candidate_id = None
        
        self.reqid = 1000000
        self._initialized = False

    def _cookie_header(self):
        return "; ".join(f"{k}={v}" for k, v in self.session.cookies.items())

    def init_session(self):
        """Fetch gemini.google.com/app to get bl, f.sid, then fetch at token."""
        print("[*] Initializing Gemini session...", file=sys.stderr)

        # Step 1: Load app page
        resp = self.session.get(
            "https://gemini.google.com/app",
            headers={**BASE_HEADERS, "Cookie": self._cookie_header()},
        )
        
        # Debugging: Check if we got redirected to login
        if "accounts.google.com" in resp.url:
            raise RuntimeError("Failed: Redirected to Google Login. Your cookies are invalid or expired.")

        if resp.status_code != 200:
            raise RuntimeError(f"Failed to load app page: {resp.status_code}")

        html = resp.text

        # --- Extraction of 'bl' (Build Label) ---
        # Method 1: Look in the standard config object
        bl_match = re.search(r'"bl":"(boq_assistant-bard-web-server[^"]*)"', html)
        if bl_match:
            self.bl = bl_match.group(1)
        else:
            # Method 2: Search for the variable assignment (common in newer builds)
            # Example: bl:"boq_assistant-bard-web-server_..."
            bl_match2 = re.search(r'[, "]bl[":\s]+"(boq_assistant-bard-web-server[^"]*)"', html)
            if bl_match2:
                self.bl = bl_match2.group(1)
            else:
                # Method 3: Find any string that looks like the build label
                bl_match3 = re.search(r'(boq_assistant-bard-web-server_\d{8}\.\d+_p\d+)', html)
                if bl_match3:
                    self.bl = bl_match3.group(1)
                else:
                    # Last Resort: Dump the page source if we can't find it
                    print("\n[DEBUG] Could not find 'bl'. Saving page source to 'debug_page.html' for inspection.", file=sys.stderr)
                    with open("debug_page.html", "w", encoding="utf-8") as f:
                        f.write(html)
                    raise RuntimeError("Could not extract 'bl'. Saved source to 'debug_page.html'. Check if you are logged in.")

        # --- Extraction of 'f.sid' ---
        fsid_match = re.search(r'"FdrFJe":"(-?\d+)"', html)
        if not fsid_match:
            fsid_match = re.search(r'"w2bAxc","(-?\d+)"', html)
        
        if fsid_match:
            self.f_sid = fsid_match.group(1)
        else:
            # Fallback: Sometimes it's in the URL or a meta tag, but rarely. 
            # If we have bl but no f.sid, we might be able to proceed, but it's risky.
            print(f"[!] Warning: Could not extract f.sid from page.", file=sys.stderr)
            # We can try to proceed without it or raise error. Let's raise error.
            raise RuntimeError("Could not extract 'f.sid'.")

        print(f"[+] bl={self.bl[:50]}...", file=sys.stderr)
        print(f"[+] f.sid={self.f_sid}", file=sys.stderr)

        # Step 2: Fetch at token
        self._fetch_at_token()
        self._initialized = True

    def _fetch_at_token(self):
        """POST to batchexecute with rpcids=otAQ7b to get the 'at' token."""
        url = (
            f"https://gemini.google.com/_/BardChatUi/data/batchexecute"
            f"?rpcids=otAQ7b&source-path=%2Fapp&bl={self.bl}"
            f"&f.sid={self.f_sid}&hl=en&_reqid={self.reqid}&rt=c"
        )
        self.reqid += 100000

        payload = urlencode({
            "f.req": json.dumps([[[
                "otAQ7b",
                json.dumps([None, None, None, []]),
                None,
                "generic"
            ]]])
        })

        resp = self.session.post(
            url,
            data=payload,
            headers={
                **BASE_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Referer": "https://gemini.google.com/",
                "X-Same-Domain": "1",
                "Cookie": self._cookie_header(),
            }
        )

        text = resp.text
        
        # Parse the response
        if text.startswith(")]}'\n"):
            text = text[5:]
            
        try:
            data = json.loads(text)
            if data and data[0]:
                inner_data = data[0]
                # Standard location for 'at' token
                if len(inner_data) > 3 and inner_data[3]:
                    self.at_token = inner_data[3][0]
        except (json.JSONDecodeError, IndexError):
            pass

        # Fallback regex
        if not self.at_token:
            at_match = re.search(r'AJvLN6[A-Za-z0-9_\-]+:\d+', text)
            if at_match:
                self.at_token = at_match.group(0)
        
        if not self.at_token:
            print(f"[!] Response for 'at' token: {text[:200]}", file=sys.stderr)
            raise RuntimeError("Could not extract 'at' token. Session might be invalid.")
            
        print(f"[+] at_token={self.at_token[:20]}...", file=sys.stderr)

    def _build_freq(self, message: str) -> str:
        """Build the f.req payload for StreamGenerate."""
        req_id = str(uuid.uuid4())
        
        inner = json.dumps([
            [message, 0, None, None, None, None, 0],
            ["en"],
            [
                self.conv_id or None,
                self.response_id or None,
                self.candidate_id or None,
                None, None, None, None, None, None, ""
            ],
            self.at_token,
            "",
            None,
            [1],
            1,
            None,
            None,
            1,
            0,
            None,
            None,
            None,
            None,
            None,
            [[1]],
            0,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            1,
            None,
            None,
            [4]
        ])

        return json.dumps([None, inner])

    def send_message(self, message: str) -> str:
        """Send a message and return the full response text."""
        if not self._initialized:
            self.init_session()

        url = (
            f"https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
            f"?bl={self.bl}&f.sid={self.f_sid}&hl=en&_reqid={self.reqid}&rt=c"
        )
        self.reqid += 400000

        freq = self._build_freq(message)
        payload = urlencode({"f.req": freq, "at": self.at_token})

        extra_headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Referer": f"https://gemini.google.com/app/{self.conv_id or ''}",
            "X-Same-Domain": "1",
            "Cookie": self._cookie_header(),
            "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-bitness": '"64"',
            "sec-ch-ua-full-version-list": '"Not:A-Brand";v="99.0.0.0", "Brave";v="145.0.0.0", "Chromium";v="145.0.0.0"',
            "sec-ch-ua-model": '""',
            "sec-ch-ua-platform-version": '""',
            "sec-ch-ua-wow64": "?0",
            "x-goog-ext-525001261-jspb": f'[1,null,null,null,"{req_id}",null,null,0,[4],null,null,1]',
            "x-goog-ext-525005358-jspb": f'["{req_id}",1]',
            "x-goog-ext-73010989-jspb": "[0]",
            "x-goog-ext-73010990-jspb": "[0]",
        }

        resp = self.session.post(
            url,
            data=payload,
            headers={**BASE_HEADERS, **extra_headers},
            stream=True,
        )

        if resp.status_code != 200:
            err_text = resp.text
            raise RuntimeError(f"StreamGenerate failed: {resp.status_code}\n{err_text[:500]}")

        return self._parse_stream(resp)

    def _parse_stream(self, resp) -> str:
        """Parse the chunked StreamGenerate response."""
        full_text = ""
        raw = resp.text

        if raw.startswith(")]}'\n"):
            raw = raw[5:]

        try:
            data = json.loads(raw)
            
            if isinstance(data, list) and data:
                target = data[0] if isinstance(data[0], list) else data
                
                if target[0] == "wrb.fr":
                    if len(target) > 2 and target[2]:
                        inner_json_str = target[2]
                        inner_data = json.loads(inner_json_str)
                        
                        if inner_data and len(inner_data) > 1 and inner_data[1]:
                            ids = inner_data[1]
                            if ids[0]: self.conv_id = ids[0]
                            if len(ids) > 1 and ids[1]: self.response_id = ids[1]
                        
                        if inner_data and len(inner_data) > 4 and inner_data[4]:
                            candidates = inner_data[4]
                            for cand in candidates:
                                if not cand: continue
                                if cand[0]: self.candidate_id = cand[0]
                                if len(cand) > 1 and cand[1]:
                                    for chunk in cand[1]:
                                        if isinstance(chunk, str):
                                            full_text += chunk
                                        
        except json.JSONDecodeError:
            pass
                                        
        return full_text or "[!] No response text found."

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    session = GeminiSession()

    if len(sys.argv) > 1:
        message = " ".join(sys.argv[1:])
        print(f"\nYou: {message}")
        try:
            response = session.send_message(message)
            print(f"\nGemini: {response}")
        except Exception as e:
            print(f"\n[ERROR] {e}")
    else:
        print("Gemini Web Chat — type 'quit' to exit\n")
        while True:
            try:
                message = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nbye")
                break
            if not message:
                continue
            if message.lower() in ("quit", "exit", "q"):
                break
            try:
                response = session.send_message(message)
                print(f"\nGemini: {response}\n")
            except Exception as e:
                print(f"[ERROR] {e}\n")


if __name__ == "__main__":
    main()
