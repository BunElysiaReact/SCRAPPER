// background.js - SESSION ISOLATED SCRAPER v3.3 (WEBSOCKET DEEP CAPTURE)
const BROWSER = 'brave';
let nativePort = null;
let reconnectAttempts = 0;
let keepAliveInterval = null;

const debuggedTabs    = new Set();
const pendingRequests = new Map();
const siteContexts    = new Map();
const tabContexts     = new Map();

// ── WebSocket connection registry ─────────────────────────────────────────────
// Full lifecycle: created → handshake → frames → closed
const wsConnections = new Map(); // requestId → WSConnection

function makeWSConnection(requestId, url, tabId, domain) {
  return {
    requestId,
    url,
    tabId,
    domain,
    createdAt:    Date.now(),
    closedAt:     null,
    frameCount:   { sent: 0, recv: 0 },
    byteCount:    { sent: 0, recv: 0 },
    frames:       [],           // last N frames kept in memory
    MAX_FRAMES:   200,
    patterns:     new Set(),    // interesting patterns found
  };
}

function wsAddFrame(conn, direction, payload, opcode) {
  const frame = {
    direction,
    payload,
    opcode,         // 1=text, 2=binary, 8=close, 9=ping, 10=pong
    timestamp: Date.now(),
    size:      typeof payload === 'string' ? payload.length : 0,
    parsed:    null,
    flags:     [],
  };

  // ── Try to parse payload ──────────────────────────────────────────────────
  if (opcode === 1 || opcode === undefined) {
    // TEXT frame — try JSON
    try {
      frame.parsed = JSON.parse(payload);
    } catch {
      // Not JSON — try to find JSON embedded in the string
      const m = payload.match(/\{[\s\S]*\}|\[[\s\S]*\]/);
      if (m) {
        try { frame.parsed = JSON.parse(m[0]); } catch {}
      }
    }
  } else if (opcode === 2) {
    // BINARY frame — try to detect encoding
    frame.flags.push('BINARY');
    // payloadData from CDP is base64 for binary
    try {
      const decoded = atob ? atob(payload) : Buffer.from(payload, 'base64').toString('binary');
      // Try JSON from decoded binary
      try {
        frame.parsed = JSON.parse(decoded);
        frame.flags.push('BINARY_JSON');
      } catch {
        // Check for MessagePack magic bytes or protobuf
        const bytes = decoded.split('').map(c => c.charCodeAt(0));
        if (bytes[0] === 0x82 || bytes[0] === 0x83 || bytes[0] === 0x84) frame.flags.push('MSGPACK');
        else frame.flags.push('PROTOBUF_OR_CUSTOM');
      }
    } catch {}
  }

  // ── Pattern detection (works on both parsed and raw) ─────────────────────
  const searchTarget = frame.parsed
    ? JSON.stringify(frame.parsed)
    : (typeof payload === 'string' ? payload : '');

  const PATTERNS = {
    CRASH_POINT:   /crash[\s_-]?point|crashPoint|bust|busted/i,
    MULTIPLIER:    /multiplier|multi|\"x\":\s*[\d.]+|\"m\":\s*[\d.]+/i,
    GAME_STATE:    /gameState|game_state|status.*running|status.*crashed/i,
    ROUND_ID:      /round[\s_-]?id|roundId|game[\s_-]?id|gameId/i,
    HASH:          /hash|seed|serverSeed/i,
    CASHOUT:       /cashout|cash_out|escape/i,
    BALANCE:       /balance|wallet|credit/i,
    BET:           /bet|wager|stake|amount/i,
    PLAYER_DATA:   /player|user[\s_-]?id|userId|seat/i,
    RESULT:        /result|outcome|winner/i,
    PAYOUT:        /payout|win|profit/i,
    HEARTBEAT:     /ping|pong|heartbeat|keepalive/i,
  };

  for (const [name, rx] of Object.entries(PATTERNS)) {
    if (rx.test(searchTarget)) {
      frame.flags.push(name);
      conn.patterns.add(name);
    }
  }

  // Extract numeric multiplier values if present
  if (frame.parsed) {
    const multiplierKeys = ['multiplier', 'x', 'm', 'rate', 'odds', 'crashPoint', 'bustAt', 'value'];
    frame.extractedValues = {};
    const scan = (obj, depth = 0) => {
      if (depth > 5 || !obj || typeof obj !== 'object') return;
      for (const [k, v] of Object.entries(obj)) {
        if (multiplierKeys.some(mk => k.toLowerCase().includes(mk))) {
          const n = parseFloat(v);
          if (!isNaN(n) && n >= 1.0 && n <= 10000) {
            frame.extractedValues[k] = n;
          }
        }
        if (typeof v === 'object') scan(v, depth + 1);
      }
    };
    scan(frame.parsed);
    if (Object.keys(frame.extractedValues).length > 0) frame.flags.push('HAS_NUMBERS');
  }

  // Update connection stats
  conn.frameCount[direction]++;
  conn.byteCount[direction] += frame.size;

  // Keep ring buffer
  conn.frames.push(frame);
  if (conn.frames.length > conn.MAX_FRAMES) conn.frames.shift();

  return frame;
}

