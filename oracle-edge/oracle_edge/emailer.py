"""Email alerts over SMTP (Gmail / Google Workspace app password, Outlook, iCloud or custom).

Settings live in the app (config table); the password lives in Windows Credential Manager.
"""

from __future__ import annotations

import html
import smtplib
import ssl
from email.message import EmailMessage

from . import db, secrets
from .signals import BADGE_LABEL

PRESETS = {
    "google": {"label": "Google (Gmail / Workspace)", "host": "smtp.gmail.com", "port": 587, "security": "starttls",
               "help": "Use a 16-character app password from myaccount.google.com/apppasswords "
                       "(needs 2-Step Verification; Workspace admins must allow app passwords)."},
    "outlook": {"label": "Outlook / Microsoft 365", "host": "smtp.office365.com", "port": 587, "security": "starttls",
                "help": "Your organization must allow SMTP AUTH for the mailbox."},
    "icloud": {"label": "iCloud Mail", "host": "smtp.mail.me.com", "port": 587, "security": "starttls",
               "help": "Use an app-specific password from appleid.apple.com."},
    "custom": {"label": "Custom SMTP", "host": "", "port": 587, "security": "starttls", "help": ""},
}

DEFAULTS = {"enabled": False, "preset": "google", "host": "smtp.gmail.com", "port": 587, "security": "starttls",
            "username": "", "from_name": "Oracle Edge", "to": [], "send_actions": True, "send_weekly": True,
            "send_failures": True, "send_price_alerts": True}


def settings() -> dict:
    s = dict(DEFAULTS)
    s.update(db.cfg("email", {}) or {})
    return s


def save_settings(s: dict, password: str | None = None) -> None:
    db.set_cfg("email", {k: s[k] for k in DEFAULTS if k in s})
    if password:
        secrets.set("smtp_password", password.replace(" ", ""))


def send(subject: str, html_body: str, text_body: str = "") -> None:
    s = settings()
    pw = secrets.get("smtp_password")
    if not s["username"] or not pw or not s["to"]:
        raise RuntimeError("Email isn't set up yet (Settings → Email).")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{s['from_name']} <{s['username']}>"
    msg["To"] = ", ".join(s["to"])
    msg.set_content(text_body or "Open this email in an HTML-capable mail app.")
    msg.add_alternative(html_body, subtype="html")
    ctx = ssl.create_default_context()
    if s["security"] == "ssl":
        with smtplib.SMTP_SSL(s["host"], int(s["port"]), context=ctx, timeout=30) as smtp:
            smtp.login(s["username"], pw)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(s["host"], int(s["port"]), timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ctx)
            smtp.ehlo()
            smtp.login(s["username"], pw)
            smtp.send_message(msg)


def explain_error(e: Exception) -> str:
    t = str(e)
    if "5.7.9" in t or "Application-specific password required" in t:
        return "Google wants an app password (534-5.7.9). Create one at myaccount.google.com/apppasswords."
    if "5.7.8" in t or "Username and Password not accepted" in t:
        return "Google rejected the sign-in (535-5.7.8). Check the address and that you pasted the app password."
    if "timed out" in t.lower():
        return "Couldn't reach the mail server (timeout). Check the server name and port."
    return t


def send_test() -> str:
    try:
        send("Oracle Edge · test email", _wrap("<p>Your Oracle Edge email alerts are set up correctly. ✅</p>"))
        return "Sent! Check your inbox."
    except Exception as e:
        raise RuntimeError(explain_error(e)) from e


COLORS = {"STRONG_BUY": "#1f6f4a", "BUY": "#2e8b57", "ADD": "#2e8b57", "SELL": "#a33a2b", "SWAP": "#2c5d8f",
          "TRIM": "#7a4f9e"}


def _wrap(inner: str) -> str:
    return f"""<div style="font-family:Segoe UI,Arial,sans-serif;max-width:640px;margin:auto;color:#1d2321">
    <div style="font-size:20px;font-weight:700;margin:8px 0 14px">Oracle Edge</div>{inner}
    <p style="color:#888;font-size:12px;margin-top:24px">Research tool, not financial advice. Signals are meant to be
    acted on at the next market open.</p></div>"""


def _g(r, k):
    try:
        return r[k]
    except (KeyError, IndexError):
        return None


def _strength(r) -> str:
    from . import strength

    return strength.strength_html(r["type"], _g(r, "rank_score"), _g(r, "quality"), _g(r, "mos"), r["reason"] or "")


