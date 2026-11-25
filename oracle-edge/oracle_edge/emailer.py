"""Email alerts over SMTP (Gmail / Google Workspace app password, Outlook, iCloud or custom).

Settings live in the app (config table); the password lives in Windows Credential Manager.
"""

from __future__ import annotations

import html
import logging
import smtplib
import socket
import ssl
from email.message import EmailMessage

from . import db, secrets
from .signals import BADGE_LABEL

log = logging.getLogger("oracle_edge")

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
            "send_failures": True, "send_price_alerts": True, "send_calendar": True}


def settings() -> dict:
    s = dict(DEFAULTS)
    s.update(db.cfg("email", {}) or {})
    return s


def save_settings(s: dict, password: str | None = None) -> None:
    db.set_cfg("email", {k: s[k] for k in DEFAULTS if k in s})
    if password:
        secrets.set("smtp_password", password.replace(" ", ""))


class SmtpStepError(RuntimeError):
    """A failure tagged with the step it happened at (connect, greeting, encryption, sign-in, send), with the
    mail server's last error reply when it gave one."""

    def __init__(self, step: str, err: Exception, server: str = ""):
        super().__init__(f"{step}: {err}" + (f" (server said: {server})" if server else ""))
        self.step, self.err, self.server = step, err, server


class _Recorder:
    """Keeps the server's replies (never what we send, so no password), to report its real reason for a refusal."""

    def getreply(self):
        code, msg = super().getreply()
        text = msg.decode("utf-8", "replace") if isinstance(msg, bytes) else str(msg)
        self.__dict__.setdefault("replies", []).append((code, " ".join(text.split())))
        return code, msg

    def last_error(self) -> str:
        bad = [f"{c} {t}" for c, t in self.__dict__.get("replies", []) if c >= 400]
        return bad[-1][:300] if bad else ""


def _client(use_ssl: bool):
    """smtplib's client with reply recording, built at connect time (so a substituted smtplib.SMTP is honoured)."""
    base = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    return type(f"Recording{base.__name__}", (_Recorder, base), {})


# The two standard ways to submit mail. Port 465 speaks TLS from the first byte; 587 (and 25/2525) starts in
# plain text and switches with STARTTLS. Using the wrong one for the port is the classic cause of
# "Connection unexpectedly closed": each side waits for the other to speak first until the server hangs up.
def _mode_for(port: int, security: str) -> str:
    if port == 465:
        return "ssl"
    if port in (25, 587, 2525):
        return "starttls"
    return security


def _open(host: str, port: int, security: str, user: str, pw: str, timeout: int = 30) -> smtplib.SMTP:
    """Connect, encrypt and sign in, raising SmtpStepError with the step that failed."""
    ctx = ssl.create_default_context()
    try:
        if security == "ssl":
            smtp = _client(True)(host, port, context=ctx, timeout=timeout)
        else:
            smtp = _client(False)(host, port, timeout=timeout)
    except (OSError, smtplib.SMTPException) as e:
        step = "find server" if isinstance(e, socket.gaierror) else "connect"
        if isinstance(e, (smtplib.SMTPServerDisconnected, ssl.SSLError)):
            step = "greeting"
        raise SmtpStepError(step, e) from e
    try:
        if security != "ssl":
            smtp.ehlo()
            try:
                smtp.starttls(context=ctx)
            except (smtplib.SMTPException, ssl.SSLError, OSError) as e:
                raise SmtpStepError("encryption", e) from e
            smtp.ehlo()
        # One sign-in method only. smtplib otherwise tries the next method after a refusal; if the server has
        # already hung up, its real reason ("535 5.7.8 ...") is replaced by "Connection unexpectedly closed".
        ef = getattr(smtp, "esmtp_features", None)
        feats = (ef.get("auth") or "").upper().split() if isinstance(ef, dict) else []
        for mech in ("PLAIN", "LOGIN"):
            if mech in feats:
                ef["auth"] = mech
                break
        try:
            smtp.login(user, pw)
        except (smtplib.SMTPException, OSError) as e:
            raise SmtpStepError("sign-in", e, smtp.last_error()) from e
        return smtp
    except BaseException:
        try:
            smtp.close()
        except Exception:
            pass
        raise


def _alternative(host: str, port: int, security: str) -> tuple[int, str] | None:
    """The other standard port/encryption pair to try when the first one gets hung up on."""
    if (port, security) == (587, "starttls"):
        return 465, "ssl"
    if (port, security) == (465, "ssl"):
        return 587, "starttls"
    return None