// ── Per-tab stats (for popup) ─────────────────────────────────────────────────
const tabStats = new Map();

function getTabStats(tabId) {
  if (!tabStats.has(tabId)) {
    tabStats.set(tabId, {
      requests: 0, tokens: 0, authCookies: 0, websockets: 0,
      wsFrames: 0,
      events: []
    });
  }
  return tabStats.get(tabId);
}

function recordTabEvent(tabId, event) {
  if (!tabId) return;
  const s = getTabStats(tabId);
  if (event.type === "request")     s.requests++;
  if (event.type === "auth_cookie") s.authCookies++;
  if (event.type === "websocket")   { s.websockets++; s.wsFrames++; }
  if (event.type === "websocket_opened") s.websockets++;
  if (event.type === "response_body") {
    const auth = event.reqHeaders?.authorization || event.reqHeaders?.Authorization || "";
    if (auth.toLowerCase().startsWith("bearer ")) s.tokens++;
  }
  s.events.unshift({ ...event, _tabId: tabId });
  if (s.events.length > 100) s.events.length = 100;
}

console.log('🦁 Scraper v3.3 Starting...');

function send(obj) {
  if (nativePort) {
    try { nativePort.postMessage(obj); }
    catch(e) { console.error('send failed', e); }
  }
}

function getDomain(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); }
  catch { return 'unknown'; }
}


// ── Task / Session Token Scanner ─────────────────────────────────────────────
const TOKEN_REGEXES = [
  /"(task|taskId|task_id|jobId|job_id|sessionToken|session_token|nonce|_token|csrf[a-zA-Z_]*|xsrf[a-zA-Z_]*|upload_?[Tt]oken|api_?[Tt]oken|requestToken|auth_?[Tt]oken)"\s*:\s*"([a-zA-Z0-9_\-\.]{8,512})"/g,
  /\b(task|taskId|task_id|jobId|sessionToken|nonce|csrfToken|csrf_token|xsrfToken|uploadToken|apiToken|_token)\s*[=:]\s*["']([a-zA-Z0-9_\-\.]{8,512})["']/g,
  /data-(?:task|job|token|csrf|nonce|session)[a-zA-Z0-9\-]*=["']([a-zA-Z0-9_\-\.]{8,512})["']/gi,
];

function extractTokensFromText(text, sourceUrl) {
  if (!text || text.length < 10) return [];
  const found = [], seen = new Set();
  for (const re of TOKEN_REGEXES) {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      const name  = m[2] ? m[1] : 'token';
      const value = m[2] || m[1];
      const key   = name.toLowerCase() + ':' + value;
      if (!seen.has(key) && value.length >= 8) {
        seen.add(key);
        found.push({ name: name.toLowerCase(), value, source: sourceUrl });
      }
    }
  }
  return found;
}

function scanBodyForTokens(bodyText, url, domain, tabId) {
  const tokens = extractTokensFromText(bodyText, url);
  if (!tokens.length) return;
  const evt = { type: 'task_tokens', domain, url, tokens, timestamp: Date.now(), tabId };
  send(evt);
  recordTabEvent(tabId, evt);
}

function extractPageGlobals(tabId, domain, pageUrl) {
  chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      const results = [], seen = new Set();
      const add = (name, value, src) => {
        if (!value || typeof value !== 'string' || value.length < 8) return;
        const k = name + ':' + value;
        if (seen.has(k)) return;
        seen.add(k);
        results.push({ name, value, source: src || 'window_global' });
      };
      const GLOBAL_NAMES = ['task','taskId','task_id','jobId','job_id','nonce',
        'csrfToken','csrf_token','_token','xsrfToken','uploadToken','apiToken',
        'sessionToken','ILovePDF','App','__INITIAL_STATE__','__CONFIG__','bootstrap'];
      for (const g of GLOBAL_NAMES) {
        try {
          const v = window[g];
          if (typeof v === 'string') { add(g, v, 'window.' + g); continue; }
          if (v && typeof v === 'object') {
            for (const k of Object.keys(v).slice(0, 50)) {
              if (/task|token|csrf|nonce|job|session|auth|key|secret/i.test(k))
                add(g + '.' + k, String(v[k] ?? ''), 'window.' + g + '.' + k);
            }
          }
        } catch {}
      }
      const scriptREs = [
        /"(task|taskId|task_id|jobId|sessionToken|nonce|_token|csrfToken|xsrfToken|uploadToken)"\s*:\s*"([a-zA-Z0-9_\-\.]{8,512})"/g,
        /\b(task|taskId|job_?[Ii]d|sessionToken|csrfToken|nonce|uploadToken|apiToken)\s*[=:]\s*["']([a-zA-Z0-9_\-\.]{8,512})["']/g,
      ];
      document.querySelectorAll('script:not([src])').forEach(s => {
        const t = s.textContent || '';
        for (const re of scriptREs) {
          re.lastIndex = 0; let m;
          while ((m = re.exec(t)) !== null) add(m[1], m[2], 'inline_script');
        }
      });
      document.querySelectorAll('meta').forEach(el => {
        const name = el.getAttribute('name') || el.getAttribute('property') || '';
        const val  = el.getAttribute('content') || '';
        if (/token|csrf|nonce|task|auth/i.test(name) && val) add(name, val, 'meta:' + name);
      });
      ['task','job','token','csrf','nonce','session'].forEach(attr => {
        const bv = document.body?.dataset?.[attr];
        const hv = document.documentElement?.dataset?.[attr];
        if (bv) add('body-data-' + attr, bv, 'data-' + attr);
        if (hv) add('html-data-' + attr, hv, 'data-' + attr);
      });
      return results;
    }
  }, (results) => {
    if (chrome.runtime.lastError || !results?.[0]?.result?.length) return;
    const tokens = results[0].result;
    const evt = { type: 'task_tokens', domain, url: pageUrl, tokens, source: 'page_globals', timestamp: Date.now(), tabId };
    send(evt);
    recordTabEvent(tabId, evt);
  });
}

