#!/usr/bin/env python3
"""Tray and CLI quota viewer for CLIProxyAPI — zero server plugins required.

Queries CLIProxyAPI's built-in Management API (/v0/management/auth-files and the
/v0/management/api-call relay) so the server injects OAuth tokens server-side and
the client never touches provider credentials.

Configuration (~/.config/quota-view.json or environment variables):
  base_url / CPA_BASE_URL              Base URL of CLIProxyAPI (default http://127.0.0.1:8317)
  mgmt_key_cmd / CPA_MANAGEMENT_KEY_CMD Command to print the management key (e.g. 1Password CLI)
  CPA_MANAGEMENT_KEY                   Direct management key via environment variable
  config_path / CPA_CONFIG_PATH        Optional local config.yaml / config.toml fallback
  refresh_sec / CPA_REFRESH_SEC        Auto-refresh interval in seconds (default 300)
  theme                                "system" | "light" | "dark"
  notifications                        true | false (default true)
  notify_thresholds                    List of % thresholds (default [80, 100])
  tray_backend                         "auto" | "statusicon" | "appindicator"
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

CONFIG_DIR = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "quota-view")
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR") or CACHE_DIR
SETTINGS = os.path.join(CONFIG_DIR, "quota-view.json")
SNAPSHOT_CACHE = os.path.join(CACHE_DIR, "snapshot.json")
NOTIFY_STATE = os.path.join(CACHE_DIR, "notify-state.json")
CONTROL_SOCK = os.path.join(RUNTIME_DIR, "quota-view.sock")

DEFAULT_BASE = "http://127.0.0.1:8317"
DEFAULT_REFRESH_SEC = 300  # providers rate-limit usage endpoints; 5 min is plenty
DEFAULT_THRESHOLDS = [80, 100]
USER_AGENT = "cpa-quota-view/2.1"

HEADERS = {
    "claude": {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1.0",
    },
    "codex": {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal",
    },
    "kimi": {"Authorization": "Bearer $TOKEN$"},
    "xai": {
        "Authorization": "Bearer $TOKEN$",
        "x-xai-token-auth": "xai-grok-cli",
        "x-grok-client-version": "0.2.91",
        "accept": "*/*",
        "user-agent": "grok-pager/0.2.91 grok-shell/0.2.91 (macos; aarch64)",
    },
    "antigravity": {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "antigravity/1.21.9 linux/amd64",
    },
    "gemini-cli": {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "google-api-nodejs-client/9.15.1",
        "X-Goog-Api-Client": "gl-node/22.17.0",
        "Client-Metadata": "ideType=IDE_UNSPECIFIED,platform=PLATFORM_UNSPECIFIED,pluginType=GEMINI",
    },
}

GET_URLS = {
    "claude": "https://api.anthropic.com/api/oauth/usage",
    "codex": "https://chatgpt.com/backend-api/wham/usage",
    "kimi": "https://api.kimi.com/coding/v1/usages",
    "xai": "https://cli-chat-proxy.grok.com/v1/billing",
}

ANTIGRAVITY_URLS = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
    "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:fetchAvailableModels",
)
GEMINI_CLI_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"

CLAUDE_LABELS = {
    "five_hour": "5 hours",
    "seven_day": "7 days",
    "seven_day_opus": "7d Opus",
    "seven_day_sonnet": "7d Sonnet",
    "seven_day_oauth_apps": "7d OAuth apps",
    "seven_day_cowork": "7d Cowork",
}

NAMES = {
    "claude": "Claude",
    "codex": "Codex",
    "antigravity": "Antigravity",
    "gemini-cli": "Gemini CLI",
    "xai": "xAI",
    "kimi": "Kimi",
    "vertex": "Vertex AI",
    "proxy": "CLIProxyAPI",
}

SHORT_NAMES = {
    "claude": "CL",
    "codex": "CX",
    "antigravity": "AG",
    "gemini-cli": "GM",
    "xai": "XA",
    "kimi": "KM",
    "vertex": "VX",
    "proxy": "CPA",
}

_key_lock = threading.Lock()
_cached_mgmt_key = None


def _atomic_write_json(path, data, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(d):
    _atomic_write_json(SETTINGS, d, mode=0o600)


def get_base_url():
    cfg = load_settings()
    return (os.environ.get("CPA_BASE_URL") or cfg.get("base_url") or DEFAULT_BASE).rstrip("/")


def get_refresh_sec():
    cfg = load_settings()
    raw = os.environ.get("CPA_REFRESH_SEC") or cfg.get("refresh_sec") or DEFAULT_REFRESH_SEC
    try:
        return max(30, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_REFRESH_SEC


def get_notify_thresholds():
    cfg = load_settings()
    vals = cfg.get("notify_thresholds", DEFAULT_THRESHOLDS)
    if not isinstance(vals, list):
        return list(DEFAULT_THRESHOLDS)
    out = []
    for v in vals:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            pass
    return sorted(set(out)) or list(DEFAULT_THRESHOLDS)


def _read_key_from_config(path):
    try:
        text = open(os.path.expanduser(path), encoding="utf-8").read()
    except OSError:
        return ""
    for pattern in (
        r'^\s*management-secret-key\s*=\s*"([^"]+)"',
        r'^\s*secret-key\s*:\s*["\']?([^\s"\'#]+)["\']?',
    ):
        m = re.search(pattern, text, re.M)
        if m:
            return m.group(1).strip()
    return ""


def mgmt_key(force=False):
    global _cached_mgmt_key
    with _key_lock:
        if _cached_mgmt_key and not force:
            return _cached_mgmt_key

        if os.environ.get("CPA_MANAGEMENT_KEY"):
            _cached_mgmt_key = os.environ["CPA_MANAGEMENT_KEY"].strip()
            return _cached_mgmt_key

        cfg = load_settings()
        cmd = os.environ.get("CPA_MANAGEMENT_KEY_CMD") or cfg.get("mgmt_key_cmd")
        if cmd:
            argv = shlex.split(cmd) if isinstance(cmd, str) else [str(x) for x in cmd]
            if argv:
                argv[0] = os.path.expanduser(argv[0])
            env = {k: v for k, v in os.environ.items() if k != "OP_SERVICE_ACCOUNT_TOKEN"}
            key = subprocess.check_output(argv, env=env, text=True, timeout=20).strip()
            if not key:
                raise RuntimeError("Empty management key returned by mgmt_key_cmd")
            _cached_mgmt_key = key
            return key

        cfg_path = os.environ.get("CPA_CONFIG_PATH") or cfg.get("config_path")
        if cfg_path:
            key = _read_key_from_config(cfg_path)
            if key:
                _cached_mgmt_key = key
                return key

        raise RuntimeError(
            "No management key configured: set CPA_MANAGEMENT_KEY, CPA_MANAGEMENT_KEY_CMD, "
            f"or mgmt_key_cmd in {SETTINGS}"
        )


def _mgmt_request(path, data=None, key=None):
    base = get_base_url()
    token = key or mgmt_key()
    body = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    try:
        req = urllib.request.Request(base + "/v0/management" + path, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp), dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and key is None:
            headers["Authorization"] = "Bearer " + mgmt_key(force=True)
            req = urllib.request.Request(base + "/v0/management" + path, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp), dict(resp.headers.items())
        raise


def mgmt(path, data=None, key=None):
    payload, _ = _mgmt_request(path, data=data, key=key)
    return payload


def api_call(key, auth_index, url, header, method="GET", payload=None):
    req_body = {"authIndex": auth_index, "method": method, "url": url, "header": header}
    if payload is not None:
        req_body["data"] = payload if isinstance(payload, str) else json.dumps(payload)
    r = mgmt("/api-call", req_body, key)
    status = r.get("status_code") or r.get("statusCode") or 0
    body = r.get("body") or r.get("bodyText") or ""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            pass
    if not 200 <= status < 300:
        raise RuntimeError(f"HTTP {status}: {str(body)[:80]}")
    return body


def when(ts, now=None):
    """Reset time -> (relative, absolute local) strings. ts: ISO string or epoch."""
    if ts is None or ts == "":
        return "", ""
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, timezone.utc)
        elif str(ts).isdigit():
            dt = datetime.fromtimestamp(int(ts), timezone.utc)
        else:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return "", ""
    ref = now or datetime.now(timezone.utc)
    secs = (dt - ref).total_seconds()
    if secs <= 0:
        rel = "now"
    else:
        d, h, m = int(secs // 86400), int(secs % 86400 // 3600), int(secs % 3600 // 60)
        rel = "in " + (f"{d}d {h}h" if d else f"{h}h {m:02d}m" if h else f"{m}m")
    local = dt.astimezone()
    return rel, ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[local.weekday()] + local.strftime(" %H:%M")


def codex_account_id(auth_entry):
    if not isinstance(auth_entry, dict):
        return ""
    acc = auth_entry.get("account_id") or auth_entry.get("chatgpt_account_id")
    if not acc and auth_entry.get("id_token"):
        try:
            seg = str(auth_entry["id_token"]).split(".")[1]
            pl = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
            acc = (pl.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id", "")
        except Exception:
            acc = ""
    return acc or ""


def _antigravity_family(model_id, display_name):
    mid = (model_id or "").lower()
    dn = (display_name or "").lower()
    if mid.startswith(("claude-", "gpt-oss-")) or "claude" in dn or "gpt-oss" in dn:
        return "Claude / GPT"
    if "flash" in mid or "flash" in dn:
        return "Gemini Flash"
    if mid.startswith("gemini-") or "gemini" in dn:
        return "Gemini Pro"
    return None


def _parse_iso_ts(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_antigravity_families(body):
    """Return (rows, details_by_family) for Antigravity fetchAvailableModels payloads."""
    models = (body or {}).get("models") or {}
    grouped = {}
    details = {}
    order = ("Claude / GPT", "Gemini Pro", "Gemini Flash")
    for mid, entry in sorted(models.items()):
        if not isinstance(entry, dict):
            continue
        dn = entry.get("displayName") or entry.get("display_name")
        qi = entry.get("quotaInfo") or entry.get("quota_info")
        if not dn or not isinstance(qi, dict):
            continue
        fam = _antigravity_family(mid, dn)
        if not fam:
            continue
        rem = qi.get("remainingFraction")
        if rem is None:
            rem = qi.get("remaining_fraction")
        reset = qi.get("resetTime") or qi.get("reset_time")
        # Antigravity omits remainingFraction when a bucket is exhausted and only sets resetTime.
        if rem is None and reset:
            used = 100.0
        elif rem is not None:
            used = max(0.0, min(100.0, (1.0 - float(rem)) * 100.0))
        else:
            continue
        details.setdefault(fam, []).append({
            "id": mid,
            "name": dn,
            "used_percent": round(used, 1),
            "reset_at": reset,
        })
        cur = grouped.get(fam)
        if cur is None or used > cur[0]:
            grouped[fam] = [used, reset]
        elif used == cur[0] and reset:
            t_old, t_new = _parse_iso_ts(cur[1]), _parse_iso_ts(reset)
            if t_new and (not t_old or t_new < t_old):
                cur[1] = reset
    rows = [(fam, grouped[fam][0], grouped[fam][1], "") for fam in order if fam in grouped]
    return rows, details


def rows_for(provider, body):
    """Normalise a provider payload into [(window label, used_percent or None, reset_ts, note)]."""
    rows = []
    if not isinstance(body, dict):
        return rows

    if provider == "claude":
        for k, label in CLAUDE_LABELS.items():
            w = body.get(k)
            if isinstance(w, dict) and w.get("utilization") is not None:
                rows.append((label, float(w["utilization"]), w.get("resets_at"), ""))
        # Model-scoped weekly limits (e.g. Fable) only appear in `limits`, not as named windows.
        for lim in body.get("limits") or []:
            if not isinstance(lim, dict) or lim.get("kind") != "weekly_scoped":
                continue
            scope = lim.get("scope") or {}
            model = (
                (scope.get("model") or {}).get("display_name")
                or (scope.get("model") or {}).get("id")
                or (scope.get("surface") or "scoped")
            )
            rows.append((
                f"7 days {model}",
                float(lim.get("percent") or 0),
                lim.get("resets_at"),
                "active limit" if lim.get("is_active") else "",
            ))
        ex = body.get("extra_usage") or {}
        if ex.get("is_enabled"):
            rows.append(("Extra credits", ex.get("utilization"), None, f"${ex.get('used_credits', 0)} used"))

    elif provider == "codex":
        def win(rl, prefix=""):
            for kind, w in (("primary_window", "5 hours"), ("secondary_window", "7 days")):
                r = (rl or {}).get(kind)
                if isinstance(r, dict):
                    secs = r.get("limit_window_seconds") or 0
                    lab = "7 days" if secs >= 604800 else "5 hours" if secs else w
                    rows.append((prefix + lab, float(r.get("used_percent") or 0), r.get("reset_at"), ""))

        win(body.get("rate_limit"))
        for extra in body.get("additional_rate_limits") or []:
            short = re.sub(r"(?i)^gpt-?[\d.]*-?codex-?", "", extra.get("limit_name") or "extra")
            win(extra.get("rate_limit"), short + " ")
        cr = body.get("credits") or {}
        if cr.get("has_credits"):
            rows.append(("Credits", None, None, f"balance {cr.get('balance')}"))

    elif provider == "antigravity":
        rows, _ = parse_antigravity_families(body)

    elif provider == "gemini-cli":
        buckets = body.get("buckets") or []
        grouped = {}
        for b in buckets:
            if not isinstance(b, dict):
                continue
            mid = b.get("modelId") or b.get("model_id") or ""
            rem = b.get("remainingFraction")
            if rem is None:
                rem = b.get("remaining_fraction")
            if not mid or rem is None:
                continue
            fam = "Gemini Flash" if "flash" in mid.lower() else "Gemini Pro" if "pro" in mid.lower() else mid
            used = max(0.0, min(100.0, (1.0 - float(rem)) * 100.0))
            reset = b.get("resetTime") or b.get("reset_time")
            cur = grouped.get(fam)
            if cur is None or used > cur[0]:
                grouped[fam] = [used, reset]
        for fam in sorted(grouped):
            rows.append((fam, grouped[fam][0], grouped[fam][1], ""))

    elif provider == "xai":
        c = body.get("config") or {}
        used = (c.get("used") or {}).get("val", 0)
        lim = (c.get("monthlyLimit") or {}).get("val", 0)
        pct = 100.0 * used / lim if lim else None
        rows.append(("Monthly", pct, c.get("billingPeriodEnd"), f"{used} / {lim}" if lim else "no monthly limit"))

    elif provider == "kimi":
        items = body.get("usages") or body.get("data") or ([body] if isinstance(body, dict) else [])
        for u in items:
            if not isinstance(u, dict):
                continue
            lim, used = u.get("limit"), u.get("used")
            pct = 100.0 * used / lim if lim else None
            rows.append((
                u.get("name") or "Usage",
                pct,
                u.get("reset_at") or u.get("resetAt"),
                f"{used} / {lim}" if lim else "",
            ))

    return rows


def passive_claude_rows(auth_entry):
    """Fallback to passive Anthropic rate-limit headers captured by CLIProxyAPI."""
    sig = ((auth_entry.get("quota") or {}).get("signals")) or {}
    rows = []
    for prefix, label in (("5h", "5 hours"), ("7d", "7 days")):
        u = sig.get(f"Anthropic-Ratelimit-Unified-{prefix}-Utilization")
        r = sig.get(f"Anthropic-Ratelimit-Unified-{prefix}-Reset")
        if u is not None:
            try:
                val = float(u)
                pct = val * 100.0 if val <= 1.0 else val
                rows.append((label, pct, r, "cached"))
            except ValueError:
                pass
    return rows


def parse_cooldowns(auth_entry):
    """Extract normalized active cooldowns from /v0/management/auth-files entries."""
    out = []
    for c in auth_entry.get("cooldowns") or []:
        if isinstance(c, str) and c.strip():
            out.append({"model": c.strip(), "until": None, "reason": ""})
        elif isinstance(c, dict):
            model = c.get("model") or c.get("model_id") or c.get("scope") or "account"
            until = c.get("until") or c.get("expires_at") or c.get("reset_at") or c.get("retry_after")
            reason = c.get("reason") or c.get("message") or ""
            out.append({"model": str(model), "until": until, "reason": str(reason)})
    return out


def recent_request_totals(auth_entry):
    buckets = auth_entry.get("recent_requests") or []
    ok = sum(int(b.get("success") or 0) for b in buckets if isinstance(b, dict))
    fail = sum(int(b.get("failed") or 0) for b in buckets if isinstance(b, dict))
    return ok, fail


def recent_activity_note(auth_entry):
    ok, fail = recent_request_totals(auth_entry)
    if ok or fail:
        return f"{ok} ok, {fail} err" if fail else f"{ok} reqs recently"
    return "no quota window"


def _fetch_one(key, f):
    provider = (f.get("provider") or f.get("type") or "").lower()
    provider = {"anthropic": "claude", "gemini": "gemini-cli"}.get(provider, provider)
    name = f.get("name", "")
    label = f.get("email") or f.get("label") or name
    if provider == "vertex":  # SA email is noise; the project id is what you recognise
        label = f.get("project_id") or re.sub(r"^vertex-|\.json$", "", name)

    status = "disabled" if f.get("disabled") else ("unavailable" if f.get("unavailable") else (f.get("status") or "active"))
    status_msg = str(f.get("status_message") or "").strip()
    cooldowns = parse_cooldowns(f)
    req_ok, req_fail = recent_request_totals(f)

    acct = {
        "provider": provider,
        "label": label,
        "plan": "",
        "status": status,
        "status_message": status_msg,
        "cooldowns": cooldowns,
        "recent_ok": req_ok,
        "recent_fail": req_fail,
        "rows": [],
        "row_details": {},
        "error": "",
        "disabled": bool(f.get("disabled")),
    }

    if acct["disabled"]:
        acct["rows"] = [("Status", None, None, "disabled")]
        return acct

    if status not in ("active", "ok", "") and status_msg:
        acct["error"] = f"{status}: {status_msg}"[:90]

    auth_index = f.get("auth_index") or f.get("authIndex")
    if provider == "vertex":
        acct["rows"] = [("Pay as you go", None, None, recent_activity_note(f))]
        return acct

    if not auth_index:
        acct["rows"] = [("Status", None, None, status_msg or status)]
        return acct

    if provider == "antigravity":
        payload = {"project": f["project_id"]} if f.get("project_id") else {}
        last_err = None
        for url in ANTIGRAVITY_URLS:
            try:
                body = api_call(key, auth_index, url, HEADERS["antigravity"], method="POST", payload=payload)
                rows, details = parse_antigravity_families(body)
                if rows:
                    acct["rows"] = rows
                    acct["row_details"] = details
                    return acct
            except Exception as e:
                last_err = e
        if last_err:
            acct["error"] = str(last_err)[:90]
        return acct

    if provider == "gemini-cli":
        payload = {"project": f["project_id"]} if f.get("project_id") else {}
        try:
            body = api_call(key, auth_index, GEMINI_CLI_URL, HEADERS["gemini-cli"], method="POST", payload=payload)
            acct["rows"] = rows_for("gemini-cli", body)
        except Exception as e:
            acct["error"] = str(e)[:90]
        return acct

    if provider not in GET_URLS:
        acct["rows"] = [("Status", None, None, status_msg or status)]
        return acct

    hdr = dict(HEADERS[provider])
    if provider == "codex":
        acc = codex_account_id(f)
        if acc:
            hdr["Chatgpt-Account-Id"] = acc

    try:
        body = api_call(key, auth_index, GET_URLS[provider], hdr)
        acct["rows"] = rows_for(provider, body)
        acct["plan"] = str(body.get("plan_type") or "") if isinstance(body, dict) else ""
        if provider == "codex":  # manual "full reset" credits; only Codex offers these
            try:
                cr = api_call(
                    key,
                    auth_index,
                    "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits",
                    hdr,
                )
                avail = [c for c in cr.get("credits", []) if c.get("status") == "available"]
                acct["resets"] = len(avail)
                exp = min((c.get("expires_at") for c in avail if c.get("expires_at")), default=None)
                acct["resets_expire"] = when(exp)[0] if exp else ""
            except Exception:
                pass
    except Exception as e:
        if provider == "claude":
            fallback = passive_claude_rows(f)
            if fallback:
                acct["rows"] = fallback
                return acct
        acct["error"] = str(e)[:90]
    return acct


def fetch_snapshot():
    """Fetch live accounts and metadata from CLIProxyAPI, saving to disk cache."""
    key = mgmt_key()
    payload, resp_headers = _mgmt_request("/auth-files", key=key)
    files = payload.get("files", [])
    server_version = ""
    for hk, hv in resp_headers.items():
        if hk.lower() == "x-cpa-version":
            server_version = hv.strip()
            break
    if not files:
        accounts = []
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(files))) as pool:
            accounts = list(pool.map(lambda f: _fetch_one(key, f), files))
    snap = build_json_snapshot(accounts, server_version=server_version)
    try:
        _atomic_write_json(SNAPSHOT_CACHE, snap, mode=0o600)
    except OSError:
        pass
    return accounts, snap


def fetch_all():
    accounts, _ = fetch_snapshot()
    return accounts


def load_cached_snapshot(max_age=None):
    """Load cached sanitized snapshot if present, matching base_url and age limit."""
    try:
        with open(SNAPSHOT_CACHE, encoding="utf-8") as fh:
            snap = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(snap, dict) or snap.get("base_url") != get_base_url():
        return None
    if max_age is not None:
        fetched = _parse_iso_ts(snap.get("fetched_at"))
        if not fetched:
            return None
        age = (datetime.now(timezone.utc) - fetched).total_seconds()
        if age > max_age:
            return None
    return snap


def accounts_from_snapshot(snap):
    """Convert a cached JSON snapshot back into internal account dicts for rendering."""
    out = []
    for a in (snap or {}).get("accounts") or []:
        rows = []
        row_details = {}
        for w in a.get("windows") or []:
            rows.append((w.get("label", ""), w.get("used_percent"), w.get("reset_at"), w.get("note", "")))
            if w.get("models"):
                row_details[w.get("label", "")] = w["models"]
        item = {
            "provider": a.get("provider", "proxy"),
            "label": a.get("label", ""),
            "plan": a.get("plan", ""),
            "status": a.get("status", "active"),
            "status_message": a.get("status_message", ""),
            "cooldowns": a.get("cooldowns") or [],
            "recent_ok": a.get("recent_requests", 0),
            "recent_fail": a.get("recent_errors", 0),
            "rows": rows,
            "row_details": row_details,
            "error": a.get("error", ""),
            "disabled": bool(a.get("disabled")),
        }
        if "resets" in a:
            item["resets"] = a["resets"]
            item["resets_expire"] = a.get("resets_expire", "")
        out.append(item)
    return out


def build_json_snapshot(accounts, server_version=""):
    """Build a sanitized, machine-readable JSON snapshot (no tokens or raw bodies)."""
    worst_overall = max((r[1] for a in accounts for r in a["rows"] if r[1] is not None), default=None)
    total_ok = sum(int(a.get("recent_ok") or 0) for a in accounts)
    total_fail = sum(int(a.get("recent_fail") or 0) for a in accounts)
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "base_url": get_base_url(),
        "server_version": server_version,
        "summary": {
            "accounts": len(accounts),
            "errors": sum(1 for a in accounts if a.get("error")),
            "disabled": sum(1 for a in accounts if a.get("disabled")),
            "cooldowns": sum(len(a.get("cooldowns") or []) for a in accounts),
            "recent_requests": total_ok,
            "recent_errors": total_fail,
            "highest_used_percent": round(worst_overall, 1) if worst_overall is not None else None,
        },
        "accounts": [
            {
                "provider": a["provider"],
                "label": a["label"],
                "status": a.get("status", "active"),
                "status_message": a.get("status_message", ""),
                "plan": a.get("plan", ""),
                "disabled": bool(a.get("disabled")),
                "error": a.get("error", ""),
                "cooldowns": a.get("cooldowns") or [],
                "recent_requests": int(a.get("recent_ok") or 0),
                "recent_errors": int(a.get("recent_fail") or 0),
                **({"resets": a["resets"], "resets_expire": a.get("resets_expire", "")} if "resets" in a else {}),
                "windows": [
                    {
                        "label": label,
                        "used_percent": round(pct, 1) if pct is not None else None,
                        "remaining_percent": round(100.0 - pct, 1) if pct is not None else None,
                        "reset_at": reset,
                        "reset_in": when(reset)[0] if reset else "",
                        "note": note,
                        **({"models": a["row_details"][label]} if (a.get("row_details") or {}).get(label) else {}),
                    }
                    for label, pct, reset, note in a["rows"]
                ],
            }
            for a in accounts
        ],
    }


def filter_accounts(accounts, providers=None):
    if not providers:
        return accounts
    wanted = {p.strip().lower() for p in providers if p.strip()}
    if not wanted:
        return accounts
    aliases = {"gemini": "gemini-cli", "anthropic": "claude"}
    wanted = {aliases.get(w, w) for w in wanted}
    return [a for a in accounts if a.get("provider", "").lower() in wanted]


def compute_notifications(prev_state, accounts, thresholds=None):
    """Compare current accounts against prev_state and return (events, new_state).

    Each event is (urgency, title, body).
    """
    th = sorted(thresholds or DEFAULT_THRESHOLDS)
    prev_win = (prev_state or {}).get("windows") or {}
    prev_err = (prev_state or {}).get("errors") or {}
    new_win = {}
    new_err = {}
    events = []

    for a in accounts:
        if a.get("disabled") or a.get("provider") == "proxy":
            continue
        acct_key = f"{a['provider']}:{a['label']}"
        pname = NAMES.get(a["provider"], a["provider"])
        cur_err = a.get("error") or ""
        new_err[acct_key] = cur_err
        if cur_err and cur_err != prev_err.get(acct_key, ""):
            events.append(("normal", f"{pname} quota error", f"{a['label']}: {cur_err}"))

        for label, pct, reset, _note in a.get("rows") or []:
            if pct is None:
                continue
            wkey = f"{acct_key}:{label}"
            new_win[wkey] = round(pct, 1)
            old = prev_win.get(wkey)
            if old is None:
                continue
            rel = when(reset)[0]
            suffix = f" (resets {rel})" if rel else ""
            crossed = [t for t in th if old < t <= pct]
            if crossed:
                top = max(crossed)
                urg = "critical" if top >= 100 else "normal"
                events.append((urg, f"{pname} at {pct:.0f}%", f"{a['label']} · {label}{suffix}"))
            elif old >= 100 and pct < 50:
                events.append(("low", f"{pname} quota reset", f"{a['label']} · {label} is now {pct:.0f}%"))

    return events, {"windows": new_win, "errors": new_err}


def load_notify_state():
    try:
        with open(NOTIFY_STATE, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_notify_state(state):
    try:
        _atomic_write_json(NOTIFY_STATE, state, mode=0o600)
    except OSError:
        pass


def send_desktop_notification(urgency, title, body):
    notify_bin = shutil.which("notify-send")
    if not notify_bin:
        return
    try:
        subprocess.Popen(
            [notify_bin, "-a", "AI Quota", "-u", urgency, "-i", "utilities-system-monitor-symbolic", title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def format_short(accounts):
    per_prov = {}
    for a in accounts:
        vals = [r[1] for r in a["rows"] if r[1] is not None]
        if vals:
            per_prov[a["provider"]] = max(per_prov.get(a["provider"], 0.0), max(vals))
    if not per_prov:
        return "CPA: ok"
    return " ".join(f"{SHORT_NAMES.get(p, p.upper())}:{v:.0f}%" for p, v in per_prov.items())


def format_waybar(accounts):
    text = format_short(accounts)
    worst = max((r[1] for a in accounts for r in a["rows"] if r[1] is not None), default=0.0)
    cls = "hot" if worst >= 80 else "warn" if worst >= 50 else "ok"
    lines = []
    for a in accounts:
        head = f"{NAMES.get(a['provider'], a['provider'])} ({a['label']})"
        if a.get("error"):
            lines.append(f"{head}: {a['error']}")
            continue
        parts = []
        for label, pct, reset, note in a["rows"]:
            p = f"{pct:.0f}%" if pct is not None else "–"
            tail = when(reset)[0] or note
            parts.append(f"{label} {p}" + (f" ({tail})" if tail else ""))
        lines.append(f"{head}: " + ", ".join(parts) if parts else head)
    return {"text": text, "tooltip": "\n".join(lines), "class": cls}


def format_table(accounts):
    lines = []
    for a in accounts:
        extra = f" [{a['status']}]" if a.get("status") not in ("active", "ok", "") else ""
        lines.append(f"{a['provider']:11} {a['label']:32} {a['plan']:6}{extra} {a['error']}".rstrip())
        for c in a.get("cooldowns") or []:
            rel = when(c.get("until"))[0]
            lines.append(f"   Cooldown         {c.get('model', 'account'):16} {rel:10} {c.get('reason', '')}".rstrip())
        for label, pct, reset, note in a["rows"]:
            pct_str = f"{pct:3.0f}%" if pct is not None else "  – "
            lines.append(f"   {label:<16} {pct_str}  {when(reset)[0]:10} {note}".rstrip())
    return "\n".join(lines)


def send_control_command(cmd):
    """Send a command to an already-running tray instance; return True if delivered."""
    if not os.path.exists(CONTROL_SOCK):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.5)
            s.connect(CONTROL_SOCK)
            s.sendall((cmd.strip() + "\n").encode("utf-8"))
            return True
    except OSError:
        try:
            os.unlink(CONTROL_SOCK)
        except OSError:
            pass
        return False


def _start_control_server(on_cmd):
    os.makedirs(os.path.dirname(CONTROL_SOCK), exist_ok=True)
    try:
        os.unlink(CONTROL_SOCK)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(CONTROL_SOCK)
    os.chmod(CONTROL_SOCK, 0o600)
    srv.listen(4)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            with conn:
                try:
                    data = conn.recv(256).decode("utf-8", "replace").strip()
                    if data:
                        on_cmd(data)
                except OSError:
                    pass

    threading.Thread(target=loop, daemon=True).start()
    return srv


def run_gui(start_panel=False, pin=False):
    if not pin and send_control_command("show" if start_panel else "toggle"):
        return

    import cairo
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gio, GLib, Gtk

    app_indicator_mod = None
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as app_indicator_mod
    except (ValueError, ImportError):
        app_indicator_mod = None

    os.makedirs(CACHE_DIR, exist_ok=True)
    colors = {"ok": (0.18, 0.64, 0.31), "warn": (0.75, 0.53, 0.0), "hot": (0.81, 0.13, 0.18)}

    def level(pct):
        return "hot" if pct >= 80 else "warn" if pct >= 50 else "ok"

    def pill(cr, x, y, w, h):
        r = h / 2
        cr.new_sub_path()
        cr.arc(x + r, y + r, r, 1.5708, 4.7124)
        cr.arc(x + w - r, y + r, r, -1.5708, 1.5708)
        cr.close_path()

    def draw_bar(cr, x, y, w, h, pct):
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.25)
        pill(cr, x, y, w, h)
        cr.fill()
        if pct is not None:
            cr.set_source_rgb(*colors[level(pct)])
            pill(cr, x, y, max(h, w * min(pct, 100) / 100), h)
            cr.fill()

    def tray_icon(accounts):
        """The icon *is* the data: one vertical bar per provider, its worst window."""
        worst = {}
        for a in accounts:
            vals = [r[1] for r in a["rows"] if r[1] is not None]
            if vals:
                worst[a["provider"]] = max(worst.get(a["provider"], 0), max(vals))
        provs = list(worst) or ["proxy"]
        bw, gap, h = 5, 3, 22
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, bw * len(provs) + gap * (len(provs) - 1), h)
        cr = cairo.Context(surf)
        for i, p in enumerate(provs):
            x = i * (bw + gap)
            pct = worst.get(p)
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.35)
            cr.rectangle(x, 0, bw, h)
            cr.fill()
            if pct is not None:
                fill = max(1, h * min(pct, 100) / 100)
                cr.set_source_rgb(*colors[level(pct)])
                cr.rectangle(x, h - fill, bw, fill)
                cr.fill()
        path = os.path.join(CACHE_DIR, "tray.png")
        surf.write_to_png(path)
        return path

    class Theme:
        """system | light | dark. System follows GNOME's color-scheme live."""

        def __init__(self):
            self.mode = load_settings().get("theme", "system")
            try:
                self.gnome = Gio.Settings.new("org.gnome.desktop.interface")
                self.gnome.connect("changed::color-scheme", lambda *_: self.apply())
            except Exception:
                self.gnome = None
            self.apply()

        def set(self, mode):
            self.mode = mode
            save_settings({**load_settings(), "theme": mode})
            self.apply()

        def apply(self):
            sys_dark = self.gnome is not None and self.gnome.get_string("color-scheme") == "prefer-dark"
            dark = self.mode == "dark" or (self.mode == "system" and sys_dark)
            Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", dark)

    def open_dashboard(*_):
        Gio.AppInfo.launch_default_for_uri(get_base_url() + "/management.html", None)

    def open_quota_page(*_):
        Gio.AppInfo.launch_default_for_uri(get_base_url() + "/management.html#/quota", None)

    def copy_key(*_):
        try:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(mgmt_key(), -1)
        except Exception:
            pass

    class Panel(Gtk.Window):
        CSS = b"""
        .qv { border-radius: 6px; border: 1px solid alpha(@theme_fg_color, 0.18); }
        .qv-acct { font-weight: 600; }
        .qv-meta { opacity: 0.65; font-size: 0.9em; }
        .qv-warn { color: #d97706; font-size: 0.88em; }
        .qv-pct { font-weight: 500; font-size: 0.9em; }
        .qv-hair { background-color: alpha(@theme_fg_color, 0.12); min-height: 1px; }
        .qv-badge { border-radius: 9px; padding: 0 6px 0 4px; font-size: 0.85em; font-weight: 600;
                    background-color: alpha(@theme_fg_color, 0.10); }
        """
        W = 385

        def __init__(self, tray):
            super().__init__(title="AI quota")
            self.tray = tray
            self.set_decorated(False)
            self.set_resizable(False)
            self.set_keep_above(True)
            self.set_skip_taskbar_hint(True)
            self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
            self.set_default_size(self.W, -1)
            if not pin:
                self.connect("focus-out-event", lambda *a: (self.hide(), False)[1])
            self.connect(
                "key-press-event",
                lambda w, e: (self.hide(), True)[1] if e.keyval == Gdk.KEY_Escape else False,
            )
            self.connect("delete-event", lambda w, e: (w.hide(), True)[1])
            prov = Gtk.CssProvider()
            prov.load_from_data(self.CSS)
            Gtk.StyleContext.add_provider_for_screen(
                self.get_screen(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            self.get_style_context().add_class("qv")
            self.outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            self.outer.set_property("margin", 10)
            self.add(self.outer)
            self.connect("map", self._place)
            self.cols = [Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL) for _ in range(2)]

        def _place(self, *_):
            w = self.get_allocated_width() or self.W
            mon = Gdk.Display.get_default().get_primary_monitor() or Gdk.Display.get_default().get_monitor(0)
            geo = mon.get_workarea()
            x = geo.x + geo.width - w - 8
            if self.tray.status_icon is not None:
                ok, _screen, area, _ = self.tray.status_icon.get_geometry()
                if ok and area.width:
                    x = area.x + area.width // 2 - w // 2
            self.move(max(geo.x, min(x, geo.x + geo.width - w - 4)), geo.y + 6)

        def render(self):
            for c in self.outer.get_children():
                self.outer.remove(c)
            for i, a in enumerate(self.tray.accounts):
                if i:
                    self.outer.pack_start(self._hair(), False, False, 7)
                self.outer.pack_start(self._account(a), False, False, 0)
            foot = Gtk.Box(spacing=6)
            host = re.sub(r"^https?://", "", get_base_url())
            reqs = sum(int(a.get("recent_ok") or 0) for a in self.tray.accounts)
            req_part = f" · {reqs} reqs" if reqs else ""
            status_txt = (
                f"Updated {self.tray.updated}{req_part} · {host}"
                if self.tray.updated
                else f"Loading · {host}…"
            )
            upd = Gtk.Label(label=status_txt, xalign=0, ellipsize=3)
            upd.get_style_context().add_class("qv-meta")
            foot.pack_start(upd, True, True, 0)
            for icon, tip, cb in (
                ("view-refresh-symbolic", "Refresh", self.tray.refresh),
                ("utilities-system-monitor-symbolic", "Open CPA dashboard", open_dashboard),
            ):
                btn = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
                btn.set_relief(Gtk.ReliefStyle.NONE)
                btn.set_tooltip_text(tip)
                btn.connect("clicked", cb)
                foot.pack_end(btn, False, False, 0)
            self.outer.pack_start(self._hair(), False, False, 7)
            self.outer.pack_start(foot, False, False, 0)
            self.outer.show_all()

        def _hair(self):
            h = Gtk.Box()
            h.get_style_context().add_class("qv-hair")
            return h

        def _account(self, a):
            g = Gtk.Grid(column_spacing=10, row_spacing=5)
            icon_key = "gemini" if a["provider"] == "gemini-cli" else a["provider"]
            theme = Gtk.IconTheme.get_default()
            icon_name = f"qv-{icon_key}-symbolic" if theme.has_icon(f"qv-{icon_key}-symbolic") else "qv-proxy-symbolic"
            logo = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
            logo.set_tooltip_text(NAMES.get(a["provider"], a["provider"]))
            g.attach(logo, 0, 0, 1, 1)
            acct = Gtk.Label(label=a["label"], xalign=0, ellipsize=3)
            acct.get_style_context().add_class("qv-acct")
            head = Gtk.Box(spacing=6)
            head.pack_start(acct, False, False, 0)
            if a.get("resets") is not None:
                badge = Gtk.Box(spacing=3, valign=Gtk.Align.CENTER)
                badge.get_style_context().add_class("qv-badge")
                badge.pack_start(Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.MENU), False, False, 0)
                badge.pack_start(Gtk.Label(label=str(a["resets"])), False, False, 0)
                tip = f"{a['resets']} manual reset{'s' if a['resets'] != 1 else ''} available"
                badge.set_tooltip_text(tip + (f" · first expires {a['resets_expire']}" if a.get("resets_expire") else ""))
                head.pack_start(badge, False, False, 0)
            g.attach(head, 1, 0, 2, 1)
            if a["plan"]:
                plan = Gtk.Label(label=a["plan"].title(), xalign=1)
                plan.get_style_context().add_class("qv-meta")
                g.attach(plan, 3, 0, 1, 1)
            row = 1
            if a["error"]:
                e = Gtk.Label(label=a["error"], xalign=0, wrap=True)
                e.get_style_context().add_class("qv-warn")
                g.attach(e, 1, row, 3, 1)
                row += 1
            for cd in a.get("cooldowns") or []:
                rel = when(cd.get("until"))[0]
                cd_txt = f"Cooldown: {cd.get('model', 'account')}" + (f" ({rel})" if rel else "")
                lbl = Gtk.Label(label=cd_txt, xalign=0, ellipsize=3)
                lbl.get_style_context().add_class("qv-warn")
                g.attach(lbl, 1, row, 3, 1)
                row += 1
            for label, pct, reset, note in a["rows"]:
                name = Gtk.Label(label=label, xalign=0, ellipsize=3)
                name.get_style_context().add_class("qv-meta")
                details = (a.get("row_details") or {}).get(label)
                if details:
                    tip_lines = [
                        f"{m['name']}: {m['used_percent']:.0f}%"
                        + (f" ({when(m.get('reset_at'))[0]})" if m.get("reset_at") else "")
                        for m in details
                    ]
                    name.set_tooltip_text("\n".join(tip_lines))
                self.cols[0].add_widget(name)
                g.attach(name, 1, row, 1, 1)
                bar = Gtk.DrawingArea(hexpand=True, valign=Gtk.Align.CENTER)
                bar.set_size_request(120, 5)
                bar.connect("draw", lambda w, cr, p=pct: draw_bar(cr, 0, 0, w.get_allocated_width(), 5, p))
                if details:
                    bar.set_tooltip_text(name.get_tooltip_text())
                g.attach(bar, 2, row, 1, 1)
                rel, _ = when(reset)
                right = f"{pct:.0f}%" if pct is not None else "–"
                tail = rel or note
                lab = Gtk.Label(xalign=1)
                lab.get_style_context().add_class("qv-pct")
                self.cols[1].add_widget(lab)
                lab.set_markup(
                    GLib.markup_escape_text(right)
                    + (
                        f"  <span alpha='62%' weight='normal'>{GLib.markup_escape_text(tail)}</span>"
                        if tail
                        else ""
                    )
                )
                g.attach(lab, 3, row, 1, 1)
                row += 1
            return g

    class Tray:
        def __init__(self):
            self.theme = Theme()
            self.notifications_enabled = bool(load_settings().get("notifications", True))
            self.notify_state = load_notify_state()
            self.accounts, self.updated = [], None
            self.status_icon = None
            self.indicator = None
            self.panel = Panel(self)

            backend = str(load_settings().get("tray_backend", "auto")).lower()
            use_indicator = (
                app_indicator_mod is not None
                and (backend == "appindicator" or (backend == "auto" and os.environ.get("XDG_SESSION_TYPE") == "wayland"))
            )
            if use_indicator:
                self.indicator = app_indicator_mod.Indicator.new(
                    "cpa-quota-view",
                    "utilities-system-monitor-symbolic",
                    app_indicator_mod.IndicatorCategory.APPLICATION_STATUS,
                )
                self.indicator.set_status(app_indicator_mod.IndicatorStatus.ACTIVE)
                self.indicator.set_menu(self._build_menu(include_toggle=True))
            else:
                self.status_icon = Gtk.StatusIcon()
                self.status_icon.set_from_icon_name("utilities-system-monitor-symbolic")
                self.status_icon.set_tooltip_text("AI quota")
                self.status_icon.set_title("AI quota")
                self.status_icon.connect("activate", self.toggle)
                self.status_icon.connect("popup-menu", self.popup)

            # Hydrate immediately from disk cache if available for instant startup.
            cached = load_cached_snapshot()
            if cached:
                self._apply(accounts_from_snapshot(cached), from_cache=True)

            self.refresh()
            GLib.timeout_add_seconds(get_refresh_sec(), self.refresh)

        def _toggle_notifications(self, widget):
            self.notifications_enabled = widget.get_active()
            save_settings({**load_settings(), "notifications": self.notifications_enabled})

        def _build_menu(self, include_toggle=False):
            m = Gtk.Menu()
            if include_toggle:
                tog = Gtk.MenuItem(label="Show / Hide Quota Panel")
                tog.connect("activate", self.toggle)
                m.append(tog)
                m.append(Gtk.SeparatorMenuItem())
            for text, cb in (
                ("Refresh", self.refresh),
                ("Open CPA dashboard", open_dashboard),
                ("Open quota page in browser", open_quota_page),
                ("Copy management key", copy_key),
            ):
                it = Gtk.MenuItem(label=text)
                it.connect("activate", cb)
                m.append(it)
            m.append(Gtk.SeparatorMenuItem())
            notif = Gtk.CheckMenuItem(label="Desktop notifications")
            notif.set_active(self.notifications_enabled)
            notif.connect("toggled", self._toggle_notifications)
            m.append(notif)
            m.append(Gtk.SeparatorMenuItem())
            group = None
            for mode, text in (("system", "System theme"), ("light", "Light"), ("dark", "Dark")):
                it = Gtk.RadioMenuItem.new_with_label_from_widget(group, text)
                group = group or it
                it.set_active(self.theme.mode == mode)
                it.connect("toggled", lambda w, mo=mode: w.get_active() and self.theme.set(mo))
                m.append(it)
            m.append(Gtk.SeparatorMenuItem())
            q = Gtk.MenuItem(label="Quit")
            q.connect("activate", Gtk.main_quit)
            m.append(q)
            m.show_all()
            return m

        def refresh(self, *_):
            threading.Thread(target=self._fetch, daemon=True).start()
            return True

        def _fetch(self):
            try:
                accounts, _ = fetch_snapshot()
            except Exception as e:
                accounts = [{
                    "provider": "proxy",
                    "label": "CLIProxyAPI",
                    "plan": "",
                    "status": "error",
                    "status_message": "",
                    "cooldowns": [],
                    "recent_ok": 0,
                    "recent_fail": 0,
                    "rows": [],
                    "row_details": {},
                    "error": str(e)[:90],
                }]
            GLib.idle_add(self._apply, accounts, False)

        def _apply(self, accounts, from_cache=False):
            self.accounts = accounts
            self.updated = "cached" if from_cache else time.strftime("%H:%M")
            icon_path = tray_icon(accounts)
            if self.status_icon is not None:
                self.status_icon.set_from_file(icon_path)
                per_prov = {}
                for a in accounts:
                    vals = [r[1] for r in a["rows"] if r[1] is not None]
                    if vals:
                        per_prov[a["provider"]] = max(per_prov.get(a["provider"], 0), max(vals))
                if per_prov:
                    summary = " · ".join(f"{NAMES.get(p, p)} {v:.0f}%" for p, v in per_prov.items())
                    self.status_icon.set_tooltip_text(f"AI quota · {summary}")
                else:
                    self.status_icon.set_tooltip_text("AI quota")
            elif self.indicator is not None:
                self.indicator.set_icon_full(icon_path, "AI quota")

            if not from_cache:
                events, new_state = compute_notifications(
                    self.notify_state, accounts, thresholds=get_notify_thresholds()
                )
                self.notify_state = new_state
                save_notify_state(new_state)
                if self.notifications_enabled:
                    for urg, title, body in events:
                        send_desktop_notification(urg, title, body)

            self.panel.render()
            return False

        def show_panel(self, *_):
            self.panel.render()
            self.panel.show_all()
            self.panel.present()

        def toggle(self, *_):
            if self.panel.get_visible():
                self.panel.hide()
            else:
                self.show_panel()

        def popup(self, icon, button, t):
            m = self._build_menu(include_toggle=False)
            m.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, t)

    tray = Tray()

    def handle_cmd(cmd):
        if cmd == "show":
            GLib.idle_add(tray.show_panel)
        elif cmd == "toggle":
            GLib.idle_add(tray.toggle)
        elif cmd == "refresh":
            GLib.idle_add(tray.refresh)

    srv = _start_control_server(handle_cmd)
    if start_panel:
        GLib.timeout_add(400, tray.show_panel)
    try:
        Gtk.main()
    finally:
        try:
            srv.close()
        except OSError:
            pass
        try:
            os.unlink(CONTROL_SOCK)
        except OSError:
            pass


