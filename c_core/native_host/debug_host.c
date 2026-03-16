// debug_host.c - SCRAPPER HOST v3.3 — FULL WEBSOCKET SUPPORT
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <time.h>
#include <pthread.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>

#define SOCKET_PATH    "/tmp/scraper.sock"
#define MAX_MSG        (5 * 1024 * 1024)
#define MAX_CLIENTS    4

// ── Dynamic paths ─────────────────────────────────────────────────────────────
static char BASE_DIR[512];
static char LOG_FILE[512];
static char DATA_DIR[512];
static char REQUESTS_FILE[512];
static char RESPONSES_FILE[512];
static char BODIES_FILE[512];
static char AUTH_FILE[512];
static char COOKIES_FILE[512];
static char WS_FILE[512];
static char WS_FRAMES_FILE[512];
static char WS_CONNECTIONS_FILE[512];
static char DOMMAP_FILE[512];
static char STORAGE_FILE[512];
static char FINGERPRINT_FILE[512];

void init_paths(void) {
    snprintf(BASE_DIR, sizeof(BASE_DIR), "/home/PeaseErnest/scraper");

    snprintf(LOG_FILE,             sizeof(LOG_FILE),             "%s/logs/debug_host.log",          BASE_DIR);
    snprintf(DATA_DIR,             sizeof(DATA_DIR),              "%s/data",                         BASE_DIR);
    snprintf(REQUESTS_FILE,        sizeof(REQUESTS_FILE),         "%s/data/requests.jsonl",          BASE_DIR);
    snprintf(RESPONSES_FILE,       sizeof(RESPONSES_FILE),        "%s/data/responses.jsonl",         BASE_DIR);
    snprintf(BODIES_FILE,          sizeof(BODIES_FILE),           "%s/data/bodies.jsonl",            BASE_DIR);
    snprintf(AUTH_FILE,            sizeof(AUTH_FILE),             "%s/data/auth.jsonl",              BASE_DIR);
    snprintf(COOKIES_FILE,         sizeof(COOKIES_FILE),          "%s/data/cookies.jsonl",           BASE_DIR);
    snprintf(WS_FILE,              sizeof(WS_FILE),               "%s/data/websockets.jsonl",        BASE_DIR);
    snprintf(WS_FRAMES_FILE,       sizeof(WS_FRAMES_FILE),        "%s/data/ws_frames.jsonl",         BASE_DIR);
    snprintf(WS_CONNECTIONS_FILE,  sizeof(WS_CONNECTIONS_FILE),   "%s/data/ws_connections.jsonl",    BASE_DIR);
    snprintf(DOMMAP_FILE,          sizeof(DOMMAP_FILE),           "%s/data/dommaps.jsonl",           BASE_DIR);
    snprintf(STORAGE_FILE,         sizeof(STORAGE_FILE),          "%s/data/storage.jsonl",           BASE_DIR);
    snprintf(FINGERPRINT_FILE,     sizeof(FINGERPRINT_FILE),      "%s/data/fingerprints.jsonl",      BASE_DIR);
}

void mkdir_p(const char *path) {
    char tmp[512];
    snprintf(tmp, sizeof(tmp), "%s", path);
    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') { *p = '\0'; mkdir(tmp, 0755); *p = '/'; }
    }
    mkdir(tmp, 0755);
}

// ── Mutexes & CLI state ───────────────────────────────────────────────────────
static pthread_mutex_t send_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t file_mutex = PTHREAD_MUTEX_INITIALIZER;
static int cli_clients[MAX_CLIENTS];
static int cli_count = 0;
static pthread_mutex_t cli_mutex = PTHREAD_MUTEX_INITIALIZER;

// ── WS connection tracking in C ───────────────────────────────────────────────
#define MAX_WS_CONNS 64
typedef struct {
    char  request_id[64];
    char  url[512];
    char  domain[128];
    long  created_at;
    long  closed_at;    // 0 = still open
    int   frames_recv;
    int   frames_sent;
    long  bytes_recv;
    long  bytes_sent;
    int   active;
} WSConn;

static WSConn ws_conns[MAX_WS_CONNS];
static int    ws_conn_count = 0;
static pthread_mutex_t ws_mutex = PTHREAD_MUTEX_INITIALIZER;