def _connect(s: dict, pw: str) -> smtplib.SMTP:
    host, port = s["host"].strip(), int(s["port"])
    sec = _mode_for(port, s["security"])
    if sec != s["security"]:
        log.warning("email: port %s needs %s, not %s; using %s", port, sec, s["security"], sec)
    try:
        return _open(host, port, sec, s["username"].strip(), pw)
    except SmtpStepError as e:
        alt = _alternative(host, port, sec)
        if e.step not in ("connect", "greeting", "encryption") or not alt:
            raise
        log.warning("email: %s:%s (%s) failed at %s (%s); trying %s:%s", host, port, sec, e.step, e.err, host, alt[0])
        try:
            smtp = _open(host, alt[0], alt[1], s["username"].strip(), pw)
        except SmtpStepError:
            raise e from None
        # the other pair works: remember it so later sends go straight there
        saved = settings()
        saved.update({"port": alt[0], "security": alt[1]})
        db.set_cfg("email", {k: saved[k] for k in DEFAULTS if k in saved})
        log.warning("email: switched to %s:%s (%s), which works", host, alt[0], alt[1])
        return smtp


def send(subject: str, html_body: str, text_body: str = "") -> None:
    s = settings()
    pw = secrets.get("smtp_password")
    if not s["username"] or not pw or not s["to"]:
        raise RuntimeError("Email isn't set up yet (Settings → Email).")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{s['from_name']} <{s['username'].strip()}>"
    msg["To"] = ", ".join(s["to"])
    msg.set_content(text_body or "Open this email in an HTML-capable mail app.")
    msg.add_alternative(html_body, subtype="html")
    try:
        smtp = _connect(s, pw)
        with smtp:
            try:
                smtp.send_message(msg)
            except (smtplib.SMTPException, OSError) as e:
                raise SmtpStepError("send", e, smtp.last_error()) from e
    except Exception as e:
        log.error("email failed (%s, %s:%s %s): %s", subject[:60], s["host"], s["port"], s["security"], e)
        raise


_STEP_HELP = {
    "find server": "The server name couldn't be looked up. Check the spelling of the SMTP server and your internet "
                   "connection.",
    "connect": "Couldn't open a connection to the mail server. A firewall, VPN or antivirus is blocking the port, "
               "or the server name or port is wrong.",
    "greeting": "The connection opened but the server hung up before saying hello. Usual causes: the port and "
                "security don't match (465 = SSL, 587 = STARTTLS), or antivirus 'email protection' / a VPN is "
                "intercepting mail. Oracle Edge already tried the other port.",
    "encryption": "The server hung up while switching to encryption. Usually antivirus 'email protection' "
                  "(Avast, AVG, Norton, Kaspersky, ESET, Bitdefender) or a VPN intercepting the connection: turn its "
                  "mail scanning off, or allow Python.",
    "sign-in": "The server refused the sign-in.",
    "send": "Signed in, but the server refused the message. Check the 'Send to' addresses; Gmail also refuses a "
            "From address that isn't your own.",
}


_SIGNIN_HUNG_UP = (
    "The server hung up during sign-in without giving a reason. For Google: (1) the password must be a 16-letter app "
    "password from myaccount.google.com/apppasswords, not your normal Google password, even if that one is correct; "
    "(2) the sign-in address must be the account the app password was made for; (3) Workspace accounts need the "
    "admin to allow app passwords; (4) after many failed tries Google blocks sign-ins for a while, so wait an hour "
    "and test once.")


def explain_error(e: Exception) -> str:
    t = str(e)
    if "4.7.0" in t and ("Too many" in t or "try again later" in t):
        return ("Google is temporarily blocking sign-ins after too many attempts (454 4.7.0). Wait an hour, then send "
                "one test email.")
    if "5.7.14" in t or "log in via your web browser" in t.lower():
        return ("Google wants you to confirm this sign-in in a browser (534 5.7.14). Sign in to Gmail on this PC, "
                "visit accounts.google.com/DisplayUnlockCaptcha, then test again.")
    if "5.7.9" in t or "Application-specific password required" in t:
        return "Google wants an app password (534-5.7.9). Create one at myaccount.google.com/apppasswords."
    if "5.7.8" in t or "Username and Password not accepted" in t:
        return ("Google rejected the sign-in (535-5.7.8). The sign-in address must be the full Gmail or Workspace "
                "address the app password was made for, and the password is the 16-letter app password, not "
                "your normal one.")
    if "5.7.139" in t or "SmtpClientAuthentication is disabled" in t:
        return "Microsoft 365 has SMTP sign-in turned off for this mailbox (5.7.139). An admin has to allow SMTP AUTH."
    if isinstance(e, SmtpStepError):
        if e.step == "sign-in" and not e.server and isinstance(e.err, smtplib.SMTPServerDisconnected):
            return f"Failed at 'sign-in': {e.err}. {_SIGNIN_HUNG_UP}"
        said = f" The server said: {e.server}." if e.server else ""
        return f"Failed at '{e.step}': {e.err}.{said} {_STEP_HELP.get(e.step, '')}"
    if "timed out" in t.lower():
        return "Couldn't reach the mail server (timeout). Check the server name and port."
    return t


