#!/usr/bin/env python3
"""
babelfish_query_renamer.py
==========================
Lists, saves and renames AEP Query Service /query-templates owned by
MY_USER_ID. Authenticates with a Bearer token pasted from Postman.

These are the named/saved queries you see in the AEP Query Editor's
"Templates" panel -- NOT the execution history (which lives at /queries).

VDI-friendly: stdlib only, no pip install required.

First-time setup:
    1. Copy `config.example.json` to `config.json` (next to this script).
    2. Fill in client_id / client_secret / org_id (and optionally my_user_ids).
    3. python babelfish_query_renamer.py

`config.json` is gitignored -- never commit it. It contains the bearer token
and/or client_secret, which are credentials. A `sql\\` folder (also gitignored)
is created next to the script for the SQL exports, with one subfolder per
tenant and per sandbox.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

# ============================================================================
# CONFIG
# ----------------------------------------------------------------------------
# All tunable values live in `config.json` next to this script. Required keys:
#   bearer_token   -- pasted access token (~24h); only used as a fallback for
#                     quick local testing. Leave "" in normal operation.
#   client_id      -- Adobe IMS client ID
#   client_secret  -- IMS client_credentials secret. PREFERRED -- mints a fresh
#                     token on every run, so expiry/scope/credential changes
#                     are picked up automatically.
#   org_id         -- Adobe org ID (e.g. "ABC@AdobeOrg")
#   oauth_url      -- IMS token endpoint
#   scopes         -- IMS scopes (comma-separated)
#   sandbox        -- "all" or a specific sandbox name
#   sandbox_names  -- fallback list when sandbox-management API is denied
#   my_user_ids    -- user IDs you own across orgs (Valtech, VDI, etc.)
# client_secret wins over bearer_token when both are set.
# ============================================================================

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def _load_config() -> dict:
    """Read config.json next to this script. Hard-fail with a clear message
    if missing, malformed, or missing required keys."""
    if not CONFIG_PATH.exists():
        print(f"[ERROR] Config file not found: {CONFIG_PATH}", file=sys.stderr)
        print("[ERROR] Required JSON keys: bearer_token, client_id, "
              "client_secret, org_id, oauth_url, scopes, sandbox, "
              "sandbox_names, my_user_ids", file=sys.stderr)
        sys.exit(1)
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[ERROR] {CONFIG_PATH} is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    required = ["client_id", "org_id", "oauth_url", "scopes",
                "sandbox", "sandbox_names", "my_user_ids"]
    missing = [k for k in required if k not in cfg]
    if missing:
        print(f"[ERROR] {CONFIG_PATH} is missing required keys: {missing}",
              file=sys.stderr)
        sys.exit(1)
    return cfg


_CFG          = _load_config()
BEARER_TOKEN  = _CFG.get("bearer_token", "")
CLIENT_ID     = _CFG["client_id"]
CLIENT_SECRET = _CFG.get("client_secret", "")
ORG_ID        = _CFG["org_id"]
OAUTH_URL     = _CFG["oauth_url"]
SCOPES        = _CFG["scopes"]
SANDBOX       = _CFG["sandbox"]
SANDBOX_NAMES = list(_CFG["sandbox_names"])
MY_USER_IDS   = list(_CFG["my_user_ids"])

# ============================================================================

# Script identity (shown in the startup banner).
SCRIPT_NAME   = "babelfish_query_renamer"
SCRIPT_VERSION = "0.4.0"
SCRIPT_DATE   = "2026-05-07"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"

TEMPLATES_URL = "https://platform.adobe.io/data/foundation/query/query-templates"
SANDBOX_URL   = "https://platform.adobe.io/data/foundation/sandbox-management/sandboxes"
PAGE_LIMIT    = 50

# Adobe doesn't expose org names via API, so we hard-code a friendly label per
# org_id. Used to namespace sql/<tenant>/<sandbox>/... so two orgs with the
# same sandbox name (e.g. Valtech and Admiral both have 'prod') don't collide.
ORG_LABELS: dict[str, str] = {
    "E71EADC8584130D00A495EBD@AdobeOrg": "valtech",
    # Add more here as discovered, e.g. "<admiral-org-id>@AdobeOrg": "admiral".
}


def tenant_for_org(org_id: str) -> str:
    """Friendly label for an Adobe org_id. Falls back to a short prefix when
    unknown so different unknown orgs still get distinct folders."""
    if org_id in ORG_LABELS:
        return ORG_LABELS[org_id]
    return f"org-{org_id.split('@')[0][:8]}"


TENANT  = tenant_for_org(ORG_ID)
SQL_DIR = Path(__file__).resolve().parent / "sql" / TENANT


# ---- Coloured logging --------------------------------------------------------
# ANSI colour codes; we enable VT processing on Windows so PowerShell/cmd
# render them. Auto-disabled when stdout isn't a terminal (e.g. piped to a file).
_USE_COLOR = sys.stdout.isatty()
if _USE_COLOR and sys.platform == "win32":
    try:
        import ctypes
        _k32 = ctypes.windll.kernel32
        _h = _k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        _mode = ctypes.c_ulong()
        if _k32.GetConsoleMode(_h, ctypes.byref(_mode)):
            _k32.SetConsoleMode(_h, _mode.value | 0x4)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        else:
            _USE_COLOR = False
    except Exception:
        _USE_COLOR = False

_RESET   = "\033[0m"
_TAG_COLORS = {
    "START":  "\033[1;36m",  # bold cyan
    "AUTH":   "\033[34m",    # blue
    "FETCH":  "\033[36m",    # cyan
    "PREP":   "\033[32m",    # green
    "FILTER": "\033[35m",    # magenta
    "PICK":   "\033[33m",    # yellow
    "SAVE":   "\033[92m",    # bright green
    "RENAME": "\033[93m",    # bright yellow
    "ERROR":  "\033[1;31m",  # bold red
    "HINT":   "\033[33m",    # yellow
}


def step(tag: str, msg: str) -> None:
    """Log one line, prefixed with a [TAG] indicating the current step."""
    if _USE_COLOR:
        color = _TAG_COLORS.get(tag, "")
        print(f"{color}[{tag}]{_RESET} {msg}", flush=True)
    else:
        print(f"[{tag}] {msg}", flush=True)


def print_banner() -> None:
    """Print a short header so each run is self-identifying in the log."""
    bar    = "=" * 72
    head   = f"\033[1;36m{bar}\033[0m" if _USE_COLOR else bar
    title  = f"\033[1m{SCRIPT_NAME} v{SCRIPT_VERSION}\033[0m" if _USE_COLOR \
             else f"{SCRIPT_NAME} v{SCRIPT_VERSION}"
    print(head)
    print(f"  {title}   ({SCRIPT_DATE})")
    print(f"  by {SCRIPT_AUTHOR}")
    print( "  Lists, saves, and renames AEP Query Service templates owned by you.")
    print( "  Auto-suggests names from each query's SQL; output goes to sql/<tenant>/<sandbox>/.")
    print(f"  Tenant for this run: {TENANT}    (org_id: {ORG_ID})")
    print(head)


_CACHED_TOKEN: str | None = None


def _mask_secret(s: str, keep: int = 4) -> str:
    """Return a short masked version of a secret -- e.g.
    'p8e-Yc3mHk' -> '********mHk'. Always renders as 8 stars + last `keep`
    chars + length, regardless of original length, so a 1500-char bearer
    token doesn't produce a screen full of asterisks."""
    if not s:
        return "(empty)"
    if len(s) <= keep:
        return "*" * len(s)
    return f"********{s[-keep:]} ({len(s)} chars)"