// ── DOM Map ───────────────────────────────────────────────────────────────────
function mapDOM(tabId, domain) {
  chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      const all = document.querySelectorAll('*');
      const tagCount = {}, classCount = {}, ids = [];
      all.forEach(el => {
        const tag = el.tagName.toLowerCase();
        tagCount[tag] = (tagCount[tag] || 0) + 1;
        el.classList.forEach(c => {
          if (c && c.length < 100) classCount[c] = (classCount[c] || 0) + 1;
        });
        if (el.id && el.id.length < 100) ids.push(el.id);
      });
      return {
        url: window.location.href, title: document.title,
        tags:    Object.entries(tagCount).map(([tag,count])=>({tag,count})).sort((a,b)=>b.count-a.count),
        classes: Object.entries(classCount).map(([name,count])=>({name,count})).sort((a,b)=>b.count-a.count),
        ids: [...new Set(ids)]
      };
    }
  }, (results) => {
    if (chrome.runtime.lastError || !results?.[0]) return;
    const evt = { type: 'dommap', domain, url: results[0].result?.url, dommap: results[0].result, timestamp: Date.now() };
    send(evt);
    recordTabEvent(tabId, evt);
    console.log(`🗺️ DOM mapped for ${domain}`);
  });
}

// ── Fingerprint capture ───────────────────────────────────────────────────────
function captureFingerprint(tabId) {
  chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      let webglVendor = '', webglRenderer = '';
      try {
        const canvas = document.createElement('canvas');
        const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
        if (gl) {
          const ext = gl.getExtension('WEBGL_debug_renderer_info');
          if (ext) {
            webglVendor   = gl.getParameter(ext.UNMASKED_VENDOR_WEBGL);
            webglRenderer = gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
          }
        }
      } catch (_) {}
      let audioSampleRate = null;
      try {
        const actx = new (window.AudioContext || window.webkitAudioContext)();
        audioSampleRate = actx.sampleRate;
        actx.close();
      } catch (_) {}
      return {
        userAgent:           navigator.userAgent,
        language:            navigator.language,
        languages:           Array.from(navigator.languages || []),
        platform:            navigator.platform,
        hardwareConcurrency: navigator.hardwareConcurrency,
        deviceMemory:        navigator.deviceMemory ?? null,
        cookieEnabled:       navigator.cookieEnabled,
        doNotTrack:          navigator.doNotTrack,
        vendor:              navigator.vendor,
        maxTouchPoints:      navigator.maxTouchPoints,
        screen: {
          width:       screen.width,
          height:      screen.height,
          availWidth:  screen.availWidth,
          availHeight: screen.availHeight,
          colorDepth:  screen.colorDepth,
          pixelDepth:  screen.pixelDepth,
        },
        devicePixelRatio: window.devicePixelRatio,
        timezone:         Intl.DateTimeFormat().resolvedOptions().timeZone,
        timezoneOffset:   new Date().getTimezoneOffset(),
        webgl:            { vendor: webglVendor, renderer: webglRenderer },
        audioSampleRate,
        connection:       navigator.connection ? {
          effectiveType: navigator.connection.effectiveType,
          downlink:      navigator.connection.downlink,
          rtt:           navigator.connection.rtt,
        } : null,
        acceptHeaders: {
          accept:         'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
          acceptLanguage: (navigator.languages || [navigator.language]).join(','),
          acceptEncoding: 'gzip, deflate, br',
        },
      };
    }
  }, (results) => {
    if (chrome.runtime.lastError || !results?.[0]?.result) {
      console.log('Fingerprint capture error:', chrome.runtime.lastError?.message);
      return;
    }
    const domain = tabContexts.get(tabId) || 'unknown';
    const evt = {
      type:        'fingerprint',
      domain,
      tabId,
      fingerprint: results[0].result,
      timestamp:   Date.now(),
    };
    send(evt);
    recordTabEvent(tabId, evt);
    console.log(`🖥️ Fingerprint captured for ${domain}`);
  });
}

