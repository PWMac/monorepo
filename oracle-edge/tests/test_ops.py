"""Backup/restore, email (fake SMTP), scheduler time math."""

from __future__ import annotations

import smtplib
import zipfile

from oracle_edge import backup, db, emailer, secrets


def test_backup_and_restore(tmp_path):
    db.get()
    db.set_cfg("marker", "before")
    target = tmp_path / "Backups"
    path = backup.run_backup(target)
    assert path.endswith(".db.zip")
    assert len(list(target.glob("oracle_edge_*.db.zip"))) == 1  # stored once, not once per tier
    with zipfile.ZipFile(path) as z:
        assert {"oracle_edge.db", "profiles.json", "settings.json"} <= set(z.namelist())
    assert not list(target.rglob("*.part"))  # no half-written files left for the cloud to sync
    db.set_cfg("marker", "after")
    backup.restore(path)
    # restore swaps the file underneath; a fresh connection sees the old value
    con = db.connect()
    import json

    assert json.loads(con.execute("SELECT value_json FROM config WHERE key='marker'").fetchone()[0]) == "before"


def test_retention_prunes(tmp_path):
    from datetime import date

    t = tmp_path / "B"
    t.mkdir()
    days = [date(2026, 1, i + 1) for i in range(20)]
    for d in days:
        (t / f"oracle_edge_{d.isoformat()}.db.zip").write_bytes(b"x")
    backup.prune(t)
    left = sorted(p.name for p in t.glob("*.zip"))
    assert len(left) == len(backup.keep_set(days, backup.retention())) < 20
    assert "oracle_edge_2026-01-20.db.zip" in left and "oracle_edge_2026-01-01.db.zip" not in left


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=30, context=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, u, p):
        if p != "abcdabcdabcdabcd":
            raise smtplib.SMTPAuthenticationError(534, b"5.7.9 Application-specific password required")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_email_send_and_errors(monkeypatch):
    db.get()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    emailer.save_settings({**emailer.DEFAULTS, "enabled": True, "username": "owner@example.com", "to": ["owner@example.com"]},
                          "abcd abcd abcd abcd")
    assert secrets.get("smtp_password") == "abcdabcdabcdabcd"  # spaces from Google's display removed
    assert "Sent" in emailer.send_test()
    assert FakeSMTP.sent[-1]["From"] == "Oracle Edge <owner@example.com>"
    secrets.set("smtp_password", "wrong")
    try:
        emailer.send_test()
        raise AssertionError("should fail")
    except RuntimeError as e:
        assert "app password" in str(e)


def test_digest_html_contains_badges():
    rows = [{"type": "STRONG_BUY", "ticker": "ABC", "name": "Abc Co", "reason": "Rank #1", "buy_below": 98.0,
             "strong_buy_below": 79.0, "iv": 131.0, "price": 92.0}]
    h = emailer.digest_html(rows)
    assert "STRONG BUY" in h and "$98.00" in h and "ABC" in h


def test_scheduler_earliest_time_without_dst(monkeypatch):
    import os
    import time

    from oracle_edge import scheduler

    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Regina"
    time.tzset()
    try:
        db.set_cfg("run_time_et", "18:00")
        assert scheduler.earliest_local_time() == "16:00"  # 6 PM EDT = 4 PM in Regina (no DST)
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