def fetch_oauth_token() -> str:
    """POST to Adobe IMS to mint a fresh access token via client_credentials."""
    body = urlencode({
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type":    "client_credentials",
        "scope":         SCOPES,
    }).encode("utf-8")
    req = urllib.request.Request(
        OAUTH_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    step("AUTH", f"POST {OAUTH_URL} (client_credentials, "
                  f"client_id={CLIENT_ID}, client_secret={_mask_secret(CLIENT_SECRET)})...")
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")[:300]
        step("ERROR", f"IMS auth failed: HTTP {e.code} {text}")
        sys.exit(1)
    except urllib.error.URLError as e:
        step("ERROR", f"IMS auth network error: {e.reason}")
        sys.exit(1)
    token = payload.get("access_token")
    if not token:
        step("ERROR", f"IMS response had no access_token: {payload}")
        sys.exit(1)
    expires_in = payload.get("expires_in", "?")
    step("AUTH", f"OK - got access token (expires in {expires_in}s).")
    return token


def get_token() -> str:
    """Return a valid bearer token, fetching from IMS if needed (cached per run).

    Prefers minting via OAuth client_credentials so every session picks up
    current permissions/credentials. Only falls back to the pasted
    BEARER_TOKEN when no client_secret is configured."""
    global _CACHED_TOKEN
    if _CACHED_TOKEN is None:
        if CLIENT_SECRET:
            _CACHED_TOKEN = fetch_oauth_token()
        elif BEARER_TOKEN:
            step("AUTH", f"No client_secret configured; using fallback "
                          f"bearer_token={_mask_secret(BEARER_TOKEN)} "
                          f"(may be expired).")
            _CACHED_TOKEN = BEARER_TOKEN
        else:
            step("ERROR", "Neither client_secret nor bearer_token is set in "
                          "config.json - cannot authenticate.")
            sys.exit(1)
    return _CACHED_TOKEN


def auth_headers(sandbox: str | None = None) -> dict:
    """Build the standard request headers. `sandbox` overrides SANDBOX for
    requests that need to target a specific sandbox (e.g. fetching templates
    or renaming a template that lives in sandbox 'dev')."""
    headers = {
        "Authorization":   f"Bearer {get_token()}",
        "x-api-key":       CLIENT_ID,
        "x-gw-ims-org-id": ORG_ID,
        "Accept":          "application/json",
        "Content-Type":    "application/json",
    }
    sb = sandbox if sandbox is not None else SANDBOX
    if sb and sb != "all":
        headers["x-sandbox-name"] = sb
    return headers


def list_sandboxes() -> list[str]:
    """Return the names of every sandbox the token can see."""
    step("FETCH", f"GET {SANDBOX_URL} (listing all sandboxes)...")
    # Sandbox-management endpoint does NOT take x-sandbox-name itself.
    headers = auth_headers(sandbox="")
    headers.pop("x-sandbox-name", None)
    status, text = http_request("GET", SANDBOX_URL, headers)
    if status < 200 or status >= 300:
        step("ERROR", f"Sandbox list failed: HTTP {status} {text[:200]}")
        return []
    body = json.loads(text)
    names = [s.get("name") for s in body.get("sandboxes", []) if s.get("name")]
    step("FETCH", f"  -> sandboxes available: {names}")
    return names


def http_request(method, url, headers, params=None, body=None):
    """Stdlib-only HTTP. Returns (status_code, response_text).

    Never raises on HTTP error codes -- 4xx/5xx are returned as a normal
    (status, text) pair so callers can branch on them.
    """
    if params:
        url = f"{url}?{urlencode(params)}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        # Network/DNS/proxy failures land here. Surface them as a 0 status.
        return 0, f"URLError: {e.reason}"


def suggest_from_sql(sql: str) -> str:
    """Generate a name like '<dataset> - <what it does>' by reading the SQL.

    Heuristic:
      1. Find the primary dataset (FROM / INSERT INTO / UPDATE / CREATE TABLE).
      2. Identify the operation and notable shape (aggregations, GROUP BY,
         WHERE, JOIN, LIMIT) and turn that into a short phrase.
    """
    if not sql or not sql.strip():
        return "(empty query)"

    sql_clean = sql.strip()
    sql_lower = sql_clean.lower()

    # ---- 1. Primary dataset ------------------------------------------------
    table = None
    patterns = [
        r"\binsert\s+(?:overwrite\s+)?into\s+([a-zA-Z_][\w.]*)",
        r"\bupdate\s+([a-zA-Z_][\w.]*)",
        r"\bdelete\s+from\s+([a-zA-Z_][\w.]*)",
        r"\bcreate\s+(?:or\s+replace\s+)?(?:temp\s+|temporary\s+)?"
        r"(?:table|view)\s+(?:if\s+not\s+exists\s+)?([a-zA-Z_][\w.]*)",
        r"\bfrom\s+([a-zA-Z_][\w.]*)",
    ]
    for pat in patterns:
        m = re.search(pat, sql_lower)
        if m:
            table = m.group(1)
            break

    if not table:
        # No FROM / INSERT / etc. -- typically SHOW TABLES, DESCRIBE foo,
        # USE bar, SET x=y. Use the first 1-2 words verbatim so the suggestion
        # actually describes the query, e.g. "show tables" rather than "(show)".
        words = [w.strip(";,()").lower() for w in sql_clean.split() if w.strip(";,()")]
        if not words:
            return "query"
        first = words[0]
        keyword_pairs = {"show", "describe", "desc", "explain", "use", "set",
                         "reset", "analyze", "optimize", "vacuum"}
        if first in keyword_pairs and len(words) >= 2:
            return f"{first} {words[1]}"
        return first

    dataset = table.split(".")[-1]  # strip schema/db prefix

    # ---- 2. Operation + shape ---------------------------------------------
    first_word = sql_lower.split()[0]
    if first_word in ("select", "with"):
        is_count    = bool(re.search(r"\bcount\s*\(", sql_lower))
        is_sum      = bool(re.search(r"\bsum\s*\(", sql_lower))
        is_avg      = bool(re.search(r"\bavg\s*\(", sql_lower))
        is_min_max  = bool(re.search(r"\b(min|max)\s*\(", sql_lower))
        is_distinct = bool(re.search(r"\bselect\s+distinct\b", sql_lower))
        is_star     = bool(re.search(r"\bselect\s+\*", sql_lower))
        has_group   = bool(re.search(r"\bgroup\s+by\b", sql_lower))
        has_where   = bool(re.search(r"\bwhere\b", sql_lower))
        has_join    = bool(re.search(r"\bjoin\b", sql_lower))
        m_limit     = re.search(r"\blimit\s+(\d+)", sql_lower)

        if is_count and has_group:
            verb = "count by group"
        elif is_count:
            verb = "row count"
        elif is_sum:
            verb = "sum"
        elif is_avg:
            verb = "average"
        elif is_min_max:
            verb = "min/max"
        elif is_distinct:
            verb = "distinct values"
        elif is_star:
            verb = "select all"
        else:
            verb = "select columns"

        modifiers = []
        if has_join:
            modifiers.append("with join")
        if has_where:
            modifiers.append("filtered")
        if m_limit:
            modifiers.append(f"top {m_limit.group(1)}")

        description = f"{verb} ({', '.join(modifiers)})" if modifiers else verb
    elif first_word == "insert":
        description = "insert overwrite" if "overwrite" in sql_lower else "insert"
    elif first_word == "update":
        description = "update"
    elif first_word == "delete":
        description = "delete"
    elif first_word == "create":
        if re.search(r"\bcreate\s+(?:or\s+replace\s+)?(?:temp\s+|temporary\s+)?table\b", sql_lower):
            description = "create table"
        elif re.search(r"\bcreate\s+(?:or\s+replace\s+)?view\b", sql_lower):
            description = "create view"
        elif re.search(r"\bcreate\s+(?:or\s+replace\s+)?procedure\b", sql_lower):
            description = "create procedure"
        else:
            description = "create"
    elif first_word == "drop":
        description = "drop"
    else:
        description = first_word

    return f"{dataset} - {description}"


def sanitize_filename(name: str) -> str:
    """Make a string safe to use as a filename on Windows + POSIX."""
    s = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", name)  # forbidden chars
    s = re.sub(r"\s+", "_", s).strip("._ ")
    return s or "untitled"


def save_template_sql(template: dict, dest_dir: Path) -> Path:
    """Write the template's SQL to dest_dir/<sandbox>/<name>.sql with a header."""
    sandbox = template.get("_sandbox", "unknown")
    sandbox_dir = dest_dir / sanitize_filename(sandbox)
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    name    = template.get("name", "") or "untitled"
    tid     = template.get("id", "")
    sql     = template.get("sql", "") or ""
    userid  = template.get("userId", "")
    created = template.get("created", "")
    updated = template.get("updated", "")

    filename = f"{sanitize_filename(name)}.sql"
    path = sandbox_dir / filename
    header = (
        f"-- Template ID : {tid}\n"
        f"-- Sandbox     : {sandbox}\n"
        f"-- Name        : {name}\n"
        f"-- userId      : {userid}\n"
        f"-- created     : {created}\n"
        f"-- updated     : {updated}\n"
        f"-- (saved by babelfish_query_renamer)\n\n"
    )
    path.write_text(header + sql, encoding="utf-8")
    return path


_SQL_FIRST_WORDS = {
    "select", "with", "insert", "update", "delete", "create", "drop", "alter",
    "truncate", "merge", "show", "describe", "desc", "explain", "use", "set",
    "reset", "analyze", "optimize", "vacuum", "begin", "commit", "rollback",
    "start", "grant", "revoke", "copy",
}


def looks_like_valid_sql(sql: str) -> bool:
    """Cheap heuristic: non-empty and starts with a recognised SQL keyword.
    Used to filter the mega-file so an LLM downstream sees only real queries."""
    if not sql or not sql.strip():
        return False
    first = sql.strip().split()[0].lower().rstrip(";,()")
    return first in _SQL_FIRST_WORDS


def write_mega_markdown(templates: list[dict], dest_dir: Path) -> Path:
    """Combine every (syntactically valid) template's SQL into a single
    Markdown file at dest_dir/all_queries_mega_file.md, grouped by sandbox.
    The manifest at the top lists the sandboxes the queries came from."""
    from datetime import datetime
    dest_dir.mkdir(parents=True, exist_ok=True)
    md_path = dest_dir / "all_queries_mega_file.md"

    by_sandbox: dict[str, list[dict]] = {}
    skipped: list[dict] = []
    for t in templates:
        if not looks_like_valid_sql(t.get("sql", "") or ""):
            skipped.append(t)
            continue
        sb = t.get("_sandbox", "?")
        by_sandbox.setdefault(sb, []).append(t)

    lines: list[str] = []
    lines.append(f"# AEP Query Templates - {TENANT}")
    lines.append("")
    lines.append("## Manifest")
    lines.append("")
    lines.append(f"- Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- Tenant: `{TENANT}`")
    lines.append(f"- Org ID: `{ORG_ID}`")
    sb_list = sorted(by_sandbox)
    lines.append(f"- Sandboxes ({len(sb_list)}): "
                 + ", ".join(f"`{s}` ({len(by_sandbox[s])})" for s in sb_list))
    lines.append(f"- Templates included: {sum(len(v) for v in by_sandbox.values())}")
    if skipped:
        lines.append(f"- Skipped (no valid SQL): {len(skipped)}")
    lines.append(f"- Source script: {SCRIPT_NAME} v{SCRIPT_VERSION}")
    lines.append("")

    for sb in sb_list:
        lines.append(f"## Sandbox: `{sb}`")
        lines.append("")
        for t in by_sandbox[sb]:
            name = t.get("name", "") or "(unnamed)"
            tid = t.get("id", "")
            created = t.get("created", "")
            updated = t.get("updated", "")
            sql = (t.get("sql", "") or "").strip()
            lines.append(f"### {name}")
            lines.append("")
            lines.append(f"- ID: `{tid}`")
            lines.append(f"- Created: {created}")
            lines.append(f"- Updated: {updated}")
            lines.append("")
            lines.append("```sql")
            lines.append(sql)
            lines.append("```")
            lines.append("")

    if skipped:
        lines.append("## Skipped (does not look like SQL)")
        lines.append("")
        for t in skipped:
            name = t.get("name", "") or "(unnamed)"
            tid = t.get("id", "")
            sb = t.get("_sandbox", "?")
            preview = ((t.get("sql", "") or "").strip().replace("\n", " "))[:100]
            lines.append(f"- `{sb}` - {name} (`{tid}`): {preview!r}")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def rename_template(template_id: str, new_name: str, sql: str, sandbox: str) -> bool:
    url = f"{TEMPLATES_URL}/{template_id}"
    status, text = http_request("PUT", url, auth_headers(sandbox=sandbox),
                                body={"name": new_name, "sql": sql})
    if status < 200 or status >= 300:
        step("ERROR", f"PUT {url} (sandbox={sandbox}) -> HTTP {status}: {text[:300]}")
        return False
    step("RENAME", f"OK - sandbox '{sandbox}' template {template_id} -> '{new_name}'.")
    return True


def pick_user_id(templates: list[dict]) -> str | None:
    """Show a numbered menu of userIds present in the response.
    Returns the chosen userId, or None if the user picks 'a' (all).
    """
    from collections import Counter
    counts = Counter(t.get("userId", "") for t in templates if t.get("userId"))
    if not counts:
        step("PICK", "No userIds in the response; using no filter.")
        return None

    items = counts.most_common()
    print()
    step("PICK", "Choose a userId to filter by:")
    for i, (uid, n) in enumerate(items, 1):
        print(f"  [{i:>2}] {n:>5} templates    {uid}")
    print( "  [ a]               (no filter -- show every template)")
    print()
    while True:
        try:
            raw = input("  Pick a number (or 'a'): ")
        except EOFError:
            step("ERROR", "stdin closed; cannot pick. Set my_user_ids in config.json.")
            sys.exit(1)
        choice = raw.replace(chr(0xfeff), "").strip().lower()
        if choice == "a":
            return None
        try:
            n = int(choice)
            if 1 <= n <= len(items):
                picked = items[n - 1][0]
                step("PICK", f"Selected: {picked}")
                step("PICK", "Tip: add this to my_user_ids in config.json "
                            "to skip this menu next time.")
                return picked
        except ValueError:
            pass
        print(f"  Invalid choice '{choice}'. Try again.")


def fetch_templates_in_sandbox(sandbox: str) -> list[dict]:
    """Fetch all templates from a single sandbox, tagging each with `_sandbox`."""
    step("FETCH", f"Sandbox '{sandbox}': listing templates...")
    headers = auth_headers(sandbox=sandbox)
    out: list[dict] = []
    start = None
    page = 0
    while True:
        page += 1
        params = {"limit": PAGE_LIMIT, "orderby": "-created"}
        if start:
            params["start"] = start
        status, text = http_request("GET", TEMPLATES_URL, headers, params=params)
        if status == 401:
            step("ERROR", "401 Unauthorized - token expired/invalid.")
            sys.exit(1)
        if status < 200 or status >= 300:
            step("ERROR", f"  page {page}: HTTP {status} {text[:200]}")
            break
        body = json.loads(text)
        batch = body.get("templates", [])
        for t in batch:
            t["_sandbox"] = sandbox
        out.extend(batch)
        step("FETCH", f"  page {page}: got {len(batch)} (sandbox total {len(out)}).")
        next_cursor = (body.get("_page") or {}).get("next")
        if not batch or len(batch) < PAGE_LIMIT or not next_cursor:
            break
        start = next_cursor
    step("FETCH", f"  sandbox '{sandbox}' done - {len(out)} templates.")
    return out


def discover_sandboxes() -> list[str]:
    """Resolve which sandboxes to scan.

    SANDBOX="all" tries the sandbox-management API. If that fails (typically
    HTTP 403 on tokens without management scope), fall back to SANDBOX_NAMES
    so the script still works on minimally-scoped tokens. Hard-fails only when
    both the API call AND the configured fallback list are empty."""
    if SANDBOX != "all":
        return [SANDBOX]
    sandboxes = list_sandboxes()
    if sandboxes:
        step("PREP", f"Using {len(sandboxes)} sandbox(es) from sandbox-management API.")
        return sandboxes
    if SANDBOX_NAMES:
        step("PREP", f"Sandbox-management API returned nothing; using "
                     f"configured SANDBOX_NAMES fallback: {SANDBOX_NAMES}")
        return list(SANDBOX_NAMES)
    step("ERROR", "Sandbox listing returned empty AND SANDBOX_NAMES is empty. "
                  "Either get a token with sandbox-management read scope, or "
                  "fill in sandbox_names in config.json.")
    sys.exit(1)


def prepare_folder_structure(sandboxes: list[str]) -> None:
    """Create sql/<sandbox>/ for every sandbox upfront so the layout is visible
    before we fetch anything."""
    SQL_DIR.mkdir(parents=True, exist_ok=True)
    for sb in sandboxes:
        (SQL_DIR / sanitize_filename(sb)).mkdir(parents=True, exist_ok=True)
    step("PREP", f"Folder structure ready under {SQL_DIR}: {sandboxes}")


def fetch_all_templates(sandboxes: list[str]) -> list[dict]:
    """Fetch templates from each of the given sandboxes."""
    all_templates: list[dict] = []
    for sb in sandboxes:
        all_templates.extend(fetch_templates_in_sandbox(sb))
    step("FETCH", f"Done - {len(all_templates)} templates across {len(sandboxes)} sandbox(es).")
    return all_templates


def confirm_user_id(templates: list[dict]) -> str | None:
    """Always prompt to confirm which user's templates to filter by.

    If exactly one entry from MY_USER_IDS appears in the response, suggest it
    (Enter to accept). Otherwise fall through to the full numbered picker.
    Returns the chosen userId, or None for 'no filter'.
    """
    from collections import Counter
    counts = Counter(t.get("userId", "") for t in templates if t.get("userId"))
    if not counts:
        step("PICK", "No userIds in the response; no filter applied.")
        return None

    matching = [uid for uid in MY_USER_IDS if uid in counts]

    if len(matching) == 1:
        suggested = matching[0]
        n = counts[suggested]
        # Per-sandbox breakdown for the suggested user, so totals like "6"
        # don't surprise you when they span multiple sandboxes.
        per_sb: dict[str, int] = {}
        for t in templates:
            if t.get("userId") == suggested:
                sb = t.get("_sandbox", "?")
                per_sb[sb] = per_sb.get(sb, 0) + 1
        print()
        step("PICK", f"Suggestion: {suggested}")
        sb_w = max((len(s) for s in per_sb), default=8)
        print(f"        {'SANDBOX':<{sb_w}}  COUNT")
        print(f"        {'-' * sb_w}  -----")
        for sb in sorted(per_sb):
            print(f"        {sb:<{sb_w}}  {per_sb[sb]:>5}")
        print(f"        {'TOTAL':<{sb_w}}  {n:>5}")
        try:
            raw = input("  Enter=confirm this is you, 'p'=pick a different one, 'a'=no filter: ")
        except EOFError:
            step("ERROR", "stdin closed; cannot confirm. Set my_user_ids in config.json.")
            sys.exit(1)
        choice = raw.replace(chr(0xfeff), "").strip().lower()
        if choice == "":
            return suggested
        if choice == "a":
            return None
        # 'p' (or anything else) falls through to the picker.
    elif len(matching) > 1:
        step("PICK", f"Multiple MY_USER_IDS appear in the response: {matching}. "
                     "Choose one from the full list below.")
    else:
        step("PICK", "None of MY_USER_IDS appear in this response (likely a "
                     "different tenant). Showing every userId found - pick "
                     "yours, then add it to MY_USER_IDS for next time.")

    return pick_user_id(templates)


def main() -> None:
    print_banner()
    step("START", f"{SCRIPT_NAME} starting.")

    # 1. Resolve sandboxes and lay out sql/<sandbox>/ folders BEFORE fetching,
    #    so the structure is visible (and any listing failure stops us early).
    sandboxes = discover_sandboxes()
    prepare_folder_structure(sandboxes)

    # 2. Fetch templates from each sandbox.
    templates = fetch_all_templates(sandboxes)

    # 3. Confirm whose templates to act on (always asks for confirmation).
    my_user_id = confirm_user_id(templates)
    if my_user_id:
        step("FILTER", f"userId == {my_user_id}")
        mine = [t for t in templates if t.get("userId") == my_user_id]
    else:
        step("FILTER", "userId filter: OFF (every template).")
        mine = list(templates)
    step("FILTER", f"{len(mine)} of {len(templates)} match.")

    # 4. Print the summary table.
    rows = []
    for t in mine:
        tid     = t.get("id", "")
        name    = t.get("name", "") or "(unnamed)"
        sandbox = t.get("_sandbox", "?")
        client  = t.get("clientId", "") or ""
        created = (t.get("created", "") or "")[:19]
        userid  = t.get("userId", "") or ""
        sql     = (t.get("sql", "") or "").strip().replace("\n", " ")[:80]
        rows.append((created, sandbox, client, userid, tid, name, sql))

    print()
    print(f"{'CREATED':<20} {'SANDBOX':<10} {'CLIENT':<25} {'USERID':<55} {'ID':<38} {'NAME':<40} SQL")
    print("-" * 230)
    for created, sandbox, client, userid, tid, name, sql in rows:
        print(f"{created:<20} {sandbox:<10} {client:<25} {userid:<55} {tid:<38} {name:<40} {sql}")
    print(f"\nTotal: {len(rows)} of {len(templates)} templates")

    if not mine:
        return

    # 5. For each template: prompt for rename (if interactive), then save the
    #    .sql to disk. Save happens AFTER any rename so the filename reflects
    #    the new name; templates you skip still get saved with their old name.
    interactive = sys.stdin.isatty()
    if interactive:
        step("RENAME", "Rename mode. Per template: Enter=accept suggestion, "
                       "type a new name, or 's'=skip rename (still saved).")
    else:
        step("RENAME", "stdin is not a TTY; saving with current names without renaming.")

    # AEP rejects duplicate names within a sandbox. Track every existing
    # name (including templates owned by other users) so we can skip PUTs
    # that would trivially clash, instead of round-tripping AEP for the 400.
    def _norm(s: str) -> str:
        s = (s or "").replace(chr(0xfeff), "")
        return re.sub(r"\s+", " ", s).strip().casefold()

    existing_by_sandbox: dict[str, set[str]] = {}
    for tpl in templates:
        sb = tpl.get("_sandbox", "")
        nm = _norm(tpl.get("name", "") or "")
        if nm:
            existing_by_sandbox.setdefault(sb, set()).add(nm)

    for t in mine:
        tid     = t.get("id", "")
        old     = t.get("name", "") or "(unnamed)"
        sql     = t.get("sql", "") or ""
        sandbox = t.get("_sandbox", "")

        if interactive:
            suggest = suggest_from_sql(sql)
            print()
            print(f"  Sandbox     : {sandbox}")
            print(f"  Current name: {old}")
            print(f"  Suggestion  : {suggest}")
            print(f"  SQL preview : {sql.strip()[:120]}")
            try:
                raw = input("  New name (Enter=accept, 's'=skip rename): ")
            except EOFError:
                step("RENAME", "stdin closed; saving remainder with current names.")
                interactive = False
                raw = "s"
            answer = raw.replace(chr(0xfeff), "").strip()
            if answer.lower() == "s":
                step("RENAME", "Skipped rename.")
            else:
                new_name = answer if answer else suggest
                new_norm = _norm(new_name)
                old_norm = _norm(old)
                existing = existing_by_sandbox.get(sandbox, set())
                if new_norm == old_norm:
                    step("RENAME", f"Name unchanged ('{old}'); no PUT needed.")
                elif new_norm in existing:
                    step("RENAME", f"Skip PUT - '{new_name}' already exists in "
                                   f"sandbox '{sandbox}' on a different template.")
                else:
                    step("RENAME", f"PUT '{old}' -> '{new_name}'")
                    if rename_template(tid, new_name, sql, sandbox):
                        t["name"] = new_name  # save below uses the new name
                        existing.discard(old_norm)
                        existing.add(new_norm)
                        existing_by_sandbox[sandbox] = existing

        path = save_template_sql(t, SQL_DIR)
        step("SAVE", f"  -> {path.relative_to(SQL_DIR.parent)}")

    # 6. Combine every valid SQL into one big Markdown file for LLM use.
    md_path = write_mega_markdown(mine, SQL_DIR)
    step("SAVE", f"Mega file: {md_path.relative_to(SQL_DIR.parent)}")


if __name__ == "__main__":
    main()
