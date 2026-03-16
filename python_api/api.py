#!/usr/bin/env python3
"""
python_api/api.py - Scraper orchestrator API
All stdlib — no pip, no venv needed.
Run: python3 api.py
"""

import json
import random
import io
import csv
import os
import socket
import subprocess
import threading
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import urlparse, parse_qs

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

# ── Config ────────────────────────────────────────────────────────────────────

BASE         = Path("/home/PeaseErnest/scraper")
DATA_DIR     = BASE / "data"
LOGS_DIR     = BASE / "logs"
RUST_BIN     = BASE / "rust_finder" / "target" / "release" / "rust_finder"
C_SOCKET     = "/tmp/scraper.sock"
API_PORT     = 8080

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ── In-memory store ───────────────────────────────────────────────────────────

store = {
    "requests":      defaultdict(list),
    "responses":     defaultdict(list),
    "bodies":        defaultdict(list),
    "auth":          defaultdict(list),
    "cookies":       defaultdict(list),
    "websockets":    defaultdict(list),
    "ws_frames":     defaultdict(list),      # ← NEW: parsed frames
    "ws_connections":defaultdict(list),      # ← NEW: open/close/handshake
    "dommaps":       defaultdict(list),
    "storage":       defaultdict(list),
    "fingerprints":  defaultdict(list),
}
store_lock = threading.Lock()

live_feed      = []
live_feed_lock = threading.Lock()
MAX_LIVE       = 500


# ── URL Queue ─────────────────────────────────────────────────────────────────

url_queue      = []
queue_lock     = threading.Lock()
queue_running  = False
queue_thread   = None

def queue_worker():
    global queue_running
    while True:
        with queue_lock:
            if not url_queue:
                queue_running = False
                return
            item = url_queue.pop(0)

        url     = item.get("url")
        delay   = item.get("delay", 5)
        warmup  = item.get("warmup", True)

        print(f"[Queue] Processing: {url}")
        cmd = "nav" if warmup else "nav_nowarmup"
        send_to_c({"command": cmd, "args": url})

        # Random delay between requests (human-like)
        sleep_time = delay + (random.random() * delay * 0.5)
        print(f"[Queue] Waiting {sleep_time:.1f}s before next...")
        time.sleep(sleep_time)

def queue_add(urls, delay=6, warmup=True):
    global queue_running, queue_thread
    with queue_lock:
        for url in urls:
            url_queue.append({"url": url, "delay": delay, "warmup": warmup})
    if not queue_running:
        queue_running = True
        queue_thread  = threading.Thread(target=queue_worker, daemon=True)
        queue_thread.start()
    return len(url_queue)

def queue_status():
    with queue_lock:
        return {"pending": len(url_queue), "running": queue_running, "items": list(url_queue)}

def queue_clear():
    with queue_lock:
        url_queue.clear()
    return {"cleared": True}

# ── File → store key mapping ──────────────────────────────────────────────────

FILE_TO_KEY = {
    "requests.jsonl":       "requests",
    "responses.jsonl":      "responses",
    "bodies.jsonl":         "bodies",
    "auth.jsonl":           "auth",
    "cookies.jsonl":        "cookies",
    "websockets.jsonl":     "websockets",
    "ws_frames.jsonl":      "ws_frames",       # ← NEW
    "ws_connections.jsonl": "ws_connections",  # ← NEW
    "dommaps.jsonl":        "dommaps",
    "storage.jsonl":        "storage",
    "fingerprints.jsonl":   "fingerprints",
}

# ── Load existing data ────────────────────────────────────────────────────────

def load_existing():
    for fname, key in FILE_TO_KEY.items():
        path = DATA_DIR / fname
        if not path.exists():
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj    = json.loads(line)
                    domain = obj.get("domain", "unknown")
                    with store_lock:
                        store[key][domain].append(obj)
                except Exception:
                    pass
    print(f"[API] Loaded existing data from {DATA_DIR}")

# ── File watcher ──────────────────────────────────────────────────────────────

file_positions = {}

def watch_files():
    while True:
        for fname, key in FILE_TO_KEY.items():
            path = DATA_DIR / fname
            if not path.exists():
                continue
            pos = file_positions.get(fname, 0)
            try:
                with open(path) as f:
                    f.seek(pos)
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj    = json.loads(line)
                            domain = obj.get("domain", "unknown")
                            with store_lock:
                                store[key][domain].append(obj)
                            with live_feed_lock:
                                live_feed.append(obj)
                                if len(live_feed) > MAX_LIVE:
                                    live_feed.pop(0)
                        except Exception:
                            pass
                    file_positions[fname] = f.tell()
            except Exception:
                pass
        time.sleep(0.5)

# ── Send command to C host ────────────────────────────────────────────────────

def send_to_c(command_dict):
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(C_SOCKET)
        cmd  = command_dict.get("command", "")
        args = command_dict.get("args", "")
        line = f"{cmd} {args}\n" if args else f"{cmd}\n"
        sock.sendall(line.encode())
        sock.settimeout(3)
        try:    resp = sock.recv(4096).decode(errors="replace")
        except: resp = ""
        sock.close()
        return resp
    except Exception as e:
        return f"ERROR: {e}"

# ── Rust finder ───────────────────────────────────────────────────────────────