// ── Navigate + Track ──────────────────────────────────────────────────────────
function navigateAndTrack(url) {
  const domain = getDomain(url);
  chrome.tabs.create({ url, active: true }, (tab) => {
    if (chrome.runtime.lastError) {
      console.error('tabs.create failed:', chrome.runtime.lastError.message);
      return;
    }
    tabContexts.set(tab.id, domain);
    const listener = (tabId, info) => {
      if (tabId !== tab.id) return;
      if (info.status === 'loading') {
        chrome.tabs.onUpdated.removeListener(listener);
        attachDebugger(tabId);
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
    send({ type: 'nav_started', url, domain, tabId: tab.id, timestamp: Date.now() });
    console.log('🚀 Navigating to', url, 'tab:', tab.id);
  });
}

// ── Debugger ──────────────────────────────────────────────────────────────────
function attachDebugger(tabId) {
  if (debuggedTabs.has(tabId)) return;
  chrome.debugger.attach({ tabId }, '1.3', () => {
    if (chrome.runtime.lastError) {
      console.error('Attach failed:', chrome.runtime.lastError.message); return;
    }
    debuggedTabs.add(tabId);
    const evt = { type: 'debugger_status', status: 'attached', tabId, timestamp: Date.now() };
    send(evt);
    recordTabEvent(tabId, evt);

    chrome.debugger.sendCommand({ tabId }, 'Network.enable', {
      maxTotalBufferSize:    10000000,
      maxResourceBufferSize: 5000000
    });
    // ← ADD THIS — attach to iframes/workers automatically
    chrome.debugger.sendCommand({ tabId }, 'Target.setAutoAttach', {
      autoAttach:             true,
      waitForDebuggerOnStart: false,
      flatten:                true,   // critical — makes iframe events come through same onEvent
    });

    chrome.debugger.sendCommand({ tabId }, 'Fetch.enable', {
      patterns: [{ urlPattern: '*', requestStage: 'Response' }]
    });

    console.log('🔬 Debugger attached to tab', tabId);
  });
}

function detachDebugger(tabId) {
  if (!debuggedTabs.has(tabId)) return;
  chrome.debugger.detach({ tabId }, () => {
    debuggedTabs.delete(tabId);
    tabContexts.delete(tabId);
    pendingRequests.forEach((v, k) => { if (v.tabId === tabId) pendingRequests.delete(k); });
    // Clean up WS connections for this tab
    wsConnections.forEach((conn, id) => {
      if (conn.tabId === tabId) wsConnections.delete(id);
    });
    send({ type: 'debugger_status', status: 'detached', tabId, timestamp: Date.now() });
  });
}

// ── Auto DOM map + fingerprint on page load ───────────────────────────────────
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === 'complete' && debuggedTabs.has(tabId)) {
    const domain = tabContexts.get(tabId) || getDomain(tab.url || '');
    tabContexts.set(tabId, domain);
    setTimeout(() => mapDOM(tabId, domain), 1500);
    setTimeout(() => extractPageGlobals(tabId, domain, tab.url || ''), 2000);
    setTimeout(() => captureFingerprint(tabId), 2500);
    console.log('📄 Page loaded, capturing fingerprint for', domain);
  }
});