def _load_accounts_for_cli(max_age=None, providers=None):
    snap = load_cached_snapshot(max_age=max_age) if max_age is not None else None
    if snap is not None:
        accounts = accounts_from_snapshot(snap)
    else:
        accounts, snap = fetch_snapshot()
    accounts = filter_accounts(accounts, providers)
    if providers:
        snap = build_json_snapshot(accounts, server_version=snap.get("server_version", ""))
    return accounts, snap


def main(argv=None):
    parser = argparse.ArgumentParser(description="Tray and CLI quota viewer for CLIProxyAPI.")
    parser.add_argument("--print", dest="print_table", action="store_true", help="Print quota summary and exit")
    parser.add_argument("--json", dest="print_json", action="store_true", help="Print sanitized JSON snapshot and exit")
    parser.add_argument("--short", action="store_true", help="Print compact one-line summary (e.g. for tmux/prompts)")
    parser.add_argument("--waybar", action="store_true", help="Print Waybar custom-module JSON and exit")
    parser.add_argument("--watch", nargs="?", const=60, type=int, metavar="SECS", help="Live-refreshing terminal view")
    parser.add_argument("--provider", action="append", help="Filter by provider (repeatable or comma-separated)")
    parser.add_argument("--max-age", type=int, metavar="SECS", help="Reuse cached snapshot if newer than SECS")
    parser.add_argument("--cached", action="store_true", help="Shortcut for --max-age 300")
    parser.add_argument("--fail-above", type=float, metavar="PCT", help="Exit with status 2 if any window >= PCT")
    parser.add_argument("--refresh", action="store_true", help="Ask running tray instance to refresh immediately")
    parser.add_argument("--panel", action="store_true", help="Open the popup panel (toggles existing tray if running)")
    parser.add_argument("--pin", action="store_true", help="Keep the popup panel open when focus leaves")
    args = parser.parse_args(argv)

    if args.refresh:
        if send_control_command("refresh"):
            return 0
        print("quota-view: no running tray instance found", file=sys.stderr)
        return 1

    providers = []
    for p in args.provider or []:
        providers.extend(p.split(","))
    max_age = args.max_age if args.max_age is not None else (300 if args.cached else None)

    if args.watch is not None:
        interval = max(15, args.watch)
        try:
            while True:
                accounts, snap = _load_accounts_for_cli(max_age=None, providers=providers)
                sys.stdout.write("\033[2J\033[H")
                print(f"CLIProxyAPI Quota · {get_base_url()} · {time.strftime('%H:%M:%S')} (every {interval}s)\n")
                print(format_table(accounts))
                sys.stdout.flush()
                time.sleep(interval)
        except KeyboardInterrupt:
            return 0

    cli_mode = args.print_json or args.print_table or args.short or args.waybar or args.fail_above is not None
    if cli_mode:
        accounts, snap = _load_accounts_for_cli(max_age=max_age, providers=providers)
        if args.print_json:
            print(json.dumps(snap, indent=2))
        elif args.waybar:
            print(json.dumps(format_waybar(accounts)))
        elif args.short:
            print(format_short(accounts))
        elif args.print_table:
            print(format_table(accounts))
        if args.fail_above is not None:
            worst = max((r[1] for a in accounts for r in a["rows"] if r[1] is not None), default=0.0)
            if worst >= args.fail_above:
                return 2
        return 0

    run_gui(start_panel=args.panel, pin=args.pin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
