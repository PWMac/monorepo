"""Profile descriptions and the email connection fallbacks."""

from __future__ import annotations

import smtplib

from oracle_edge import db, emailer, rules


def _seeded():
    db.init_db() if hasattr(db, "init_db") else db.get()
    rules.seed_profiles()


def test_describe_and_differences_are_built_from_settings():
    _seeded()
    classic = rules.get_profile(db.one("SELECT id FROM profiles WHERE name='Oracle Classic'")["id"])
    d = rules.describe(classic.body)
    assert "Quality is at least 70" in d and "25% or more below value" in d and "at least 10%" in d
    assert "best 15" in d
    b = classic.body.copy()
    b = {**classic.body, "settings": {**classic.settings, "hurdle": 0.2, "max_holdings": 7, "max_position": 0.35}}
    assert "At most 35% per stock" in rules.describe(b)
    diffs = rules.differences(b, classic.body)
    assert "expected-return bar 20% (was 10%)" in diffs and "holdings 7 (was 15)" in diffs
    assert "max per stock 35% (was off)" in diffs


def test_copies_get_no_placeholder_and_old_notes_are_replaced_but_user_notes_kept():
    _seeded()
    b15 = db.one("SELECT id FROM profiles WHERE name='Buffettology 15%'")["id"]
    new = rules.save_as(b15, "Buffettology 20% test")
    assert rules.get_profile(new).notes == ""
    assert rules.parent_of(new).name == "Buffettology 15%"
    db.get().execute("UPDATE profiles SET notes='Copy of Buffettology 15%' WHERE id=?", (new,))
    db.get().execute("UPDATE profiles SET notes='Stricter: Mary Buffett''s 15% expected-return bar.' WHERE id=?", (b15,))
    rules.refresh_notes()
    assert rules.get_profile(new).notes == ""
    assert rules.get_profile(b15).notes.startswith("Oracle Classic with a higher bar")
    rules.set_notes(new, "My experiment")
    rules.refresh_notes()
    assert rules.get_profile(new).notes == "My experiment"


def test_port_decides_the_encryption():
    assert emailer._mode_for(465, "starttls") == "ssl"
    assert emailer._mode_for(587, "ssl") == "starttls"
    assert emailer._mode_for(2526, "ssl") == "ssl"
    assert emailer._alternative("h", 587, "starttls") == (465, "ssl")


def test_hang_up_on_587_falls_back_to_465_and_remembers(monkeypatch):
    _seeded()
    emailer.save_settings({**emailer.DEFAULTS, "enabled": True, "username": " me@example.com ",
                           "to": ["me@example.com"]})
    calls = []

    class Fake:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send_message(self, m):
            calls.append(("sent", m["From"]))

    def fake_open(host, port, sec, user, pw, timeout=30):
        calls.append((port, sec, user))
        if port == 587:
            raise emailer.SmtpStepError("greeting", smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))
        return Fake()

    monkeypatch.setattr(emailer, "_open", fake_open)
    monkeypatch.setattr(emailer.secrets, "get", lambda k: "pw")
    emailer.send("s", "<p>x</p>")
    assert calls[0] == (587, "starttls", "me@example.com") and calls[1][:2] == (465, "ssl")
    assert calls[2] == ("sent", "Oracle Edge <me@example.com>")
    assert emailer.settings()["port"] == 465 and emailer.settings()["security"] == "ssl"


def test_sign_in_failure_is_not_retried_and_is_explained(monkeypatch):
    _seeded()
    emailer.save_settings({**emailer.DEFAULTS, "enabled": True, "username": "me@example.com", "to": ["a@b.c"]})
    tried = []

    def fake_open(host, port, sec, user, pw, timeout=30):
        tried.append(port)
        raise emailer.SmtpStepError("sign-in", smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password "
                                                                                     b"not accepted"))

    monkeypatch.setattr(emailer, "_open", fake_open)
    monkeypatch.setattr(emailer.secrets, "get", lambda k: "pw")
    try:
        emailer.send_test()
    except RuntimeError as e:
        assert "535-5.7.8" in str(e)
    assert tried == [587]


def _fake_server(reply_to_auth: bytes):
    """A one-connection SMTP server: offers AUTH PLAIN and LOGIN, refuses the sign-in, then hangs up."""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    seen = []

    def run():
        c, _ = srv.accept()
        f = c.makefile("rwb")
        f.write(b"220 fake ESMTP\r\n")
        f.flush()
        for line in f:
            cmd = line.decode().strip()
            seen.append(cmd.split()[0].upper() if cmd else "")
            if cmd.upper().startswith(("EHLO", "HELO")):
                f.write(b"250-fake\r\n250-STARTTLS\r\n250 AUTH LOGIN PLAIN XOAUTH2\r\n")
            elif cmd.upper().startswith("AUTH"):
                f.write(reply_to_auth)
                f.flush()
                break  # hang up right after refusing, like Gmail
            else:
                f.write(b"250 ok\r\n")
            f.flush()
        c.close()
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1], seen


def test_refused_sign_in_shows_the_servers_reason(monkeypatch):
    monkeypatch.setattr(emailer._SMTP, "starttls", lambda self, context=None: (220, b"ok"))
    port, seen = _fake_server(b"535-5.7.8 Username and Password not accepted. For more information, go to\r\n"
                              b"535 5.7.8  https://support.google.com/mail/?p=BadCredentials x\r\n")
    try:
        emailer._open("127.0.0.1", port, "starttls", "me@example.com", "pw", timeout=5)
        raise AssertionError("should have failed")
    except emailer.SmtpStepError as e:
        assert e.step == "sign-in" and "5.7.8" in e.server
        assert "535-5.7.8" in emailer.explain_error(e)
    assert seen.count("AUTH") == 1  # one method only, so the real reason isn't lost


def test_silent_hang_up_during_sign_in_is_explained(monkeypatch):
    monkeypatch.setattr(emailer._SMTP, "starttls", lambda self, context=None: (220, b"ok"))
    port, _ = _fake_server(b"")
    try:
        emailer._open("127.0.0.1", port, "starttls", "me@example.com", "pw", timeout=5)
        raise AssertionError("should have failed")
    except emailer.SmtpStepError as e:
        msg = emailer.explain_error(e)
        assert e.step == "sign-in" and "app password" in msg and "wait an hour" in msg
