"""Windows toast notification (best effort; silently skipped elsewhere)."""

from __future__ import annotations

import os
import subprocess

from . import db


def summary_line() -> str:
    last = db.cfg("last_daily_run", {}) or {}
    rows = db.rows(
        """SELECT g.type, s.ticker FROM signals g JOIN runs r ON r.id=g.run_id AND r.kind='live'
           JOIN securities s ON s.cik=g.cik WHERE g.date=? ORDER BY g.type""", (last.get("through", ""),))
    n_alerts = last.get("price_alerts") or 0
    extra = f" · {n_alerts} price alert{'s' if n_alerts != 1 else ''}" if n_alerts else ""
    if not rows:
        return f"Up to date through {last.get('through', '?')}. No new actions{extra}."
    sym = {"STRONG_BUY": "▲▲", "BUY": "▲", "ADD": "＋", "SELL": "▼", "SWAP": "⇄", "TRIM": "✂"}
    return " · ".join(f"{sym.get(r['type'], '')} {r['type'].replace('_', ' ')} {r['ticker']}" for r in rows[:6]) + extra


def toast(title: str, message: str) -> None:
    if os.name != "nt":
        return
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode('{_esc(title)}')) > $null;"
        f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode('{_esc(message)}')) > $null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Oracle Edge').Show("
        "[Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=20,
                   creationflags=0x08000000)


def _esc(s: str) -> str:
    return s.replace("'", "''")[:240]