def diagnose() -> list[dict]:
    """Walk through every step of sending, without sending, and say where it stops.
    Each item: {step, ok, detail}. Tries both standard ports so a blocked or mismatched one shows up."""
    s = settings()
    pw = secrets.get("smtp_password")
    host, port = (s["host"] or "").strip(), int(s["port"] or 587)
    sec = _mode_for(port, s["security"])
    out = []

    def add(step, ok, detail):
        out.append({"step": step, "ok": ok, "detail": detail})

    add("Settings", bool(host and s["username"] and pw and s["to"]),
        f"server {host or '(blank)'}, port {port}, {sec.upper()}; sign-in {s['username'] or '(blank)'}; "
        f"password {'saved' if pw else 'MISSING'}; send to {', '.join(s['to']) or '(nobody)'}"
        + (f". Port {port} needs {sec.upper()}, not {s['security'].upper()}: it will be used automatically"
           if sec != s["security"] else ""))
    if not host:
        return out
    if s["username"] != s["username"].strip():
        add("Sign-in address", False, "It has a space before or after it; it is trimmed when sending.")
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(host, None)})
        add("Find server", True, f"{host} → {', '.join(ips[:3])}")
    except OSError as e:
        add("Find server", False, f"{e}. {_STEP_HELP['find server']}")
        return out
    for p in dict.fromkeys([port, 587, 465]):
        try:
            socket.create_connection((host, p), timeout=8).close()
            add(f"Open port {p}", True, "reachable")
        except OSError as e:
            add(f"Open port {p}", False, f"blocked or not answering ({e})")
    if not pw:
        return out
    tried = [(port, sec)] + ([_alternative(host, port, sec)] if _alternative(host, port, sec) else [])
    for p, mode in tried:
        try:
            smtp = _open(host, p, mode, s["username"].strip(), pw, timeout=20)
            smtp.quit()
            add(f"Sign in on {p} ({mode.upper()})", True, "connected, encrypted and signed in")
            if (p, mode) != (port, sec):
                add("Fix", True, f"Port {p} with {mode.upper()} works: set the port and security to that, or just send "
                                 f"a test email and Oracle Edge will switch automatically.")
            break
        except SmtpStepError as e:
            add(f"Sign in on {p} ({mode.upper()})", False, explain_error(e))
    return out


def send_test() -> str:
    try:
        send("Oracle Edge · test email", _wrap("<p>Your Oracle Edge email alerts are set up correctly. ✅</p>"))
        s = settings()
        return f"Sent! Check your inbox (via {s['host']}:{s['port']}, {s['security'].upper()})."
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
    ceo_rows = [r for r in rows if r["kind"] == "ceo_change"]
    mkt = [r for r in rows if r["kind"] != "ceo_change"]
    out = ""
    if mkt:
        items = "".join(f"<div style='border-left:4px solid #8A5A12;padding:6px 10px;margin:6px 0'>"
                        f"<span style='color:#8A5A12;font-size:11px;font-weight:700'>MARKET</span> "
                        f"{html.escape(r['message'] or '')}</div>" for r in mkt)
        out += ("<h3 style='margin:18px 0 4px;font-size:15px'>Market temperature</h3><div style='font-size:12px;"
                "color:#666'>Warnings, not hard stops: Oracle Edge keeps following its rules.</div>" + items)
    if ceo_rows:
        items = "".join(f"<div style='border-left:4px solid #2c5d8f;padding:6px 10px;margin:6px 0'>"
                        f"<span style='color:#2c5d8f;font-size:11px;font-weight:700'>CEO</span> "
                        f"{html.escape(r['message'] or '')}</div>" for r in ceo_rows)
        out += ("<h3 style='margin:18px 0 4px;font-size:15px'>CEO changes</h3><div style='font-size:12px;"
                "color:#666'>Found by the daily check (SEC 8-K filings and CEO certifications, Yahoo, Wikidata). A new CEO isn't graded "
                "until they have two fiscal years in the role; their record at other companies still counts.</div>"
                + items)
    return out