WSConn *ws_find(const char *req_id) {
    for (int i = 0; i < ws_conn_count; i++)
        if (ws_conns[i].active && strcmp(ws_conns[i].request_id, req_id) == 0)
            return &ws_conns[i];
    return NULL;
}

WSConn *ws_create(const char *req_id, const char *url, const char *domain) {
    pthread_mutex_lock(&ws_mutex);
    // Reuse closed slot first
    for (int i = 0; i < MAX_WS_CONNS; i++) {
        if (!ws_conns[i].active) {
            memset(&ws_conns[i], 0, sizeof(WSConn));
            strncpy(ws_conns[i].request_id, req_id,  sizeof(ws_conns[i].request_id)-1);
            strncpy(ws_conns[i].url,         url,     sizeof(ws_conns[i].url)-1);
            strncpy(ws_conns[i].domain,      domain,  sizeof(ws_conns[i].domain)-1);
            ws_conns[i].created_at = (long)time(NULL);
            ws_conns[i].active     = 1;
            if (i >= ws_conn_count) ws_conn_count = i + 1;
            pthread_mutex_unlock(&ws_mutex);
            return &ws_conns[i];
        }
    }
    // Overwrite oldest if full
    int oldest = 0;
    for (int i = 1; i < MAX_WS_CONNS; i++)
        if (ws_conns[i].created_at < ws_conns[oldest].created_at) oldest = i;
    memset(&ws_conns[oldest], 0, sizeof(WSConn));
    strncpy(ws_conns[oldest].request_id, req_id, sizeof(ws_conns[oldest].request_id)-1);
    strncpy(ws_conns[oldest].url,        url,    sizeof(ws_conns[oldest].url)-1);
    strncpy(ws_conns[oldest].domain,     domain, sizeof(ws_conns[oldest].domain)-1);
    ws_conns[oldest].created_at = (long)time(NULL);
    ws_conns[oldest].active     = 1;
    pthread_mutex_unlock(&ws_mutex);
    return &ws_conns[oldest];
}

// ── Logging ───────────────────────────────────────────────────────────────────
void write_log(const char *msg) {
    FILE *f = fopen(LOG_FILE, "a");
    if (!f) return;
    fprintf(f, "[%ld] %s\n", (long)time(NULL), msg);
    fflush(f);
    fclose(f);
}

// ── File saving ───────────────────────────────────────────────────────────────
void save_to_file(const char *filepath, const char *json) {
    pthread_mutex_lock(&file_mutex);
    FILE *f = fopen(filepath, "a");
    if (f) { fprintf(f, "%s\n", json); fflush(f); fclose(f); }
    pthread_mutex_unlock(&file_mutex);
}

// ── CLI broadcast ─────────────────────────────────────────────────────────────
void broadcast_to_cli(const char *line) {
    pthread_mutex_lock(&cli_mutex);
    for (int i = 0; i < cli_count; i++) {
        send(cli_clients[i], line, strlen(line), MSG_NOSIGNAL);
        send(cli_clients[i], "\n", 1, MSG_NOSIGNAL);
    }
    pthread_mutex_unlock(&cli_mutex);
}

void remove_cli_client(int fd) {
    pthread_mutex_lock(&cli_mutex);
    for (int i = 0; i < cli_count; i++) {
        if (cli_clients[i] == fd) {
            close(fd);
            cli_clients[i] = cli_clients[--cli_count];
            break;
        }
    }
    pthread_mutex_unlock(&cli_mutex);
}

// ── Native messaging I/O ──────────────────────────────────────────────────────
int send_message(const char *msg) {
    if (!msg) return -1;
    uint32_t len = (uint32_t)strlen(msg);
    if (len > MAX_MSG) { write_log("ERROR: msg too large"); return -1; }
    pthread_mutex_lock(&send_mutex);
    fwrite(&len, 4, 1, stdout);
    fwrite(msg, 1, len, stdout);
    fflush(stdout);
    pthread_mutex_unlock(&send_mutex);
    char buf[256];
    snprintf(buf, sizeof(buf), "SENT: %.200s", msg);
    write_log(buf);
    return 0;
}