// ── CDP events ────────────────────────────────────────────────────────────────
chrome.debugger.onEvent.addListener((source, method, params) => {
  const tabId  = source.tabId;
  const domain = tabContexts.get(tabId) || 'unknown';

  // ── HTTP request/response (unchanged) ─────────────────────────────────────
  if (method === 'Network.requestWillBeSent') {
    const req = params.request;
    pendingRequests.set(params.requestId, {
      tabId, domain, url: req.url, method: req.method,
      headers: req.headers, postData: req.postData || null
    });
    const flags = isInteresting(req);
    if (flags.length > 0) {
      const evt = {
        type: 'request', domain, url: req.url, method: req.method,
        headers: req.headers, postData: req.postData || null,
        reqType: params.type, flags, requestId: params.requestId,
        timestamp: Date.now()
      };
      send(evt);
      recordTabEvent(tabId, evt);
    }
  }

  else if (method === 'Network.responseReceived') {
    const res     = params.response;
    const pending = pendingRequests.get(params.requestId);
    const flags   = isInteresting({ url: res.url, headers: res.headers, method: pending?.method });
    const isJson  = (res.mimeType || '').includes('json') || (res.headers?.['content-type'] || '').includes('json');
    const isHtml  = (res.mimeType || '').includes('html');

    if (flags.length > 0 || isJson || isHtml) {
      const evt = {
        type: 'response', domain, url: res.url, status: res.status,
        statusText: res.statusText, headers: res.headers,
        mimeType: res.mimeType, requestId: params.requestId,
        reqMethod: pending?.method, reqHeaders: pending?.headers,
        reqPostData: pending?.postData, flags, timestamp: Date.now()
      };
      send(evt);
      recordTabEvent(tabId, evt);
    }
    pendingRequests.delete(params.requestId);
  }

  else if (method === 'Fetch.requestPaused') {
    const req    = params.request;
    const flags  = isInteresting(req);
    const mime   = params.responseHeaders?.find(h => h.name.toLowerCase() === 'content-type')?.value || '';
    const isJson = mime.includes('json') || mime.includes('javascript');
    const isHtml = mime.includes('html');

    if (isJson || isHtml || flags.length > 0) {
      chrome.debugger.sendCommand({ tabId }, 'Fetch.getResponseBody',
        { requestId: params.requestId }, (body) => {
          if (!chrome.runtime.lastError && body?.body) {
            const evt = {
              type: 'response_body', domain, url: req.url,
              method: req.method, status: params.responseStatusCode,
              body: body.body, base64: body.base64Encoded,
              mimeType: mime,
              reqHeaders: req.headers, resHeaders: params.responseHeaders,
              requestId: params.requestId,
              flags, timestamp: Date.now()
            };
            send(evt);
            recordTabEvent(tabId, evt);
            if (!body.base64Encoded && body.body && (isJson || isHtml)) {
              scanBodyForTokens(body.body, req.url, domain, tabId);
            }
          }
          chrome.debugger.sendCommand({ tabId }, 'Fetch.continueRequest',
            { requestId: params.requestId });
        });
    } else {
      chrome.debugger.sendCommand({ tabId }, 'Fetch.continueRequest',
        { requestId: params.requestId });
    }
  }

  // ══════════════════════════════════════════════════════════════════════════
  // ── WebSocket FULL lifecycle capture ──────────────────────────────────────
  // ══════════════════════════════════════════════════════════════════════════

  else if (method === 'Network.webSocketCreated') {
    // New WS connection opened
    const conn = makeWSConnection(params.requestId, params.url, tabId, domain);
    wsConnections.set(params.requestId, conn);

    const evt = {
      type:       'websocket_opened',
      requestId:  params.requestId,
      url:        params.url,
      domain,
      tabId,
      timestamp:  Date.now(),
      initiator:  params.initiator || null,
    };
    send(evt);
    recordTabEvent(tabId, evt);
    console.log(`🔌 WS opened: ${params.url} (tab ${tabId})`);
  }

  else if (method === 'Network.webSocketHandshakeResponseReceived') {
    // Upgrade successful — headers contain cookies, auth, etc.
    const conn = wsConnections.get(params.requestId);
    if (conn) {
      conn.handshakeHeaders = params.response?.headers || {};
      conn.handshakeStatus  = params.response?.status;
    }
    const evt = {
      type:       'websocket_handshake',
      requestId:  params.requestId,
      domain,
      tabId,
      timestamp:  Date.now(),
      status:     params.response?.status,
      headers:    params.response?.headers || {},
    };
    send(evt);
    recordTabEvent(tabId, evt);
  }

  else if (method === 'Network.webSocketFrameReceived') {
    const conn    = wsConnections.get(params.requestId);
    const payload = params.response?.payloadData ?? '';
    const opcode  = params.response?.opcode ?? 1;

    let frame = null;
    if (conn) {
      frame = wsAddFrame(conn, 'recv', payload, opcode);
    }

    const evt = {
      type:       'websocket',
      subtype:    'frame',
      direction:  'recv',
      requestId:  params.requestId,
      domain,
      tabId,
      timestamp:  Date.now(),
      payload,
      opcode,
      parsed:     frame?.parsed     ?? null,
      flags:      frame?.flags      ?? [],
      extracted:  frame?.extractedValues ?? {},
      // Connection stats snapshot
      connStats: conn ? {
        url:        conn.url,
        frameCount: { ...conn.frameCount },
        byteCount:  { ...conn.byteCount },
        patterns:   [...conn.patterns],
        age:        Date.now() - conn.createdAt,
      } : null,
    };
    send(evt);
    recordTabEvent(tabId, evt);

    // Alert for high-value patterns
    if (frame?.flags?.some(f => ['CRASH_POINT','MULTIPLIER','GAME_STATE','RESULT'].includes(f))) {
      console.log(`🎯 WS INTERESTING [${frame.flags.join(',')}]:`, payload.slice(0, 200));
    }
  }

  else if (method === 'Network.webSocketFrameSent') {
    const conn    = wsConnections.get(params.requestId);
    const payload = params.response?.payloadData ?? '';
    const opcode  = params.response?.opcode ?? 1;

    let frame = null;
    if (conn) {
      frame = wsAddFrame(conn, 'sent', payload, opcode);
    }

    const evt = {
      type:       'websocket',
      subtype:    'frame',
      direction:  'sent',
      requestId:  params.requestId,
      domain,
      tabId,
      timestamp:  Date.now(),
      payload,
      opcode,
      parsed:     frame?.parsed     ?? null,
      flags:      frame?.flags      ?? [],
      extracted:  frame?.extractedValues ?? {},
      connStats: conn ? {
        url:        conn.url,
        frameCount: { ...conn.frameCount },
        patterns:   [...conn.patterns],
        age:        Date.now() - conn.createdAt,
      } : null,
    };
    send(evt);
    recordTabEvent(tabId, evt);
  }

  else if (method === 'Network.webSocketFrameError') {
    const conn = wsConnections.get(params.requestId);
    const evt = {
      type:       'websocket_error',
      requestId:  params.requestId,
      domain,
      tabId,
      timestamp:  Date.now(),
      errorMessage: params.errorMessage,
      connStats: conn ? {
        url:        conn.url,
        frameCount: { ...conn.frameCount },
        patterns:   [...conn.patterns],
      } : null,
    };
    send(evt);
    recordTabEvent(tabId, evt);
    console.warn(`🔌 WS error on ${params.requestId}:`, params.errorMessage);
  }

  else if (method === 'Network.webSocketClosed') {
    const conn = wsConnections.get(params.requestId);
    if (conn) conn.closedAt = Date.now();

    const evt = {
      type:       'websocket_closed',
      requestId:  params.requestId,
      domain,
      tabId,
      timestamp:  Date.now(),
      summary: conn ? {
        url:        conn.url,
        duration:   conn.closedAt - conn.createdAt,
        frameCount: { ...conn.frameCount },
        byteCount:  { ...conn.byteCount },
        patterns:   [...conn.patterns],
      } : null,
    };
    send(evt);
    recordTabEvent(tabId, evt);

    // Keep connection in registry for 60s for any late queries, then remove
    if (conn) setTimeout(() => wsConnections.delete(params.requestId), 60000);

    console.log(`🔌 WS closed: ${params.requestId}`);
  }
});

