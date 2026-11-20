import base64
from datetime import datetime, timezone
import json
import os
import tempfile
import unittest
from unittest import mock

import quota_view as qv


class QuotaViewTests(unittest.TestCase):
    def test_claude_rows_and_scoped_limits(self):
        payload = {
            "five_hour": {"utilization": 22.5, "resets_at": "2026-11-21T05:00:00Z"},
            "seven_day": {"utilization": 85.0, "resets_at": "2026-11-24T00:00:00Z"},
            "limits": [
                {
                    "kind": "weekly_scoped",
                    "percent": 12.0,
                    "resets_at": "2026-11-24T00:00:00Z",
                    "is_active": True,
                    "scope": {"model": {"display_name": "Fable"}},
                }
            ],
            "extra_usage": {"is_enabled": True, "utilization": 10.0, "used_credits": 4.5},
        }
        rows = qv.rows_for("claude", payload)
        self.assertEqual([r[0] for r in rows], ["5 hours", "7 days", "7 days Fable", "Extra credits"])
        self.assertEqual(rows[0][1], 22.5)
        self.assertEqual(rows[2][3], "active limit")
        self.assertEqual(rows[3][3], "$4.5 used")

    def test_passive_claude_fallback(self):
        entry = {
            "quota": {
                "signals": {
                    "Anthropic-Ratelimit-Unified-7d-Utilization": "0.99",
                    "Anthropic-Ratelimit-Unified-7d-Reset": "1795431600",
                }
            }
        }
        rows = qv.passive_claude_rows(entry)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "7 days")
        self.assertAlmostEqual(rows[0][1], 99.0)
        self.assertEqual(rows[0][3], "cached")

    def test_codex_jwt_account_id_and_rows(self):
        claims = base64.urlsafe_b64encode(
            json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-123"}}).encode()
        ).decode().rstrip("=")
        entry = {"id_token": f"hdr.{claims}.sig"}
        self.assertEqual(qv.codex_account_id(entry), "acct-123")

        payload = {
            "rate_limit": {
                "primary_window": {"used_percent": 15, "limit_window_seconds": 18000, "reset_at": 1795431600},
                "secondary_window": {"used_percent": 40, "limit_window_seconds": 604800, "reset_at": 1795600000},
            },
            "credits": {"has_credits": True, "balance": 25},
        }
        rows = qv.rows_for("codex", payload)
        self.assertEqual([r[0] for r in rows], ["5 hours", "7 days", "Credits"])
        self.assertEqual(rows[0][1], 15.0)
        self.assertEqual(rows[1][1], 40.0)

    def test_antigravity_grouping_details_and_exhausted_detection(self):
        payload = {
            "models": {
                "claude-opus-5-5-high": {
                    "displayName": "Claude Opus 5.5 (High)",
                    "quotaInfo": {"resetTime": "2026-11-21T01:45:10Z"},
                },
                "gpt-oss-120b-medium": {
                    "displayName": "GPT-OSS 120B (Medium)",
                    "quotaInfo": {"remainingFraction": 0.5, "resetTime": "2026-11-21T02:00:00Z"},
                },
                "gemini-2.5-pro": {
                    "displayName": "Gemini 2.5 Pro",
                    "quotaInfo": {"remainingFraction": 0.8, "resetTime": "2026-11-21T02:34:37Z"},
                },
                "gemini-3-flash": {
                    "displayName": "Gemini 3 Flash",
                    "quotaInfo": {"remainingFraction": 1.0, "resetTime": "2026-11-21T02:34:37Z"},
                },
                "tab_flash_lite_preview": {
                    "displayName": None,
                    "quotaInfo": {"remainingFraction": 1.0},
                },
            }
        }
        rows, details = qv.parse_antigravity_families(payload)
        self.assertEqual([r[0] for r in rows], ["Claude / GPT", "Gemini Pro", "Gemini Flash"])
        self.assertAlmostEqual(rows[0][1], 100.0)
        self.assertAlmostEqual(rows[1][1], 20.0)
        self.assertAlmostEqual(rows[2][1], 0.0)
        self.assertEqual(len(details["Claude / GPT"]), 2)

    def test_gemini_cli_buckets(self):
        payload = {
            "buckets": [
                {"modelId": "gemini-2.5-pro", "remainingFraction": 0.65, "resetTime": "2026-11-21T06:00:00Z"},
                {"modelId": "gemini-2.5-flash", "remainingFraction": 0.9, "resetTime": "2026-11-21T06:00:00Z"},
            ]
        }
        rows = qv.rows_for("gemini-cli", payload)
        self.assertEqual(rows[0][0], "Gemini Flash")
        self.assertAlmostEqual(rows[0][1], 10.0)
        self.assertEqual(rows[1][0], "Gemini Pro")
        self.assertAlmostEqual(rows[1][1], 35.0)

    def test_when_relative_formatting(self):
        now = datetime(2026, 11, 21, 0, 0, 0, tzinfo=timezone.utc)
        rel, _ = qv.when("2026-11-21T02:15:00Z", now=now)
        self.assertEqual(rel, "in 2h 15m")
        rel_past, _ = qv.when("2026-11-20T23:00:00Z", now=now)
        self.assertEqual(rel_past, "now")

    def test_cooldown_parsing_and_formatting(self):
        cds = qv.parse_cooldowns({
            "cooldowns": [
                {"model": "gemini-3-flash", "until": "2026-11-21T03:00:00Z", "reason": "429"},
                "claude-opus",
            ]
        })
        self.assertEqual(len(cds), 2)
        self.assertEqual(cds[0]["model"], "gemini-3-flash")
        self.assertEqual(cds[1]["model"], "claude-opus")

    def test_notifications_threshold_reset_and_error(self):
        prev = {
            "windows": {
                "claude:a@example.com:7 days": 70.0,
                "codex:b@example.com:7 days": 100.0,
            },
            "errors": {},
        }
        accounts = [
            {
                "provider": "claude",
                "label": "a@example.com",
                "disabled": False,
                "error": "",
                "rows": [("7 days", 85.0, None, "")],
            },
            {
                "provider": "codex",
                "label": "b@example.com",
                "disabled": False,
                "error": "",
                "rows": [("7 days", 0.0, None, "")],
            },
            {
                "provider": "xai",
                "label": "c@example.com",
                "disabled": False,
                "error": "HTTP 401",
                "rows": [],
            },
        ]
        events, state = qv.compute_notifications(prev, accounts, thresholds=[80, 100])
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0][0], "normal")
        self.assertIn("85%", events[0][1])
        self.assertEqual(events[1][0], "low")
        self.assertIn("reset", events[1][1])
        self.assertIn("error", events[2][1])
        self.assertEqual(state["windows"]["claude:a@example.com:7 days"], 85.0)

    def test_short_waybar_filter_and_cache_roundtrip(self):
        accounts = [
            {
                "provider": "claude",
                "label": "alice@example.com",
                "status": "active",
                "status_message": "",
                "plan": "",
                "disabled": False,
                "error": "",
                "cooldowns": [],
                "recent_ok": 12,
                "recent_fail": 1,
                "rows": [("7 days", 88.4, "2026-11-24T00:00:00Z", "")],
                "row_details": {},
            },
            {
                "provider": "codex",
                "label": "bob@example.com",
                "status": "active",
                "status_message": "",
                "plan": "pro",
                "disabled": False,
                "error": "",
                "cooldowns": [],
                "recent_ok": 4,
                "recent_fail": 0,
                "rows": [("7 days", 5.0, "2026-11-25T00:00:00Z", "")],
                "row_details": {},
            },
        ]
        self.assertEqual(qv.format_short(accounts), "CL:88% CX:5%")
        wb = qv.format_waybar(accounts)
        self.assertEqual(wb["class"], "hot")
        self.assertEqual(wb["text"], "CL:88% CX:5%")

        filtered = qv.filter_accounts(accounts, ["codex"])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["provider"], "codex")

        snap = qv.build_json_snapshot(accounts, server_version="v8.0.79")
        self.assertEqual(snap["server_version"], "v8.0.79")
        self.assertEqual(snap["summary"]["recent_requests"], 16)
        self.assertEqual(snap["summary"]["recent_errors"], 1)

        restored = qv.accounts_from_snapshot(snap)
        self.assertEqual(len(restored), 2)
        self.assertAlmostEqual(restored[0]["rows"][0][1], 88.4)

    def test_fail_above_exit_code(self):
        snap = qv.build_json_snapshot([
            {
                "provider": "claude",
                "label": "alice@example.com",
                "status": "active",
                "status_message": "",
                "plan": "",
                "disabled": False,
                "error": "",
                "cooldowns": [],
                "recent_ok": 0,
                "recent_fail": 0,
                "rows": [("7 days", 92.0, None, "")],
                "row_details": {},
            }
        ])
        with tempfile.TemporaryDirectory() as td:
            cache_path = os.path.join(td, "snapshot.json")
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(snap, fh)
            with mock.patch.object(qv, "SNAPSHOT_CACHE", cache_path), mock.patch.object(
                qv, "get_base_url", return_value=snap["base_url"]
            ):
                self.assertEqual(qv.main(["--cached", "--fail-above", "90"]), 2)
                self.assertEqual(qv.main(["--cached", "--fail-above", "95"]), 0)


if __name__ == "__main__":
    unittest.main()