char *receive_message(void) {
    uint32_t len;
    if (fread(&len, 4, 1, stdin) != 1) {
        write_log(feof(stdin) ? "Browser disconnected" : "ERROR: read len");
        return NULL;
    }
    if (len == 0 || len > MAX_MSG) {
        char err[128];
        snprintf(err, sizeof(err), "ERROR: bad length %u", len);
        write_log(err);
        char tmp[4096]; uint32_t left = len;
        while (left > 0) {
            size_t n = left > sizeof(tmp) ? sizeof(tmp) : left;
            fread(tmp, 1, n, stdin);
            left -= (uint32_t)n;
        }
        return NULL;
    }
    char *buf = malloc(len + 1);
    if (!buf) { write_log("ERROR: malloc"); return NULL; }
    if (fread(buf, 1, len, stdin) != len) {
        write_log("ERROR: read body"); free(buf); return NULL;
    }
    buf[len] = '\0';
    return buf;
}

// ── JSON helpers ──────────────────────────────────────────────────────────────
void json_get_str(const char *json, const char *key, char *out, size_t outlen) {
    char search[128];
    snprintf(search, sizeof(search), "\"%s\":\"", key);
    const char *p = strstr(json, search);
    if (!p) { out[0] = '\0'; return; }
    p += strlen(search);
    size_t i = 0;
    while (*p && *p != '"' && i < outlen - 1) out[i++] = *p++;
    out[i] = '\0';
}

// Extract array like ["FLAG1","FLAG2"]
void json_get_array_str(const char *json, const char *key, char *out, size_t outlen) {
    char search[128];
    snprintf(search, sizeof(search), "\"%s\":[", key);
    const char *p = strstr(json, search);
    if (!p) { out[0] = '\0'; return; }
    p += strlen(search) - 1; // point to '['
    const char *end = strchr(p, ']');
    if (!end || (size_t)(end - p + 2) > outlen) { out[0] = '\0'; return; }
    size_t len = (size_t)(end - p + 1);
    strncpy(out, p, len);
    out[len] = '\0';
}

// Safely extract payload preview (first N chars, handles escaping)
void json_get_payload_preview(const char *json, char *out, size_t outlen, size_t maxpreview) {
    // Look for "payload":"..."
    const char *p = strstr(json, "\"payload\":\"");
    if (!p) { out[0] = '\0'; return; }
    p += 11;
    size_t i = 0;
    while (*p && *p != '"' && i < outlen - 1 && i < maxpreview) {
        if (*p == '\\' && *(p+1)) { p++; } // skip escape
        out[i++] = *p++;
    }
    out[i] = '\0';
}