def _metrics(r) -> str:
    from . import strength

    t = strength.metrics_text(_g(r, "rank"), _g(r, "rank_score"), _g(r, "quality"), _g(r, "value_score"), _g(r, "mos"),
                              _g(r, "exp_return"))
    return f"<div style='font-size:12.5px;color:#5d6763;margin-bottom:4px'>{html.escape(t)}</div>" if t else ""


def digest_html(rows, extra: str = "") -> str:
    cards = []
    for r in rows:
        c = COLORS.get(r["type"], "#444")
        lvl = ""
        if r["buy_below"]:
            lvl = (f"<div style='font-size:13px;color:#555'>Buy below <b>${r['buy_below']:,.2f}</b> · strong buy below "
                   f"${(r['strong_buy_below'] or 0):,.2f} · value ${(r['iv'] or 0):,.2f} · last ${(r['price'] or 0):,.2f}</div>")
        cards.append(
            f"<div style='border:1px solid #e2e0d8;border-left:5px solid {c};border-radius:8px;padding:12px 14px;"
            f"margin:10px 0'><div><span style='background:{c};color:#fff;border-radius:999px;padding:2px 10px;"
            f"font-size:12px;font-weight:700'>{html.escape(BADGE_LABEL.get(r['type'], r['type']))}</span> "
            f"<b style='font-size:16px'>{html.escape(r['ticker'] or '')}</b> "
            f"<span style='color:#666'>{html.escape(r['name'] or '')}</span> {_strength(r)}</div>"
            f"<div style='margin:6px 0'>{html.escape(r['reason'] or '')}</div>{_metrics(r)}{lvl}</div>")
    return _wrap("".join(cards) + extra)


ALERT_COLORS = {"reached": "#1f6f4a", "approaching": "#9a5b00"}


def alerts_html(alerts) -> str:
    if not alerts:
        return ""
    items = []
    for a in alerts:
        c = "#a24d00" if a["kind"] == "trim" else ALERT_COLORS.get(a["state"], "#444")
        tag = ("TARGET REACHED" if a["state"] == "reached" else "APPROACHING")
        items.append(f"<div style='border-left:4px solid {c};padding:6px 10px;margin:6px 0'>"
                     f"<span style='color:{c};font-size:11px;font-weight:700'>{tag}</span> "
                     f"<b>{html.escape(a['ticker'] or '')}</b> {html.escape(a['message'] or '')}</div>")
    return ("<h3 style='margin:18px 0 4px;font-size:15px'>Price targets</h3><div style='font-size:12px;color:#666'>"
            "Targets come from the valuation; alerts use the daily close (and the day's high/low for 'touched')."
            "</div>" + "".join(items))


def _pending_market():
    try:
        return db.rows("SELECT * FROM market_alerts WHERE emailed=0 AND date >= date('now','-5 days') ORDER BY id")
    except Exception:
        return []


def market_html(rows) -> str:
    if not rows:
        return ""
    items = "".join(f"<div style='border-left:4px solid #8A5A12;padding:6px 10px;margin:6px 0'>"
                    f"<span style='color:#8A5A12;font-size:11px;font-weight:700'>MARKET</span> "
                    f"{html.escape(r['message'] or '')}</div>" for r in rows)
    return ("<h3 style='margin:18px 0 4px;font-size:15px'>Market temperature</h3><div style='font-size:12px;"
            "color:#666'>Warnings, not hard stops: Oracle Edge keeps following its rules.</div>" + items)


def _mark_market(rows) -> None:
    db.get().executemany("UPDATE market_alerts SET emailed=1 WHERE id=?", [(r["id"],) for r in rows])


def _pending_alerts():
    return db.rows("""SELECT a.*, s.ticker FROM price_alerts a JOIN securities s ON s.cik=a.cik
                      WHERE a.emailed=0 AND a.date >= date('now','-5 days') ORDER BY a.state DESC, s.ticker""")