def _mark_market(rows) -> None:
    db.get().executemany("UPDATE market_alerts SET emailed=1 WHERE id=?", [(r["id"],) for r in rows])


def _pending_alerts():
    return db.rows("""SELECT a.*, s.ticker FROM price_alerts a JOIN securities s ON s.cik=a.cik
                      WHERE a.emailed=0 AND a.date >= date('now','-5 days') ORDER BY a.state DESC, s.ticker""")


def _calendar_part(s: dict) -> str:
    """The month ahead, once a day (the first email of the day carries it)."""
    from datetime import date

    if not s.get("send_calendar", True) or db.cfg("calendar_sent") == date.today().isoformat():
        return ""
    try:
        from . import econcal

        return econcal.calendar_html()
    except Exception as e:
        db.log("email", "calendar", False, str(e)[:200])
        return ""


def _calendar_sent(part: str) -> None:
    from datetime import date

    if part:
        db.set_cfg("calendar_sent", date.today().isoformat())


def send_digest() -> int:
    s = settings()
    if not s["enabled"]:
        return 0
    alerts = _pending_alerts() if s.get("send_price_alerts", True) else []
    market = _pending_market()
    cal = _calendar_part(s)
    if not s["send_actions"]:
        return _send_alerts_only(alerts, market, cal)
    rows = db.rows(
        """SELECT g.id, g.type, g.reason, g.date, g.price, s.ticker, s.name, e.buy_below, e.strong_buy_below, e.iv,
                  e.rank, e.rank_score, e.quality, e.value AS value_score, e.mos, e.exp_return
           FROM signals g JOIN runs r ON r.id=g.run_id AND r.kind='live'
           JOIN securities s ON s.cik=g.cik
           LEFT JOIN evaluations e ON e.run_id=g.run_id AND e.date=g.date AND e.cik=g.cik
           WHERE g.emailed=0 AND g.date >= date('now','-5 days') ORDER BY g.type, s.ticker""")
    if not rows:
        return _send_alerts_only(alerts, market, cal)
    order = ["STRONG_BUY", "BUY", "ADD", "SELL", "SWAP", "TRIM"]
    rows = sorted(rows, key=lambda r: order.index(r["type"]) if r["type"] in order else 9)
    subject = "Oracle Edge · " + " · ".join(f"{BADGE_LABEL.get(r['type'], r['type'])} {r['ticker']}" for r in rows[:4])
    if len(rows) > 4:
        subject += f" +{len(rows) - 4} more"
    if alerts:
        subject += f" · {len(alerts)} price alert{'s' if len(alerts) > 1 else ''}"
    if market:
        subject += " · market warning"
    send(subject, digest_html(rows, market_html(market) + alerts_html(alerts) + cal))
    _calendar_sent(cal)
    db.get().executemany("UPDATE signals SET emailed=1 WHERE id=?", [(r["id"],) for r in rows])
    _mark_alerts(alerts)
    _mark_market(market)
    return len(rows) + len(alerts) + len(market)


def _mark_alerts(alerts) -> None:
    db.get().executemany("UPDATE price_alerts SET emailed=1 WHERE id=?", [(a["id"],) for a in alerts])


def _send_alerts_only(alerts, market=(), cal: str = "") -> int:
    if not alerts and not market:
        if not cal:
            return 0
        # nothing to act on today: the daily brief is the calendar
        from . import econcal

        nb = econcal.next_big()
        send("Oracle Edge · Daily brief · no actions" + (f" · next: {nb}" if nb else ""),
             _wrap("<p style='margin:0 0 6px'>No buys, sells or alerts today.</p>" + cal))
        _calendar_sent(cal)
        return 1
    if alerts:
        reached = [a for a in alerts if a["state"] == "reached"]
        lead = reached or alerts
        subject = ("Oracle Edge · " + ("Target reached: " if reached else "Approaching target: ") +
                   ", ".join(dict.fromkeys(a["ticker"] for a in lead[:4])))
        if market:
            subject += " · market warning"
    else:
        subject = "Oracle Edge · Market warning: " + (market[0]["message"] or "")[:80]
    send(subject, _wrap(market_html(market) + alerts_html(alerts) + cal))
    _calendar_sent(cal)
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