// ── Message router ────────────────────────────────────────────────────────────
void route_message(const char *msg) {
    char type[64];
    json_get_str(msg, "type", type, sizeof(type));

    if (strcmp(type, "request") == 0) {
        save_to_file(REQUESTS_FILE, msg);
        char url[256], method[16], flags[256];
        json_get_str(msg, "url",    url,    sizeof(url));
        json_get_str(msg, "method", method, sizeof(method));
        json_get_array_str(msg, "flags", flags, sizeof(flags));
        char line[600];
        snprintf(line, sizeof(line), "🌐 %s %s  %s", method, url, flags);
        broadcast_to_cli(line);
        write_log(line);
    }
    else if (strcmp(type, "response") == 0) {
        save_to_file(RESPONSES_FILE, msg);
        char url[256], status[8];
        json_get_str(msg, "url",    url,    sizeof(url));
        json_get_str(msg, "status", status, sizeof(status));
        char line[400];
        snprintf(line, sizeof(line), "📥 %s %s", status, url);
        broadcast_to_cli(line);
        write_log(line);
    }
    else if (strcmp(type, "response_body") == 0) {
        save_to_file(BODIES_FILE, msg);
        char url[256]; json_get_str(msg, "url", url, sizeof(url));
        char line[400]; snprintf(line, sizeof(line), "📦 BODY %s", url);
        broadcast_to_cli(line); write_log(line);
    }
    else if (strcmp(type, "auth_cookie") == 0) {
        save_to_file(AUTH_FILE, msg);
        char name[64], domain[128];
        json_get_str(msg, "name",   name,   sizeof(name));
        json_get_str(msg, "domain", domain, sizeof(domain));
        char line[300];
        snprintf(line, sizeof(line), "🔑 AUTH COOKIE %s @ %s", name, domain);
        broadcast_to_cli(line); write_log(line);
    }
    else if (strcmp(type, "cookies") == 0) {
        save_to_file(COOKIES_FILE, msg);
        char line[300];
        snprintf(line, sizeof(line), "🍪 COOKIES SAVED → %s", COOKIES_FILE);
        broadcast_to_cli(line);
    }
    else if (strcmp(type, "cookies_changed") == 0) {
        save_to_file(COOKIES_FILE, msg);
    }

    // ── WebSocket events ──────────────────────────────────────────────────────
    else if (strcmp(type, "websocket_opened") == 0) {
        save_to_file(WS_CONNECTIONS_FILE, msg);
        char req_id[64], url[512], domain[128];
        json_get_str(msg, "requestId", req_id, sizeof(req_id));
        json_get_str(msg, "url",       url,    sizeof(url));
        json_get_str(msg, "domain",    domain, sizeof(domain));
        ws_create(req_id, url, domain);
        char line[700];
        snprintf(line, sizeof(line), "🔌 WS OPENED  [%s] %s", req_id, url);
        broadcast_to_cli(line);
        write_log(line);
    }
    else if (strcmp(type, "websocket_handshake") == 0) {
        save_to_file(WS_CONNECTIONS_FILE, msg);
        char req_id[64], status[8];
        json_get_str(msg, "requestId", req_id,  sizeof(req_id));
        json_get_str(msg, "status",    status,   sizeof(status));
        char line[200];
        snprintf(line, sizeof(line), "🤝 WS HANDSHAKE [%s] status=%s", req_id, status);
        broadcast_to_cli(line);
    }
    else if (strcmp(type, "websocket") == 0) {
        // Main frame event — save to both files
        save_to_file(WS_FILE,        msg);  // legacy compatibility
        save_to_file(WS_FRAMES_FILE, msg);  // new detailed file

        char req_id[64], direction[8], domain[128], flags[256];
        char payload_preview[160];
        json_get_str(msg, "requestId", req_id,   sizeof(req_id));
        json_get_str(msg, "direction", direction, sizeof(direction));
        json_get_str(msg, "domain",    domain,    sizeof(domain));
        json_get_array_str(msg, "flags", flags,   sizeof(flags));
        json_get_payload_preview(msg, payload_preview, sizeof(payload_preview), 120);

        // Update C-side stats
        WSConn *conn = ws_find(req_id);
        if (conn) {
            if (strcmp(direction, "recv") == 0) conn->frames_recv++;
            else                                conn->frames_sent++;
        }

        // Only broadcast interesting frames to avoid flooding
        int is_interesting = (flags[0] != '\0' && strstr(flags, "HEARTBEAT") == NULL);
        char line[600];
        if (is_interesting) {
            snprintf(line, sizeof(line),
                "📡 WS %-4s [%s] %s %-40s", direction, req_id, flags, payload_preview);
        } else {
            snprintf(line, sizeof(line),
                "📡 WS %-4s [%s] %.80s", direction, req_id, payload_preview);
        }
        broadcast_to_cli(line);

        // Always log interesting frames
        if (is_interesting) write_log(line);
    }
    else if (strcmp(type, "websocket_error") == 0) {
        save_to_file(WS_CONNECTIONS_FILE, msg);
        char req_id[64];
        json_get_str(msg, "requestId", req_id, sizeof(req_id));
        char line[200];
        snprintf(line, sizeof(line), "💥 WS ERROR [%s]", req_id);
        broadcast_to_cli(line); write_log(line);
    }
    else if (strcmp(type, "websocket_closed") == 0) {
        save_to_file(WS_CONNECTIONS_FILE, msg);
        char req_id[64];
        json_get_str(msg, "requestId", req_id, sizeof(req_id));
        WSConn *conn = ws_find(req_id);
        if (conn) {
            conn->closed_at = (long)time(NULL);
            conn->active    = 0;
            char line[400];
            snprintf(line, sizeof(line),
                "🔌 WS CLOSED [%s] recv=%d sent=%d duration=%lds",
                req_id, conn->frames_recv, conn->frames_sent,
                conn->closed_at - conn->created_at);
            broadcast_to_cli(line);
            write_log(line);
        } else {
            char line[200];
            snprintf(line, sizeof(line), "🔌 WS CLOSED [%s]", req_id);
            broadcast_to_cli(line);
        }
    }

    else if (strcmp(type, "dommap") == 0) {
        save_to_file(DOMMAP_FILE, msg);
        char dom[128], url[256];
        json_get_str(msg, "domain", dom, sizeof(dom));
        json_get_str(msg, "url",    url, sizeof(url));
        char line[400];
        snprintf(line, sizeof(line), "🗺️  DOM MAP %s → %s", dom, url);
        broadcast_to_cli(line); write_log(line);
    }
    else if (strcmp(type, "storage") == 0) {
        save_to_file(STORAGE_FILE, msg);
        broadcast_to_cli("💾 STORAGE SAVED → storage.jsonl");
    }
    else if (strcmp(type, "fingerprint") == 0) {
        save_to_file(FINGERPRINT_FILE, msg);
        char dom[128]; json_get_str(msg, "domain", dom, sizeof(dom));
        char line[300];
        snprintf(line, sizeof(line), "🖥️  FINGERPRINT captured @ %s", dom);
        broadcast_to_cli(line); write_log(line);
    }
    else if (strcmp(type, "html") == 0) {
        char path[512];
        snprintf(path, sizeof(path), "%s/html_%ld.json", DATA_DIR, (long)time(NULL));
        save_to_file(path, msg);
        char line[300];
        snprintf(line, sizeof(line), "📄 HTML SAVED → %s", path);
        broadcast_to_cli(line); write_log(line);
    }
    else if (strcmp(type, "screenshot") == 0) {
        char path[512];
        snprintf(path, sizeof(path), "%s/screenshot_%ld.json", DATA_DIR, (long)time(NULL));
        save_to_file(path, msg);
        char line[300];
        snprintf(line, sizeof(line), "📷 SCREENSHOT SAVED → %s", path);
        broadcast_to_cli(line); write_log(line);
    }
    else if (strcmp(type, "debugger_status") == 0) {
        char status[32]; json_get_str(msg, "status", status, sizeof(status));
        char line[128];
        snprintf(line, sizeof(line), "🔬 DEBUGGER %s", status);
        broadcast_to_cli(line); write_log(line);
    }
    else {
        char log[256];
        snprintf(log, sizeof(log), "UNKNOWN type: %.200s", msg);
        write_log(log);
    }
}

