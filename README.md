# cpa-quota-view

A lightweight desktop tray applet and CLI/JSON quota viewer for [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) that works against **any stock CLIProxyAPI instance** — local or remote (Docker, Swarm, Tailscale) — **without installing a server-side plugin**.

## How It Works Without a Plugin

Unlike native Go plugins such as [`cpa-quota-api-extension`](https://github.com/ahmed-khaire/cpa-quota-api-extension), `cpa-quota-view` does not require `CGO_ENABLED=1`, `plugins.enabled: true`, or recompiling a `.so` whenever CLIProxyAPI updates (e.g. daily `:latest` Docker upgrades).

Instead, it uses two endpoints already built into every stock CLIProxyAPI Management API:

```text
cpa-quota-view (GTK tray / --print / --json)
        │
        │ HTTPS + MANAGEMENT_KEY (from env or 1Password CLI)
        ▼
CLIProxyAPI /v0/management
  ├── GET  /auth-files   → lists accounts, providers, auth_index, project_id, id_token, passive signals
  └── POST /api-call     → server-side authenticated relay (substitutes $TOKEN$ with live OAuth token)
        │
        ├── Anthropic  api.anthropic.com/api/oauth/usage
        ├── OpenAI     chatgpt.com/backend-api/wham/usage (+ rate-limit-reset-credits)
        ├── Antigravity daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels
        ├── Gemini CLI cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota
        ├── xAI        cli-chat-proxy.grok.com/v1/billing
        └── Kimi       api.kimi.com/coding/v1/usages
```

**Why this is safe & portable:**
- **Zero OAuth token exposure:** `cpa-quota-view` sends `"Authorization": "Bearer $TOKEN$"` inside `/v0/management/api-call`. CLIProxyAPI replaces `$TOKEN$` server-side (auto-refreshing expired OAuth tokens) and returns only the upstream response.
- **Zero local state required:** Codex's `Chatgpt-Account-Id` header is decoded directly from the `id_token` returned by `/v0/management/auth-files`, so the viewer works against remote Docker/Swarm hosts with no access to `oauth/` on disk.
- **Survives core upgrades:** Because it uses the stable Management API instead of Go plugin ABI bindings, it keeps working automatically across stock `eceasy/cli-proxy-api:latest` upgrades.

### When to Use `cpa-quota-view` vs `cpa-quota-api-extension`

| | `cpa-quota-view` (this repo) | `cpa-quota-api-extension` (Go plugin) |
| --- | --- | --- |
| **Server changes** | None (works on stock Docker `:latest`) | Requires CGO-enabled build + `plugins.enabled: true` |
| **Best for** | Linux desktop tray, terminal checks, ad-hoc `--json` scripts | Shared server-side JSON API for Homepage, Grafana, Home Assistant |
| **Caching & history** | Client-side 5 min refresh | Server-side 30 min cache, SQLite incident history, webhooks |
| **Providers** | Claude, Codex, Antigravity, Gemini CLI, xAI, Kimi, Vertex AI | Codex, Gemini CLI, Antigravity, Claude |

## Supported Providers

- **Claude OAuth:** 5h / 7d windows, model-scoped weekly limits (e.g. Fable), extra credits, plus automatic fallback to passive `quota.signals` if `/api/oauth/usage` is rate-limited.
- **Codex (ChatGPT):** 5h / 7d windows, model-specific rate limits, credit balance, and manual reset credit count/expiry badge.
- **Antigravity:** Live per-family quota (`Claude / GPT`, `Gemini Pro`, `Gemini Flash`) via `fetchAvailableModels` (including exhausted bucket detection).
- **Gemini CLI:** Per-family quota buckets via `retrieveUserQuota`.
- **xAI (Grok):** Monthly usage and billing period reset.
- **Kimi:** Usage windows and reset times.
- **Vertex AI:** Pay-as-you-go status with recent request counts from CLIProxyAPI.

## Configuration

Create `~/.config/quota-view.json` (no secrets stored on disk when using a secret manager command such as 1Password CLI):

```json
{
  "theme": "system",
  "base_url": "https://ai-ts.example.com",
  "mgmt_key_cmd": ["op", "read", "op://Work/swarm-cpa/MANAGEMENT_KEY"],
  "refresh_sec": 300
}
```

Or configure via environment variables:

| Variable | Description |
| --- | --- |
| `CPA_BASE_URL` | CLIProxyAPI base URL (default: `http://127.0.0.1:8317`) |
| `CPA_MANAGEMENT_KEY` | Management key directly (overrides `mgmt_key_cmd`) |
| `CPA_MANAGEMENT_KEY_CMD` | Shell command that prints the management key |
| `CPA_CONFIG_PATH` | Optional fallback path to a local `config.yaml` or `config.toml` |
| `CPA_REFRESH_SEC` | Tray auto-refresh interval in seconds (default: `300`, min `30`) |

## Usage

```bash
# Terminal summary (no GTK required — works headless)
python3 quota_view.py --print

# Sanitized JSON output for scripts / status bars
python3 quota_view.py --json

# Launch GNOME / GTK tray applet
python3 quota_view.py
```

## Install (Ubuntu / GNOME + systemd)

Requires Python 3 and (for the tray GUI) `python3-gi`, `python3-cairo`, and `gir1.2-gtk-3.0`:

```bash
./install.sh
```

This installs the symbolic provider icons (`icons/qv-*-symbolic.svg`), the `.desktop` launcher, and enables `quota-view.service` as a user systemd unit.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