def send_digest() -> int:
    s = settings()
    if not s["enabled"]:
        return 0
    alerts = _pending_alerts() if s.get("send_price_alerts", True) else []
    market = _pending_market()
    if not s["send_actions"]:
        return _send_alerts_only(alerts, market)
    rows = db.rows(
        """SELECT g.id, g.type, g.reason, g.date, g.price, s.ticker, s.name, e.buy_below, e.strong_buy_below, e.iv,
                  e.rank, e.rank_score, e.quality, e.value AS value_score, e.mos, e.exp_return
           FROM signals g JOIN runs r ON r.id=g.run_id AND r.kind='live'
           JOIN securities s ON s.cik=g.cik
           LEFT JOIN evaluations e ON e.run_id=g.run_id AND e.date=g.date AND e.cik=g.cik
           WHERE g.emailed=0 AND g.date >= date('now','-5 days') ORDER BY g.type, s.ticker""")
    if not rows:
        return _send_alerts_only(alerts, market)
    order = ["STRONG_BUY", "BUY", "ADD", "SELL", "SWAP", "TRIM"]
    rows = sorted(rows, key=lambda r: order.index(r["type"]) if r["type"] in order else 9)
    subject = "Oracle Edge · " + " · ".join(f"{BADGE_LABEL.get(r['type'], r['type'])} {r['ticker']}" for r in rows[:4])
    if len(rows) > 4:
        subject += f" +{len(rows) - 4} more"
    if alerts:
        subject += f" · {len(alerts)} price alert{'s' if len(alerts) > 1 else ''}"
    if market:
        subject += " · market warning"
    send(subject, digest_html(rows, market_html(market) + alerts_html(alerts)))
    db.get().executemany("UPDATE signals SET emailed=1 WHERE id=?", [(r["id"],) for r in rows])
    _mark_alerts(alerts)
    _mark_market(market)
    return len(rows) + len(alerts) + len(market)


def _mark_alerts(alerts) -> None:
    db.get().executemany("UPDATE price_alerts SET emailed=1 WHERE id=?", [(a["id"],) for a in alerts])


def _send_alerts_only(alerts, market=()) -> int:
    if not alerts and not market:
        return 0
    if alerts:
        reached = [a for a in alerts if a["state"] == "reached"]
        lead = reached or alerts
        subject = ("Oracle Edge · " + ("Target reached: " if reached else "Approaching target: ") +
                   ", ".join(dict.fromkeys(a["ticker"] for a in lead[:4])))
        if market:
            subject += " · market warning"
    else:
        subject = "Oracle Edge · Market warning: " + (market[0]["message"] or "")[:80]
    send(subject, _wrap(market_html(market) + alerts_html(alerts)))
    _mark_alerts(alerts)
    _mark_market(market)
    return len(alerts) + len(market)


def send_failure(text: str) -> None:
    s = settings()
    if s["enabled"] and s["send_failures"]:
        send("Oracle Edge · daily run problem", _wrap(f"<p>{html.escape(text)}</p>"))


def send_weekly(run_id: int) -> bool:
    """Friday summary: model vs S&P since start, holdings, closest WATCH names, market temperature."""
    s = settings()
    if not s["enabled"] or not s["send_weekly"]:
        return False
    from datetime import date

    key = f"{date.today().isocalendar()[0]}-W{date.today().isocalendar()[1]}"
    if db.cfg("weekly_sent") == key:
        return False
    from .ui import data as d

    curve = d.equity(run_id)
    st = d.stats(curve)
    hold = d.holdings(run_id)
    ld = d.latest_date(run_id)
    ev = [e for e in d.evaluations(run_id, ld) if e["list"] == "pricey" and e["buy_below"]] if ld else []
    ev = sorted(ev, key=lambda e: e["price"] / e["buy_below"])[:8]
    mk = d.market_now()["regime"]
    pct = lambda v: "—" if v is None else f"{v * 100:.1f}%"  # noqa: E731
    rows = "".join(f"<tr><td><b>{html.escape(e['ticker'])}</b></td><td>{html.escape(e['name'][:30])}</td>"
                   f"<td>${e['price']:,.2f}</td><td>buy below ${e['buy_below']:,.2f}</td>"
                   f"<td>{(e['price'] / e['buy_below'] - 1) * 100:.0f}% away</td></tr>" for e in ev)
    body = (f"<p><b>Model portfolio</b>: CAGR {pct(st.get('cagr'))} vs S&amp;P 500 {pct(st.get('bench_cagr'))} · "
            f"{len(hold)} holdings · market cap ÷ GDP {pct(mk.ratio)} ({mk.zone}).</p>"
            f"<p><b>Closest to a buy price</b></p><table cellpadding='4'>{rows or '<tr><td>None</td></tr>'}</table>")
    send("Oracle Edge · weekly summary", _wrap(body))
    db.set_cfg("weekly_sent", key)
    return True