// ── Browser message handler ───────────────────────────────────────────────────
void handle_browser_message(const char *msg) {
    if (strstr(msg, "\"command\":\"ping\"")) {
        char r[128];
        snprintf(r, sizeof(r), "{\"command\":\"pong\",\"timestamp\":%ld}", (long)time(NULL));
        send_message(r);
        return;
    }
    if (strstr(msg, "\"command\":\"register\"")) {
        send_message("{\"status\":\"registered\",\"browser\":\"brave\",\"version\":\"3.3\"}");
        broadcast_to_cli("✅ Browser registered (v3.3 WS-enabled)");
        return;
    }
    route_message(msg);
}

// ── Send WS connection list to CLI ────────────────────────────────────────────
void send_ws_list(int fd) {
    char buf[8192];
    int  pos = 0;

    pos += snprintf(buf + pos, sizeof(buf) - pos,
        "\n╔══ WebSocket Connections ══════════════════════╗\n");

    pthread_mutex_lock(&ws_mutex);
    int found = 0;
    for (int i = 0; i < ws_conn_count; i++) {
        WSConn *c = &ws_conns[i];
        if (!c->created_at) continue;
        found++;
        const char *status = c->closed_at ? "CLOSED" : "OPEN  ";
        long duration = (c->closed_at ? c->closed_at : (long)time(NULL)) - c->created_at;
        pos += snprintf(buf + pos, sizeof(buf) - pos,
            "║ [%s] %-6s  recv:%-4d sent:%-4d  %lds\n"
            "║   %s\n"
            "║   id: %s\n",
            status, c->domain, c->frames_recv, c->frames_sent, duration,
            c->url, c->request_id);
        if (pos > (int)sizeof(buf) - 300) break;
    }
    pthread_mutex_unlock(&ws_mutex);

    if (!found) pos += snprintf(buf + pos, sizeof(buf) - pos,
        "║  (no connections tracked yet)\n");

    pos += snprintf(buf + pos, sizeof(buf) - pos,
        "╚═══════════════════════════════════════════════╝\n> ");

    send(fd, buf, pos, MSG_NOSIGNAL);
}