// ── isInteresting ─────────────────────────────────────────────────────────────
function isInteresting(req) {
  const flags   = [];
  const url     = (req.url      || '').toLowerCase();
  const headers =  req.headers  || {};
  const post    = (req.postData || '').toLowerCase();

  const authHeaders = ['authorization','x-auth-token','x-access-token','x-api-key',
                       'api-key','token','x-token','x-csrf-token','csrf-token'];
  for (const h of authHeaders) {
    if (headers[h] || headers[h.toLowerCase()]) flags.push('AUTH:' + h);
  }
  const auth = headers['authorization'] || headers['Authorization'] || '';
  if (auth.toLowerCase().startsWith('bearer ')) flags.push('BEARER_TOKEN');
  if (auth.toLowerCase().startsWith('basic '))  flags.push('BASIC_AUTH');

  if (headers['cf-ray'] || headers['CF-Ray'])             flags.push('CLOUDFLARE');
  if (headers['cf-clearance'] || headers['CF-Clearance']) flags.push('CF_CLEARANCE');
  if (url.includes('cloudflare') || url.includes('/cdn-cgi/')) flags.push('CF_URL');
  if (headers['__cf_bm'] || post.includes('__cf_bm'))     flags.push('CF_BOT_MGMT');
  if (url.includes('turnstile') || post.includes('cf-turnstile')) flags.push('CF_TURNSTILE');
  if (url.includes('hcaptcha'))  flags.push('HCAPTCHA');
  if (url.includes('recaptcha')) flags.push('RECAPTCHA');

  if (url.includes('/api/') || url.includes('/graphql') ||
      url.includes('/v1/')  || url.includes('/v2/') || url.includes('/v3/')) flags.push('API');
  if (url.includes('login') || url.includes('signin') || url.includes('oauth') ||
      url.includes('token') || url.includes('auth')   || url.includes('session')) flags.push('AUTH_FLOW');

  if (req.method === 'POST' && req.postData) flags.push('POST_DATA');
  if (headers['cookie'] || headers['Cookie']) flags.push('HAS_COOKIES');
  if (url.startsWith('ws://') || url.startsWith('wss://')) flags.push('WEBSOCKET');

  return flags;
}

// ── Cookie stream ─────────────────────────────────────────────────────────────
chrome.cookies.onChanged.addListener((changeInfo) => {
  if (!nativePort) return;
  const c      = changeInfo.cookie;
  const domain = c.domain.replace(/^\./, '');
  const name   = c.name.toLowerCase();
  const authNames = ['session','auth','token','jwt','csrf','cf_clearance',
                     '__cf_bm','login','sid','user','account','access'];
  if (authNames.some(k => name.includes(k))) {
    const evt = {
      type: 'auth_cookie', domain,
      cookie: { name: c.name, domain: c.domain, value: c.value,
                httpOnly: c.httpOnly, secure: c.secure },
      cause: changeInfo.cause, removed: changeInfo.removed, timestamp: Date.now()
    };
    send(evt);
    debuggedTabs.forEach(tabId => {
      if (tabContexts.get(tabId) === domain) recordTabEvent(tabId, evt);
    });
  }
  send({
    type: 'cookies_changed', domain,
    cookie: { name: c.name, domain: c.domain, value: c.value },
    cause: changeInfo.cause, removed: changeInfo.removed, timestamp: Date.now()
  });
});