def rust_find(selector, domain=None, limit=100):
    if not RUST_BIN.exists():
        return {"error": "rust_finder not built. Run: cd rust_finder && cargo build --release"}
    html_files = list(DATA_DIR.glob("html_*.json"))
    if domain:
        filtered = []
        for hf in html_files:
            try:
                obj = json.loads(hf.read_text())
                if domain in obj.get("data", {}).get("url", ""):
                    filtered.append(str(hf))
            except Exception:
                pass
        html_files = filtered
    else:
        html_files = [str(f) for f in html_files]
    if not html_files:
        return {"error": "No HTML files. Run 'html' command first."}
    results = []
    for hf in html_files[:10]:
        try:
            proc = subprocess.run(
                [str(RUST_BIN), "--selector", selector, "--file", hf, "--limit", str(limit)],
                capture_output=True, text=True, timeout=10
            )
            if proc.stdout:
                results.append({"file": hf, "matches": json.loads(proc.stdout)})
        except Exception as e:
            results.append({"file": hf, "error": str(e)})
    return {"selector": selector, "results": results}


# ── On-demand scrape ──────────────────────────────────────────────────────────

def scrape_url(url, selector, limit=50):
    """Fetch URL directly and extract elements matching selector via rust_finder."""
    import urllib.request
    if not RUST_BIN.exists():
        return {"error": "rust_finder not built. Run: cd rust_finder && cargo build --release"}
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return {"error": f"Fetch failed: {e}"}

    # Write HTML to a temp file — passing it as a CLI arg fails on large pages (E2BIG)
    import tempfile
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as tf:
            tf.write(html)
            tmp = tf.name
        proc = subprocess.run(
            [str(RUST_BIN), "--selector", selector, "--file", tmp, "--limit", str(limit)],
            capture_output=True, text=True, timeout=15
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return {"error": proc.stderr or "No output from rust_finder"}
        matches = json.loads(proc.stdout)
        return {"url": url, "selector": selector, "count": len(matches), "matches": matches}
    except Exception as e:
        return {"error": f"rust_finder failed: {e}"}
    finally:
        if tmp:
            try: os.unlink(tmp)
            except: pass

# ── NEW: WebSocket helper functions ───────────────────────────────────────────

def get_ws_frames(domain=None, flags_filter=None, limit=200, skip_heartbeat=True):
    """Return WS frames, optionally filtered by domain, flags, excluding heartbeats."""
    with store_lock:
        if domain:
            frames = list(store["ws_frames"].get(domain, []))
        else:
            frames = [f for v in store["ws_frames"].values() for f in v]

    # Newest first
    frames.sort(key=lambda x: x.get("timestamp", 0), reverse=True)

    # Filter heartbeats (ping/pong)
    if skip_heartbeat:
        frames = [f for f in frames if "HEARTBEAT" not in (f.get("flags") or [])]

    # Filter by specific flags
    if flags_filter:
        if isinstance(flags_filter, str):
            flags_filter = [flags_filter]
        frames = [f for f in frames if any(fl in (f.get("flags") or []) for fl in flags_filter)]

    return frames[:limit]


def get_ws_connections(domain=None, open_only=False):
    """Return WebSocket connection lifecycle events."""
    with store_lock:
        if domain:
            events = list(store["ws_connections"].get(domain, []))
        else:
            events = [e for v in store["ws_connections"].values() for e in v]

    # Group by requestId — build connection summaries
    conns = {}
    for ev in events:
        rid  = ev.get("requestId") or ev.get("request_id", "unknown")
        t    = ev.get("type", "")
        if rid not in conns:
            conns[rid] = {
                "requestId": rid,
                "url":       ev.get("url", ""),
                "domain":    ev.get("domain", ""),
                "tabId":     ev.get("tabId"),
                "openedAt":  None,
                "closedAt":  None,
                "handshake": None,
                "open":      True,
                "summary":   None,
            }
        if t == "websocket_opened":
            conns[rid]["openedAt"] = ev.get("timestamp")
            conns[rid]["url"]      = ev.get("url", conns[rid]["url"])
        elif t == "websocket_handshake":
            conns[rid]["handshake"] = {
                "status":  ev.get("status"),
                "headers": ev.get("headers", {}),
            }
        elif t == "websocket_closed":
            conns[rid]["closedAt"] = ev.get("timestamp")
            conns[rid]["open"]     = False
            conns[rid]["summary"]  = ev.get("summary")

    result = list(conns.values())
    result.sort(key=lambda x: x.get("openedAt") or 0, reverse=True)

    if open_only:
        result = [c for c in result if c["open"]]

    return result


def get_ws_stats(domain=None):
    """Aggregate stats across all captured WS traffic."""
    frames = get_ws_frames(domain=domain, skip_heartbeat=False, limit=999999)
    conns  = get_ws_connections(domain=domain)

    total_recv = sum(1 for f in frames if f.get("direction") == "recv")
    total_sent = sum(1 for f in frames if f.get("direction") == "sent")

    # Flag frequency
    flag_counts = {}
    for f in frames:
        for fl in (f.get("flags") or []):
            flag_counts[fl] = flag_counts.get(fl, 0) + 1

    # Collect all extracted numeric values
    extracted_values = {}
    for f in frames:
        for k, v in (f.get("extracted") or {}).items():
            if k not in extracted_values:
                extracted_values[k] = []
            extracted_values[k].append(v)

    # Summarize numeric fields
    value_stats = {}
    for k, vals in extracted_values.items():
        if vals:
            value_stats[k] = {
                "count": len(vals),
                "min":   min(vals),
                "max":   max(vals),
                "avg":   round(sum(vals) / len(vals), 4),
                "last":  vals[-1],
            }

    # Unique URLs seen
    ws_urls = list({c["url"] for c in conns if c.get("url")})

    return {
        "connections": {
            "total":  len(conns),
            "open":   sum(1 for c in conns if c["open"]),
            "closed": sum(1 for c in conns if not c["open"]),
            "urls":   ws_urls,
        },
        "frames": {
            "total":  len(frames),
            "recv":   total_recv,
            "sent":   total_sent,
        },
        "flags":          flag_counts,
        "extractedValues": value_stats,
    }


def get_ws_interesting(domain=None, limit=100):
    """Return only frames that matched interesting patterns — no heartbeats, no empty frames."""
    INTERESTING = ['CRASH_POINT','MULTIPLIER','GAME_STATE','ROUND_ID','HASH',
                   'CASHOUT','BALANCE','BET','PLAYER_DATA','RESULT','PAYOUT',
                   'HAS_NUMBERS','BINARY_JSON']
    return get_ws_frames(domain=domain, flags_filter=INTERESTING, limit=limit)

# ── Data queries ──────────────────────────────────────────────────────────────

def get_bearer_tokens(domain=None):
    tokens = []
    with store_lock:
        domains = [domain] if domain else list(store["requests"].keys())
        for d in domains:
            for req in store["requests"][d]:
                headers = req.get("headers", {})
                auth = headers.get("authorization") or headers.get("Authorization", "")
                if auth.lower().startswith("bearer "):
                    tokens.append({
                        "domain":    d,
                        "token":     auth[7:],
                        "url":       req.get("url"),
                        "timestamp": req.get("timestamp")
                    })
    seen = set(); unique = []
    for t in tokens:
        if t["token"] not in seen:
            seen.add(t["token"]); unique.append(t)
    return unique

def get_auth_cookies(domain=None):
    with store_lock:
        domains = [domain] if domain else list(store["auth"].keys())
        return {d: store["auth"][d] for d in domains}

def get_api_endpoints(domain=None):
    with store_lock:
        domains = [domain] if domain else list(store["requests"].keys())
        endpoints = defaultdict(list)
        for d in domains:
            for req in store["requests"][d]:
                flags = req.get("flags", [])
                if "API" in flags or "AUTH_FLOW" in flags:
                    endpoints[d].append({
                        "method":    req.get("method"),
                        "url":       req.get("url"),
                        "flags":     flags,
                        "postData":  req.get("postData"),
                        "timestamp": req.get("timestamp")
                    })
        return dict(endpoints)

def get_domains():
    with store_lock:
        all_domains = set()
        for key in store:
            all_domains.update(store[key].keys())
        return sorted(all_domains)

def get_stats():
    with store_lock:
        stats = {}
        for key in store:
            total = sum(len(v) for v in store[key].values())
            stats[key] = {"total": total, "domains": list(store[key].keys())}
        return stats

# NEW: Site intel — all tokens, cookies, endpoints, DOM for one domain
def get_site_intel(domain):
    tokens    = get_bearer_tokens(domain)
    auth      = get_auth_cookies(domain)
    endpoints = get_api_endpoints(domain)
    with store_lock:
        dommap = store["dommaps"].get(domain, [])
        # Latest DOM map only
        latest_dom = dommap[-1] if dommap else None
    return {
        "domain":    domain,
        "tokens":    tokens,
        "auth":      auth.get(domain, []),
        "endpoints": endpoints.get(domain, []),
        "dommap":    latest_dom,
    }

# NEW: Export all data as zip
def export_zip(domain=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if domain:
            # Export single domain
            with store_lock:
                for key in store:
                    items = store[key].get(domain, [])
                    if items:
                        zf.writestr(f"{domain}/{key}.json",
                                    json.dumps(items, indent=2))
            # HTML files for this domain
            for hf in DATA_DIR.glob("html_*.json"):
                try:
                    obj = json.loads(hf.read_text())
                    if domain in obj.get("data", {}).get("url", ""):
                        zf.write(str(hf), f"{domain}/{hf.name}")
                except Exception:
                    pass
        else:
            # Export everything
            for fname in DATA_DIR.glob("*.jsonl"):
                zf.write(str(fname), fname.name)
            # Include WS-specific files
            for fname in DATA_DIR.glob("ws_*.jsonl"):
                zf.write(str(fname), fname.name)
            for fname in DATA_DIR.glob("html_*.json"):
                zf.write(str(fname), fname.name)
            for fname in DATA_DIR.glob("screenshot_*.json"):
                zf.write(str(fname), fname.name)
    buf.seek(0)
    return buf.read()


# ── /api/v1/ helpers ──────────────────────────────────────────────────────────

def get_fingerprint(domain=None):
    with store_lock:
        if domain:
            fps = store["fingerprints"].get(domain, [])
            return fps[-1].get("fingerprint", {}) if fps else {}
        return {d: v[-1].get("fingerprint", {}) for d, v in store["fingerprints"].items() if v}

def get_localstorage(domain=None):
    result = {}
    with store_lock:
        domains = [domain] if domain else list(store["storage"].keys())
        for d in domains:
            ls, ss = {}, {}
            for evt in store["storage"][d]:
                data = evt.get("data", {})
                if isinstance(data, dict):
                    ls.update(data.get("localStorage", {}))
                    ss.update(data.get("sessionStorage", {}))
            if ls or ss:
                result[d] = {"localStorage": ls, "sessionStorage": ss}
    return result.get(domain, {}) if domain else result

def get_session_all(domain=None):
    fp      = get_fingerprint(domain)
    ls_data = get_localstorage(domain)
    tokens  = get_bearer_tokens(domain)
    auth    = get_auth_cookies(domain)
    with store_lock:
        if domain:
            raw = store["cookies"].get(domain, [])
            raw_auth = store["auth"].get(domain, [])
        else:
            raw = [evt for d_list in store["cookies"].values() for evt in d_list]
            raw_auth = [evt for d_list in store["auth"].values() for evt in d_list]
        seen_sc = set()
        flat_cookies = []
        for evt in (raw + raw_auth):
            c = evt.get("cookie")
            if c and not evt.get("removed", False):
                k = (c.get("name"), c.get("domain"))
                if k not in seen_sc:
                    seen_sc.add(k)
                    flat_cookies.append(c)
    ls = ls_data.get("localStorage", {}) if isinstance(ls_data, dict) else {}
    ss = ls_data.get("sessionStorage", {}) if isinstance(ls_data, dict) else {}
    return {
        "fingerprint":    fp,
        "cookies":        flat_cookies,
        "localStorage":   ls,
        "sessionStorage": ss,
        "tokens":         tokens,
        "auth":           auth,
    }

def export_env(domain=None):
    lines    = ["#!/usr/bin/env bash", "# SCRAPY Session Export",
                f"# Generated: {datetime.now(timezone.utc).isoformat()}", ""]
    tokens   = get_bearer_tokens(domain)
    with store_lock:
        doms = [domain] if domain else list(store["cookies"].keys())
        seen_env = set()
        all_cookies = []
        for d in doms:
            for evt in (store["cookies"].get(d, []) + store["auth"].get(d, [])):
                c = evt.get("cookie")
                if c and not evt.get("removed", False):
                    k = (c.get("name"), c.get("domain"))
                    if k not in seen_env:
                        seen_env.add(k)
                        all_cookies.append(c)
    if tokens:
        lines.append(f'export SCRAPY_BEARER_TOKEN="{tokens[0]["token"]}"')
    if all_cookies:
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in all_cookies[:30])
        lines.append(f'export SCRAPY_COOKIES="{cookie_str}"')
    csrf = next((c["value"] for c in all_cookies if "csrf" in c.get("name","").lower()), None)
    if csrf:
        lines.append(f'export SCRAPY_CSRF_TOKEN="{csrf}"')
    cf = next((c["value"] for c in all_cookies if c.get("name") == "cf_clearance"), None)
    if cf:
        lines.append(f'export SCRAPY_CF_CLEARANCE="{cf}"')
    fp = get_fingerprint(domain)
    if isinstance(fp, dict) and fp.get("userAgent"):
        lines.append(f'export SCRAPY_USER_AGENT="{fp["userAgent"]}"')
    return "\n".join(lines)

def export_full_json(domain=None):
    with store_lock:
        all_keys  = set().union(*[store[k].keys() for k in store])
        doms      = [domain] if domain else list(all_keys)
        def collect(key):
            if domain:
                return list(store[key].get(domain, []))
            return [item for d_list in store[key].values() for item in d_list]
        all_requests  = collect("requests")
        all_responses = collect("responses")
        all_ws        = collect("websockets")
        all_cookies   = collect("cookies")
        all_dommaps   = collect("dommaps")
        all_fps       = collect("fingerprints")
        all_storage   = collect("storage")
    flat_cookies = []
    seen_ck = set()
    all_auth_evts = collect("auth")
    for evt in (all_cookies + all_auth_evts):
        c = evt.get("cookie")
        if c and not evt.get("removed", False):
            k = (c.get("name"), c.get("domain"))
            if k not in seen_ck:
                seen_ck.add(k)
                flat_cookies.append(c)
    ls_m, ss_m   = {}, {}
    for evt in all_storage:
        data = evt.get("data", {})
        if isinstance(data, dict):
            ls_m.update(data.get("localStorage", {}))
            ss_m.update(data.get("sessionStorage", {}))
    latest_fp = all_fps[-1].get("fingerprint", {}) if all_fps else {}
    tokens    = get_bearer_tokens(domain)
    return {
        "metadata": {
            "capture_time":   datetime.now(timezone.utc).isoformat(),
            "scrapy_version": "2.1.0",
            "total_requests": len(all_requests),
            "total_cookies":  len(flat_cookies),
            "domains":        sorted(set(r.get("domain","") for r in all_requests)),
        },
        "session": {
            "cookies":        flat_cookies,
            "localStorage":   ls_m,
            "sessionStorage": ss_m,
            "tokens":         {t.get("url","unknown"): t.get("token") for t in tokens},
            "fingerprint":    latest_fp,
        },
        "network": {
            "requests":   all_requests,
            "responses":  all_responses,
            "websockets": all_ws,
        },
        "dom": {"snapshots": all_dommaps},
    }

def export_jsonl(domain=None):
    lines = []
    with store_lock:
        for key in store:
            doms = [domain] if domain else list(store[key].keys())
            for d in doms:
                for item in store[key][d]:
                    lines.append(json.dumps(item))
    return "\n".join(lines)

def export_txt(domain=None):
    tokens    = get_bearer_tokens(domain)
    endpoints = get_api_endpoints(domain)
    fp        = get_fingerprint(domain)
    with store_lock:
        doms = [domain] if domain else list(set().union(*[store[k].keys() for k in store]))
        total_reqs = sum(len(store["requests"].get(d,[])) for d in doms)
        total_ws   = sum(len(store["websockets"].get(d,[])) for d in doms)
        seen_txt = set()
        all_cookies = []
        for d in doms:
            for evt in (store["cookies"].get(d, []) + store["auth"].get(d, [])):
                c = evt.get("cookie")
                if c and not evt.get("removed", False):
                    k = (c.get("name"), c.get("domain"))
                    if k not in seen_txt:
                        seen_txt.add(k)
                        all_cookies.append(c)
    lines = [
        "="*60, "  SCRAPY — Session Export (Text Format)",
        f"  Generated : {datetime.now(timezone.utc).isoformat()}",
        f"  Domains   : {', '.join(doms)}", "="*60, "",
        "[STATS]",
        f"  Requests captured : {total_reqs}",
        f"  Bearer tokens     : {len(tokens)}",
        f"  Cookies           : {len(all_cookies)}",
        f"  WebSocket frames  : {total_ws}", "",
    ]
    if isinstance(fp, dict) and fp.get("userAgent"):
        scr = fp.get("screen", {})
        lines += ["[FINGERPRINT]",
            f"  User-Agent : {fp.get('userAgent','')}",
            f"  Platform   : {fp.get('platform','')}",
            f"  Timezone   : {fp.get('timezone','')}",
            f"  Language   : {fp.get('language','')}",
            f"  Screen     : {scr.get('width')}x{scr.get('height')}", ""]
    if tokens:
        lines.append("[BEARER TOKENS]")
        for t in tokens:
            lines.append(f"  {t.get('domain','')} -> Bearer {t.get('token','')[:80]}...")
        lines.append("")
    if all_cookies:
        lines.append("[COOKIES]")
        for c in all_cookies[:50]:
            lines.append(f"  {c.get('name','')}={str(c.get('value',''))[:80]}")
        lines.append("")
    ep_list = [e for v in endpoints.values() for e in v]
    if ep_list:
        lines.append("[API ENDPOINTS]")
        for e in ep_list[:100]:
            lines.append(f"  {e.get('method','?')} {e.get('url','')}")
        lines.append("")
    return "\n".join(lines)

def export_har(domain=None):
    with store_lock:
        doms   = [domain] if domain else list(store["requests"].keys())
        entries = []
        for d in doms:
            reqs   = store["requests"].get(d, [])
            resps  = store["responses"].get(d, [])
            bodies = store["bodies"].get(d, [])
            body_map = {b.get("requestId"): b for b in bodies if b.get("requestId")}
            resp_map = {r.get("requestId"): r for r in resps  if r.get("requestId")}
            for req in reqs:
                rid  = req.get("requestId")
                resp = resp_map.get(rid, {})
                body = body_map.get(rid, {})
                ts   = req.get("timestamp", 0) or 0
                started = datetime.utcfromtimestamp(ts/1000).strftime('%Y-%m-%dT%H:%M:%S.000Z')
                rq_hdrs = [{"name":k,"value":str(v)} for k,v in (req.get("headers") or {}).items()]
                rs_hdrs = [{"name":k,"value":str(v)} for k,v in (resp.get("headers") or {}).items()]
                body_text = (body.get("body","") or "") if not body.get("base64") else ""
                entry = {
                    "startedDateTime": started, "time": 0,
                    "request": {
                        "method": req.get("method","GET"), "url": req.get("url",""),
                        "httpVersion":"HTTP/1.1","cookies":[],"headers":rq_hdrs,
                        "queryString":[],"headersSize":-1,"bodySize":len(req.get("postData") or "") or -1,
                    },
                    "response": {
                        "status": resp.get("status",0),"statusText":resp.get("statusText",""),
                        "httpVersion":"HTTP/1.1","cookies":[],"headers":rs_hdrs,
                        "content":{"size":-1,"mimeType":resp.get("mimeType","text/plain"),"text":body_text},
                        "redirectURL":"","headersSize":-1,"bodySize":len(body_text) or -1,
                    },
                    "cache":{},"timings":{"send":0,"wait":0,"receive":0},
                    "_scrapy":{"domain":d,"flags":req.get("flags",[])},
                }
                if req.get("postData"):
                    ct = (req.get("headers") or {}).get("content-type","text/plain")
                    entry["request"]["postData"] = {"mimeType":ct,"text":str(req["postData"])}
                entries.append(entry)
    return {"log":{"version":"1.2","creator":{"name":"SCRAPY","version":"2.1.0"},"pages":[],"entries":entries}}

def export_csv_data(domain=None):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp_ms","datetime","domain","method","url","status","mime_type","flags","has_bearer","request_id"])
    with store_lock:
        doms     = [domain] if domain else list(store["requests"].keys())
        resp_map = {}
        for d in doms:
            for r in store["responses"].get(d,[]):
                resp_map[r.get("requestId")] = r
        for d in doms:
            for req in store["requests"].get(d,[]):
                ts    = req.get("timestamp", 0) or 0
                flags = req.get("flags", [])
                resp  = resp_map.get(req.get("requestId"), {})
                writer.writerow([
                    ts,
                    datetime.utcfromtimestamp(ts/1000).isoformat() if ts else "",
                    d,
                    req.get("method",""),
                    req.get("url",""),
                    resp.get("status",""),
                    resp.get("mimeType",""),
                    "|".join(flags),
                    "BEARER_TOKEN" in flags,
                    req.get("requestId",""),
                ])
    return output.getvalue()

# ── HTTP handler ──────────────────────────────────────────────────────────────

class ScraperAPI(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    # ── Static file server ────────────────────────────────────────────────────
    MIME_TYPES = {
        ".html":  "text/html; charset=utf-8",
        ".js":    "application/javascript",
        ".mjs":   "application/javascript",
        ".jsx":   "application/javascript",
        ".css":   "text/css",
        ".json":  "application/json",
        ".png":   "image/png",
        ".jpg":   "image/jpeg",
        ".jpeg":  "image/jpeg",
        ".svg":   "image/svg+xml",
        ".ico":   "image/x-icon",
        ".woff":  "font/woff",
        ".woff2": "font/woff2",
        ".ttf":   "font/ttf",
        ".map":   "application/json",
        ".txt":   "text/plain",
    }

    def _serve_static(self, file_path):
        try:
            data     = file_path.read_bytes()
            ext      = file_path.suffix.lower()
            mime     = self.MIME_TYPES.get(ext, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type",   mime)
            self.send_header("Content-Length", len(data))
            self.send_header("Cache-Control",  "no-cache" if ext == ".html" else "public, max-age=3600")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        if not html:
            html = "<h1>SCRAPY — dashboard.html not found. Run the installer.</h1>"
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_sse_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        last_idx = max(0, len(live_feed) - 20)
        try:
            while True:
                with live_feed_lock:
                    current  = live_feed[last_idx:]
                    last_idx = len(live_feed)
                for item in current:
                    data = f"data: {json.dumps(item)}\n\n"
                    self.wfile.write(data.encode())
                    self.wfile.flush()
                time.sleep(0.5)
        except Exception:
            pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        qs     = parse_qs(parsed.query)
        domain = qs.get("domain", [None])[0]

        # ── Static file serving from dist/ (built BertUI dashboard) ──────────
        # Tries: BASE/ui/scrapperui/dist, ~/.scrapy/ui/scrapperui/dist
        dist_dirs = [
            BASE / "ui" / "scrapperui" / "dist",
            Path.home() / ".scrapy" / "ui" / "scrapperui" / "dist",
        ]
        dist_root = next((d for d in dist_dirs if d.exists()), None)

        if path in ("", "/") or (dist_root and path.startswith("/assets")):
            if dist_root:
                # Serve built React app
                if path in ("", "/"):
                    file_path = dist_root / "index.html"
                else:
                    # Strip leading slash
                    file_path = dist_root / path.lstrip("/")

                if file_path.exists() and file_path.is_file():
                    self._serve_static(file_path)
                else:
                    # SPA fallback — always return index.html for unknown paths
                    idx = dist_root / "index.html"
                    if idx.exists():
                        self._serve_static(idx)
                    else:
                        self.send_html(DASHBOARD_HTML)
                return
            else:
                # No build found — serve embedded fallback HTML
                self.send_html(DASHBOARD_HTML)
                return

        elif path == "/live":
            self.send_sse_stream()
        elif path == "/stats":
            self.send_json(get_stats())
        elif path == "/domains":
            self.send_json(get_domains())
        elif path == "/tokens":
            self.send_json(get_bearer_tokens(domain))
        elif path == "/auth":
            self.send_json(get_auth_cookies(domain))
        elif path == "/endpoints":
            self.send_json(get_api_endpoints(domain))
        elif path == "/requests":
            with store_lock:
                self.send_json(store["requests"][domain] if domain else dict(store["requests"]))
        elif path == "/bodies":
            limit = int(qs.get("limit", [50])[0])
            with store_lock:
                if domain:
                    self.send_json(store["bodies"][domain][-limit:])
                else:
                    self.send_json({d: v[-limit:] for d, v in store["bodies"].items()})
        elif path == "/cookies":
            with store_lock:
                self.send_json(store["cookies"][domain] if domain else dict(store["cookies"]))
        elif path == "/dommaps":
            with store_lock:
                self.send_json(store["dommaps"][domain] if domain else dict(store["dommaps"]))
        elif path == "/intel":
            if not domain:
                self.send_json({"error": "?domain= required"}, 400)
            else:
                self.send_json(get_site_intel(domain))
        elif path == "/find":
            selector = qs.get("selector", ["div"])[0]
            limit    = int(qs.get("limit", [100])[0])
            self.send_json(rust_find(selector, domain, limit))
        elif path == "/responses":
            with store_lock:
                if domain:
                    reqs  = store["requests"].get(domain, [])
                    resps = store["responses"].get(domain, [])
                    bods  = store["bodies"].get(domain, [])
                else:
                    reqs  = [r for v in store["requests"].values()  for r in v]
                    resps = [r for v in store["responses"].values() for r in v]
                    bods  = [r for v in store["bodies"].values()    for r in v]
            # merge responses with their bodies by requestId
            body_map = {b.get("requestId"): b.get("body") for b in bods if b.get("requestId")}
            merged = []
            for r in resps:
                entry = dict(r)
                entry["body"] = body_map.get(r.get("requestId"))
                merged.append(entry)
            self.send_json(merged)
        elif path == "/scrape":
            url      = qs.get("url", [""])[0]
            selector = qs.get("selector", ["div"])[0]
            limit    = int(qs.get("limit", [50])[0])
            if not url:
                self.send_json({"error": "?url= required"}, 400)
            else:
                self.send_json(scrape_url(url, selector, limit))
        elif path == "/feed":
            with live_feed_lock:
                limit = int(qs.get("limit", [100])[0])
                self.send_json(live_feed[-limit:])
        elif path == "/queue":
            self.send_json(queue_status())

        # ── NEW: WebSocket endpoints ──────────────────────────────────────────
        elif path == "/websockets":
            limit  = int(qs.get("limit", [200])[0])
            hb     = qs.get("heartbeat", ["0"])[0] == "1"
            self.send_json(get_ws_frames(domain=domain, limit=limit, skip_heartbeat=not hb))

        elif path == "/ws/frames":
            limit  = int(qs.get("limit", [200])[0])
            flags  = qs.get("flags", [None])[0]
            hb     = qs.get("heartbeat", ["0"])[0] == "1"
            self.send_json(get_ws_frames(
                domain=domain,
                flags_filter=flags.split(",") if flags else None,
                limit=limit,
                skip_heartbeat=not hb,
            ))

        elif path == "/ws/connections":
            open_only = qs.get("open", ["0"])[0] == "1"
            self.send_json(get_ws_connections(domain=domain, open_only=open_only))

        elif path == "/ws/stats":
            self.send_json(get_ws_stats(domain=domain))

        elif path == "/ws/interesting":
            limit = int(qs.get("limit", [100])[0])
            self.send_json(get_ws_interesting(domain=domain, limit=limit))

        elif path == "/export":
            data = export_zip(domain)
            fname = f"{domain or 'all_data'}.zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f"attachment; filename={fname}")
            self.send_header("Content-Length", len(data))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        # ── /api/v1/ — README-spec endpoints ─────────────────────────────────
        elif path == "/api/v1/session/cookies":
            with store_lock:
                if domain:
                    raw = store["cookies"].get(domain, [])
                    raw_auth = store["auth"].get(domain, [])
                else:
                    raw = [e for d_list in store["cookies"].values() for e in d_list]
                    raw_auth = [e for d_list in store["auth"].values() for e in d_list]
                seen_sc2 = set()
                flat = []
                for evt in (raw + raw_auth):
                    c = evt.get("cookie")
                    if c and not evt.get("removed", False):
                        k = (c.get("name"), c.get("domain"))
                        if k not in seen_sc2:
                            seen_sc2.add(k)
                            flat.append(c)
            self.send_json(flat)

        elif path == "/api/v1/session/localstorage":
            self.send_json(get_localstorage(domain))

        elif path == "/api/v1/session/all":
            self.send_json(get_session_all(domain))

        elif path == "/api/v1/fingerprint":
            self.send_json(get_fingerprint(domain))

        elif path == "/api/v1/tokens/all":
            self.send_json(get_bearer_tokens(domain))

        elif path == "/api/v1/requests/recent":
            limit = int(qs.get("limit", [50])[0])
            with store_lock:
                if domain:
                    self.send_json(store["requests"][domain][-limit:])
                else:
                    all_reqs = [r for v in store["requests"].values() for r in v]
                    all_reqs.sort(key=lambda x: x.get("timestamp", 0))
                    self.send_json(all_reqs[-limit:])

        elif path == "/api/v1/dom/snapshot":
            url_param = qs.get("url", [None])[0]
            with store_lock:
                if domain:
                    maps = list(store["dommaps"].get(domain, []))
                else:
                    maps = [m for v in store["dommaps"].values() for m in v]
                if url_param:
                    maps = [m for m in maps if url_param in (m.get("url") or "")]
            self.send_json(maps[-1] if maps else {})

        elif path == "/api/v1/export/env":
            body = export_env(domain).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.send_header("Content-Disposition", "attachment; filename=scrapy.env")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/v1/export/json":
            self.send_json(export_full_json(domain))

        elif path == "/api/v1/bulk/all":
            fmt = qs.get("format", ["json"])[0].lower()
            if fmt == "json":
                self.send_json(export_full_json(domain))
            elif fmt == "jsonl":
                body = export_jsonl(domain).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Disposition", "attachment; filename=scrapy-session.jsonl")
                self.send_header("Content-Length", len(body))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            elif fmt == "har":
                har_data = json.dumps(export_har(domain), indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition", "attachment; filename=scrapy-session.har")
                self.send_header("Content-Length", len(har_data))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(har_data)
            elif fmt == "csv":
                body = export_csv_data(domain).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", "attachment; filename=scrapy-session.csv")
                self.send_header("Content-Length", len(body))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            elif fmt == "txt":
                body = export_txt(domain).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=scrapy-session.txt")
                self.send_header("Content-Length", len(body))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json({"error": f"Unknown format '{fmt}'. Use: json|jsonl|har|csv|txt"}, 400)

        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length > 0 else {}

        if path == "/queue/add":
            urls   = body.get("urls", [])
            delay  = body.get("delay", 6)
            warmup = body.get("warmup", True)
            if not urls and body.get("url"):
                urls = [body["url"]]
            if not urls:
                self.send_json({"error": "no urls"}, 400); return
            count = queue_add(urls, delay, warmup)
            self.send_json({"queued": len(urls), "total_pending": count})

        elif path == "/queue/clear":
            self.send_json(queue_clear())

        elif path == "/cmd":
            command = body.get("command")
            args    = body.get("args", "")
            if not command:
                self.send_json({"error": "no command"}, 400); return
            resp = send_to_c({"command": command, "args": args})
            self.send_json({"sent": command, "response": resp})
        elif path == "/navigate":
            url = body.get("url")
            if not url:
                self.send_json({"error": "no url"}, 400); return
            resp = send_to_c({"command": "nav", "args": url})
            self.send_json({"navigating": url, "response": resp})
        elif path == "/clear":
            domain = body.get("domain")
            if domain:
                with store_lock:
                    for key in store: store[key].pop(domain, None)
                self.send_json({"cleared": domain})
            else:
                self.send_json({"error": "no domain"}, 400)
        else:
            self.send_json({"error": "Not found"}, 404)

# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = None  # Set at startup from file

def _load_dashboard():
    """Load dashboard HTML — tries: dist/index.html, dashboard.html beside api.py, embedded fallback."""
    global DASHBOARD_HTML
    import importlib.resources

    # 1. Check for built dist/index.html
    for dist in [
        BASE / "ui" / "scrapperui" / "dist" / "index.html",
        Path.home() / ".scrapy" / "ui" / "scrapperui" / "dist" / "index.html",
    ]:
        if dist.exists():
            DASHBOARD_HTML = dist.read_text(encoding="utf-8")
            print(f"[API] Dashboard: loaded from {dist}")
            return

    # 2. Check for dashboard.html beside api.py
    local = Path(__file__).parent / "dashboard.html"
    if local.exists():
        DASHBOARD_HTML = local.read_text(encoding="utf-8")
        print(f"[API] Dashboard: loaded from {local}")
        return

    # 3. Check ~/.scrapy/dashboard.html
    scrapy_dash = Path.home() / ".scrapy" / "dashboard.html"
    if scrapy_dash.exists():
        DASHBOARD_HTML = scrapy_dash.read_text(encoding="utf-8")
        print(f"[API] Dashboard: loaded from {scrapy_dash}")
        return

    # 4. Nothing found — show install instructions page
    DASHBOARD_HTML = _INSTALL_PAGE
    print("[API] Dashboard: showing install instructions (no dashboard.html found)")

_INSTALL_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>SCRAPY — Setup</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'JetBrains Mono',monospace;background:#07070c;color:#d0d0e8;
     display:flex;align-items:center;justify-content:center;height:100vh}
.box{max-width:520px;padding:32px;border:1px solid #1c1c2e;border-radius:6px;background:#0c0c14}
h1{font-size:18px;color:#00ff88;letter-spacing:3px;margin-bottom:6px}
.sub{font-size:10px;color:#505070;letter-spacing:1px;margin-bottom:24px}
.step{margin-bottom:18px}
.step-num{font-size:9px;color:#505070;text-transform:uppercase;letter-spacing:2px;margin-bottom:6px}
.cmd{background:#07070c;border:1px solid #252538;border-radius:3px;padding:10px 14px;
     font-size:12px;color:#00ff88;cursor:pointer;word-break:break-all;position:relative}
.cmd:hover{border-color:#00ff8866}
.copy-hint{position:absolute;right:8px;top:50%;transform:translateY(-50%);font-size:8px;color:#505070}
.note{font-size:10px;color:#606080;margin-top:8px;line-height:1.8}
a{color:#00ff8888;text-decoration:none}a:hover{color:#00ff88}
</style>
</head>
<body>
<div class="box">
  <h1>◈ SCRAPY</h1>
  <div class="sub">dashboard.html not found — run the installer</div>

  <div class="step">
    <div class="step-num">Linux / macOS</div>
    <div class="cmd" onclick="copyCmd(this,'curl -fsSL https://raw.githubusercontent.com/BunElysiaReact/SCRAPY/main/install.sh | bash')">
      curl -fsSL https://raw.githubusercontent.com/BunElysiaReact/SCRAPY/main/install.sh | bash
      <span class="copy-hint">click to copy</span>
    </div>
  </div>

  <div class="step">
    <div class="step-num">Windows (PowerShell)</div>
    <div class="cmd" onclick="copyCmd(this,'irm https://raw.githubusercontent.com/BunElysiaReact/SCRAPY/main/install.ps1 | iex')">
      irm https://raw.githubusercontent.com/BunElysiaReact/SCRAPY/main/install.ps1 | iex
      <span class="copy-hint">click to copy</span>
    </div>
  </div>

  <div class="note">
    After installing, the API server will automatically find and serve<br>
    <code style="color:#00ff88">dashboard.html</code> from the install directory.<br><br>
    <a href="https://github.com/BunElysiaReact/SCRAPY" target="_blank">
      github.com/BunElysiaReact/SCRAPY →
    </a>
  </div>
</div>
<script>
function copyCmd(el, cmd) {
  navigator.clipboard.writeText(cmd).catch(()=>{});
  var hint = el.querySelector('.copy-hint');
  hint.textContent = '✓ copied';
  hint.style.color = '#00ff88';
  setTimeout(()=>{ hint.textContent='click to copy'; hint.style.color='#505070'; }, 1500);
}
</script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _load_dashboard()
    print("[API] Loading existing data...")
    load_existing()
    threading.Thread(target=watch_files, daemon=True).start()
    print("[API] File watcher started")
    server = ThreadedHTTPServer(("0.0.0.0", API_PORT), ScraperAPI)
    print(f"[API] Dashboard → http://localhost:{API_PORT}")
    print(f"[API] Endpoints: /tokens /auth /endpoints /intel /dommaps /find /export /live")
    print(f"[API] WebSocket endpoints: /websockets /ws/frames /ws/connections /ws/stats /ws/interesting")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[API] Stopped")