// ── CLI client thread ─────────────────────────────────────────────────────────
void *cli_client_thread(void *arg) {
    int fd = *(int *)arg; free(arg);
    char banner[2048];
    snprintf(banner, sizeof(banner),
        "\n=== SCRAPPER CLI v3.3 (WebSocket Edition) ===\n"
        "  Data dir: %s\n\n"
        "  Navigation:\n"
        "    nav <url>       - Open + track all traffic (including WS)\n"
        "    track           - Track active tab\n"
        "    untrack         - Stop tracking\n\n"
        "  WebSocket:\n"
        "    ws              - List all WS connections\n"
        "    ws_watch        - Watch WS frames live (Ctrl+C to stop)\n"
        "    ws_interesting  - Show only flagged WS frames\n\n"
        "  Capture:\n"
        "    cookies         - Dump cookies\n"
        "    storage         - Dump localStorage/sessionStorage\n"
        "    html            - Get page HTML\n"
        "    screenshot      - Capture screenshot\n"
        "    fingerprint     - Capture browser fingerprint\n"
        "    dommap          - Map DOM\n"
        "    files           - Show data files\n"
        "    quit            - Exit\n> ",
        DATA_DIR);
    send(fd, banner, strlen(banner), MSG_NOSIGNAL);

    char line[512]; int pos = 0;
    while (1) {
        char c; ssize_t n = recv(fd, &c, 1, 0);
        if (n <= 0) break;
        if (c == '\n' || c == '\r') {
            if (pos == 0) { send(fd, "> ", 2, MSG_NOSIGNAL); continue; }
            line[pos] = '\0'; pos = 0;
            char cmd[600] = {0}, reply[512] = {0};

            if (strncmp(line, "nav ", 4) == 0 || strncmp(line, "navigate ", 9) == 0) {
                const char *url = strncmp(line, "nav ", 4) == 0 ? line + 4 : line + 9;
                snprintf(cmd, sizeof(cmd), "{\"command\":\"navigate\",\"url\":\"%s\"}", url);
                send_message(cmd);
                char logbuf[300]; snprintf(logbuf, sizeof(logbuf), "NAV: %s", url);
                write_log(logbuf);
                snprintf(reply, sizeof(reply), "Navigating to %s\n> ", url);

            } else if (strcmp(line, "track") == 0) {
                send_message("{\"command\":\"track\"}");
                write_log("CMD: track");
                snprintf(reply, sizeof(reply), "Tracking active tab\n> ");

            } else if (strcmp(line, "untrack") == 0) {
                send_message("{\"command\":\"untrack\"}");
                write_log("CMD: untrack");
                snprintf(reply, sizeof(reply), "Stopped tracking\n> ");

            } else if (strcmp(line, "ws") == 0) {
                // Show WS connection table
                send_message("{\"command\":\"ws_list\"}");
                send_ws_list(fd);
                continue;

            } else if (strcmp(line, "ws_watch") == 0) {
                snprintf(reply, sizeof(reply),
                    "Watching WS frames (all output will include WS)...\n"
                    "  Filter: tail -f %s\n> ", WS_FRAMES_FILE);

            } else if (strcmp(line, "ws_interesting") == 0) {
                snprintf(reply, sizeof(reply),
                    "Grep for flagged frames:\n"
                    "  grep -v HEARTBEAT %s | grep -v '\"flags\":\\[\\]'\n> ",
                    WS_FRAMES_FILE);

            } else if (strcmp(line, "cookies") == 0) {
                send_message("{\"command\":\"get_cookies\"}");
                write_log("CMD: get_cookies");
                snprintf(reply, sizeof(reply), "Fetching cookies...\n> ");

            } else if (strcmp(line, "storage") == 0) {
                send_message("{\"command\":\"get_storage\"}");
                write_log("CMD: get_storage");
                snprintf(reply, sizeof(reply), "Fetching storage...\n> ");

            } else if (strcmp(line, "html") == 0) {
                send_message("{\"command\":\"get_html\"}");
                write_log("CMD: get_html");
                snprintf(reply, sizeof(reply), "Fetching HTML...\n> ");

            } else if (strcmp(line, "fingerprint") == 0) {
                send_message("{\"command\":\"fingerprint\"}");
                write_log("CMD: fingerprint");
                snprintf(reply, sizeof(reply), "Capturing fingerprint...\n> ");

            } else if (strcmp(line, "dommap") == 0) {
                send_message("{\"command\":\"dommap\"}");
                write_log("CMD: dommap");
                snprintf(reply, sizeof(reply), "Mapping DOM...\n> ");

            } else if (strcmp(line, "screenshot") == 0) {
                send_message("{\"command\":\"screenshot\"}");
                write_log("CMD: screenshot");
                snprintf(reply, sizeof(reply), "Taking screenshot...\n> ");

            } else if (strcmp(line, "files") == 0) {
                char out[1024];
                snprintf(out, sizeof(out),
                    "Data in %s:\n"
                    "  requests.jsonl       - Flagged HTTP requests\n"
                    "  responses.jsonl      - HTTP responses\n"
                    "  bodies.jsonl         - API response bodies\n"
                    "  auth.jsonl           - Auth cookies\n"
                    "  cookies.jsonl        - All cookies\n"
                    "  websockets.jsonl     - WS frames (legacy)\n"
                    "  ws_frames.jsonl      - WS frames with parsed data + flags\n"
                    "  ws_connections.jsonl - WS open/close/handshake events\n"
                    "  fingerprints.jsonl   - Browser fingerprints\n"
                    "  html_*.json          - Saved HTML\n> ", DATA_DIR);
                send(fd, out, strlen(out), MSG_NOSIGNAL); continue;

            } else if (strcmp(line, "quit") == 0 || strcmp(line, "exit") == 0) {
                send(fd, "Bye\n", 4, MSG_NOSIGNAL); break;

            } else {
                char logbuf[300];
                snprintf(logbuf, sizeof(logbuf), "UNKNOWN CMD: %s", line);
                write_log(logbuf);
                snprintf(reply, sizeof(reply), "Unknown command: %s\n> ", line);
            }

            if (strlen(reply)) send(fd, reply, strlen(reply), MSG_NOSIGNAL);
        } else {
            if (pos < (int)sizeof(line) - 1) line[pos++] = c;
        }
    }
    remove_cli_client(fd);
    write_log("CLI client disconnected");
    return NULL;
}