// ── Tab cleanup ───────────────────────────────────────────────────────────────
chrome.tabs.onRemoved.addListener((tabId) => {
  if (debuggedTabs.has(tabId)) debuggedTabs.delete(tabId);
  tabContexts.delete(tabId);
  pendingRequests.forEach((v, k) => { if (v.tabId === tabId) pendingRequests.delete(k); });
  wsConnections.forEach((conn, id) => { if (conn.tabId === tabId) wsConnections.delete(id); });
  setTimeout(() => tabStats.delete(tabId), 60000);
});

// ── Commands from native host ─────────────────────────────────────────────────
function handleNativeCommand(msg) {
  const { command } = msg;
  console.log('📨 Native command:', command);

  if (command === 'navigate' || command === 'nav') {
    const url = msg.url || msg.args || '';
    navigateAndTrack(url);
  }
  else if (command === 'track') {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs[0]) {
        tabContexts.set(tabs[0].id, getDomain(tabs[0].url));
        attachDebugger(tabs[0].id);
      }
    });
  }
  else if (command === 'untrack') {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs[0]) detachDebugger(tabs[0].id);
    });
  }
  else if (command === 'ws_list') {
    // Return all active WS connections
    const list = [];
    wsConnections.forEach((conn, id) => {
      list.push({
        requestId:  id,
        url:        conn.url,
        domain:     conn.domain,
        tabId:      conn.tabId,
        createdAt:  conn.createdAt,
        closedAt:   conn.closedAt,
        frameCount: conn.frameCount,
        byteCount:  conn.byteCount,
        patterns:   [...conn.patterns],
        open:       !conn.closedAt,
      });
    });
    send({ type: 'ws_list', connections: list, timestamp: Date.now() });
  }
  else if (command === 'ws_frames') {
    // Return buffered frames for a specific connection
    const conn = wsConnections.get(msg.requestId);
    if (conn) {
      send({
        type:      'ws_frames_dump',
        requestId: msg.requestId,
        url:       conn.url,
        frames:    conn.frames,
        timestamp: Date.now(),
      });
    }
  }
  else if (command === 'fingerprint') {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs[0]) {
        const domain = tabContexts.get(tabs[0].id) || getDomain(tabs[0].url || '');
        tabContexts.set(tabs[0].id, domain);
        captureFingerprint(tabs[0].id);
      }
    });
  }
  else if (command === 'get_cookies') {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (!tabs[0]) return;
      chrome.cookies.getAll({ url: tabs[0].url }, (cookies) => {
        send({ type: 'cookies', domain: getDomain(tabs[0].url), cookies, url: tabs[0].url, timestamp: Date.now() });
      });
    });
  }
  else if (command === 'get_storage') {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (!tabs[0]) return;
      chrome.scripting.executeScript({
        target: { tabId: tabs[0].id },
        func: () => ({ localStorage: { ...localStorage }, sessionStorage: { ...sessionStorage }, url: window.location.href })
      }, (r) => {
        if (r?.[0]) send({ type: 'storage', domain: getDomain(tabs[0].url), data: r[0].result, timestamp: Date.now() });
      });
    });
  }
  else if (command === 'get_html') {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (!tabs[0]) return;
      chrome.scripting.executeScript({
        target: { tabId: tabs[0].id },
        func: () => ({ html: document.documentElement.outerHTML, title: document.title, url: window.location.href })
      }, (r) => {
        if (r?.[0]) send({ type: 'html', domain: getDomain(tabs[0].url), data: r[0].result, timestamp: Date.now() });
      });
    });
  }
  else if (command === 'screenshot') {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (!tabs[0]) return;
      chrome.tabs.captureVisibleTab(tabs[0].windowId, { format: 'png' }, (dataUrl) => {
        send({ type: 'screenshot', domain: getDomain(tabs[0].url), dataUrl, url: tabs[0].url, timestamp: Date.now() });
      });
    });
  }
  else if (command === 'dommap') {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs[0]) {
        const domain = tabContexts.get(tabs[0].id) || getDomain(tabs[0].url);
        mapDOM(tabs[0].id, domain);
      }
    });
  }
}

// ── Popup message handlers ────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const { command, tabId, action } = msg;

  if (command === 'popup_get_state') {
    const stats      = getTabStats(tabId);
    const isTracking = debuggedTabs.has(tabId);

    // Include active WS connections for this tab
    const tabWsConns = [];
    wsConnections.forEach((conn, id) => {
      if (conn.tabId === tabId) {
        tabWsConns.push({
          requestId:  id,
          url:        conn.url,
          open:       !conn.closedAt,
          frameCount: conn.frameCount,
          patterns:   [...conn.patterns],
        });
      }
    });

    sendResponse({
      nativeConnected: !!nativePort,
      isTracking,
      stats: {
        requests:    stats.requests,
        tokens:      stats.tokens,
        authCookies: stats.authCookies,
        websockets:  stats.websockets,
        wsFrames:    stats.wsFrames,
      },
      wsConnections:  tabWsConns,
      totalEvents:    stats.events.length,
      events:         stats.events.slice(0, 30),
    });
    return true;
  }

  if (command === 'popup_track') {
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError || !tab) { sendResponse({ success: false }); return; }
      tabContexts.set(tabId, getDomain(tab.url || ''));
      attachDebugger(tabId);
      setTimeout(() => captureFingerprint(tabId), 1000);
      sendResponse({ success: true });
    });
    return true;
  }

  if (command === 'popup_untrack') {
    detachDebugger(tabId);
    sendResponse({ success: true });
    return true;
  }

  if (command === 'popup_action') {
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError || !tab) { sendResponse({ success: false }); return; }
      const domain = tabContexts.get(tabId) || getDomain(tab.url || '');

      if (action === 'dommap') {
        mapDOM(tabId, domain);
        sendResponse({ success: true });
      } else if (action === 'fingerprint') {
        captureFingerprint(tabId);
        sendResponse({ success: true });
      } else if (action === 'screenshot') {
        chrome.tabs.captureVisibleTab(tab.windowId, { format: 'png' }, (dataUrl) => {
          if (chrome.runtime.lastError) { sendResponse({ success: false }); return; }
          const evt = { type: 'screenshot', domain, dataUrl, url: tab.url, timestamp: Date.now() };
          send(evt); recordTabEvent(tabId, evt);
          sendResponse({ success: true });
        });
      } else if (action === 'get_html') {
        chrome.scripting.executeScript({
          target: { tabId },
          func: () => ({ html: document.documentElement.outerHTML, title: document.title, url: window.location.href })
        }, (r) => {
          if (chrome.runtime.lastError || !r?.[0]) { sendResponse({ success: false }); return; }
          const evt = { type: 'html', domain, data: r[0].result, timestamp: Date.now() };
          send(evt); recordTabEvent(tabId, evt);
          sendResponse({ success: true });
        });
      } else if (action === 'get_cookies') {
        chrome.cookies.getAll({ url: tab.url }, (cookies) => {
          const evt = { type: 'cookies', domain, cookies, url: tab.url, timestamp: Date.now() };
          send(evt); recordTabEvent(tabId, evt);
          sendResponse({ success: true });
        });
      } else if (action === 'get_storage') {
        chrome.scripting.executeScript({
          target: { tabId },
          func: () => ({ localStorage: { ...localStorage }, sessionStorage: { ...sessionStorage }, url: window.location.href })
        }, (r) => {
          if (chrome.runtime.lastError || !r?.[0]) { sendResponse({ success: false }); return; }
          const evt = { type: 'storage', domain, data: r[0].result, timestamp: Date.now() };
          send(evt); recordTabEvent(tabId, evt);
          sendResponse({ success: true });
        });
      } else {
        sendResponse({ success: false });
      }
    });
    return true;
  }

  if (command === 'popup_clear_tab') {
    tabStats.delete(tabId);
    sendResponse({ success: true });
    return true;
  }

  if (command === 'navigate') { navigateAndTrack(msg.url); }
  if (command === 'ping') { /* keepalive */ }
});

// ── Native messaging ──────────────────────────────────────────────────────────
function connectToNative() {
  try {
    nativePort = chrome.runtime.connectNative('com.scraper.core');

    nativePort.onMessage.addListener((msg) => {
      if (!msg) return;
      reconnectAttempts = 0;
      console.log('📩 From native:', JSON.stringify(msg).slice(0, 100));
      if (msg.command && msg.command !== 'pong') handleNativeCommand(msg);
      chrome.runtime.sendMessage(msg).catch(() => {});
    });

    nativePort.onDisconnect.addListener(() => {
      console.error('❌ Disconnected:', chrome.runtime.lastError?.message);
      stopKeepAlive(); nativePort = null;
      setTimeout(() => { reconnectAttempts++; connectToNative(); },
        Math.min(30000, reconnectAttempts * 5000 + 2000));
    });

    nativePort.postMessage({ command: 'register', browser: BROWSER, timestamp: Date.now() });
    startKeepAlive();
    console.log('✅ Native port open');
  } catch(e) {
    console.error('connect failed:', e);
    setTimeout(connectToNative, 5000);
  }
}

function startKeepAlive() {
  stopKeepAlive();
  keepAliveInterval = setInterval(() => {
    if (nativePort) {
      try { nativePort.postMessage({ command: 'ping', timestamp: Date.now() }); }
      catch(e) { stopKeepAlive(); }
    }
  }, 25000);
}
function stopKeepAlive() {
  if (keepAliveInterval) { clearInterval(keepAliveInterval); keepAliveInterval = null; }
}

connectToNative();