// ── Socket server thread ──────────────────────────────────────────────────────
void *socket_server_thread(void *arg) {
    (void)arg;
    unlink(SOCKET_PATH);
    int server = socket(AF_UNIX, SOCK_STREAM, 0);
    if (server < 0) { write_log("ERROR: socket"); return NULL; }
    struct sockaddr_un addr = {0};
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCKET_PATH, sizeof(addr.sun_path) - 1);
    if (bind(server, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        write_log("ERROR: bind"); close(server); return NULL;
    }
    listen(server, MAX_CLIENTS);
    write_log("Socket ready: " SOCKET_PATH);
    while (1) {
        int client = accept(server, NULL, NULL);
        if (client < 0) continue;
        pthread_mutex_lock(&cli_mutex);
        if (cli_count < MAX_CLIENTS) {
            cli_clients[cli_count++] = client;
            pthread_mutex_unlock(&cli_mutex);
            write_log("CLI connected");
            pthread_t t; int *fdp = malloc(sizeof(int)); *fdp = client;
            pthread_create(&t, NULL, cli_client_thread, fdp);
            pthread_detach(t);
        } else {
            pthread_mutex_unlock(&cli_mutex);
            send(client, "Server full\n", 12, 0); close(client);
        }
    }
    return NULL;
}

// ── Main ──────────────────────────────────────────────────────────────────────
int main(void) {
    init_paths();

    char logs_dir[512];
    snprintf(logs_dir, sizeof(logs_dir), "%s/logs", BASE_DIR);
    mkdir_p(logs_dir);
    mkdir_p(DATA_DIR);

    FILE *f = fopen(LOG_FILE, "w");
    if (f) {
        fprintf(f, "=== SCRAPPER HOST v3.3 ===\nPID: %d\nBase: %s\n",
                getpid(), BASE_DIR);
        fclose(f);
    }

    setbuf(stdin,  NULL);
    setbuf(stdout, NULL);
    setbuf(stderr, NULL);

    write_log("Starting v3.3 — WebSocket deep capture enabled");
    fprintf(stderr, "🟢 SCRAPPER v3.3 PID %d — base: %s\n"
                    "   New: ws_frames.jsonl + ws_connections.jsonl\n",
                    getpid(), BASE_DIR);

    pthread_t t;
    pthread_create(&t, NULL, socket_server_thread, NULL);
    pthread_detach(t);

    while (1) {
        char *msg = receive_message();
        if (!msg) { if (feof(stdin)) break; continue; }
        handle_browser_message(msg);
        free(msg);
    }

    unlink(SOCKET_PATH);
    write_log("Exiting");
    return 0;
}