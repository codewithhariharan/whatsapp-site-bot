import io
from datetime import date, datetime

import pytest
from openpyxl import load_workbook

import mailer
import record_export
from config import settings
from sitetime import SITE_TZ

SITE = "120363021760406818@g.us"
TUNNEL = "120363412278288115@g.us"

ROWS = {
    "daily_logs": [{"log_date": "2026-11-02", "logged_at": "2026-11-02T13:01:23+00:00",
                    "main_location": "Zone 2 P6", "sub_location": "", "manpower": "",
                    "description": "casting\x07 done", "sender_name": "Vicky"}],
    "dwall_panels": [],
    "tunnel_progress": [{"report_date": "2026-10-28", "contract": "CR146",
                         "drive": "EB - Main Drive 3", "pct_complete": 25.9}],
    "tunnel_flags": [{"sent_date": "2026-10-28", "flag_text": "HARD ROCK",
                      "contract": "CR125", "downtime": None}],
}


@pytest.fixture
def fake_db(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_SITE_GROUP_ID", SITE)
    monkeypatch.setattr(settings, "TUNNEL_GROUP_IDS", TUNNEL)
    queries = []

    def fetch(query, params=()):
        text = repr(query)
        queries.append(params)
        for name, rows in ROWS.items():
            if f"'{name}'" in text:
                return rows
        raise AssertionError(text)

    monkeypatch.setattr(record_export.db, "_fetch", fetch)
    return queries


def _load(data):
    return load_workbook(io.BytesIO(data))


def test_workbook_has_one_table_per_sheet(fake_db):
    data, counts = record_export.build_workbook(datetime(2026, 11, 3, 12, tzinfo=SITE_TZ))
    wb = _load(data)
    assert wb.sheetnames == ["About", "Site logs", "D-wall panels", "Tunnel progress",
                             "Flagged items"]
    assert counts == {"Site logs": 1, "D-wall panels": 0, "Tunnel progress": 1,
                      "Flagged items": 1}
    assert set(wb["Site logs"].tables) == {"SiteLogs"}
    # An empty sheet keeps its header but no table (Excel refuses an empty one).
    assert not wb["D-wall panels"].tables
    assert wb["D-wall panels"]["A1"].value == "Panel number"


def test_dates_are_real_dates_in_site_time(fake_db):
    data, _ = record_export.build_workbook()
    ws = _load(data)["Site logs"]
    headers = [c.value for c in ws[1]]
    row = {h: c.value for h, c in zip(headers, ws[2])}
    assert row["Date"].date() == date(2026, 11, 2)
    assert row["Logged"] == datetime(2026, 11, 2, 21, 1, 23)   # 13:01 UTC = 9:01pm SGT
    assert row["Description"] == "casting done"                # control char dropped


def test_scoped_like_the_dashboard(fake_db):
    record_export.build_workbook()
    assert [SITE] == list(fake_db[0][:1])
    assert [TUNNEL] in [list(p[:1])[0] if isinstance(p[0], list) else None
                        for p in fake_db]


def test_no_phone_numbers(fake_db):
    data, _ = record_export.build_workbook()
    for ws in _load(data).worksheets[1:]:
        assert "Sender number" not in [c.value for c in ws[1]]


def test_send_attaches_the_fixed_filename(fake_db, monkeypatch):
    sent = {}
    monkeypatch.setattr(mailer, "send_email",
                        lambda to, subject, text, html=None, attachments=None:
                        sent.update(to=to, subject=subject, attachments=attachments))
    record_export.send_record(datetime(2026, 11, 3, 18, tzinfo=SITE_TZ), ["a@lta.gov.sg"])
    assert sent["to"] == ["a@lta.gov.sg"]
    assert sent["subject"] == "[Site Record] Updated 3 Nov 2026, 6:00 PM"
    (name, data, mimetype), = sent["attachments"]
    assert name == "Site_Record.xlsx" and data[:2] == b"PK" and mimetype == record_export.XLSX


def test_mailer_adds_the_attachment(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "SMTP_USER", "")
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): pass
        def send_message(self, msg): sent.append(msg)

    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    mailer.send_email(["a@b.sg"], "S", "body",
                      attachments=[("Site_Record.xlsx", b"PK\x03\x04", record_export.XLSX)])
    (part,) = list(sent[0].iter_attachments())
    assert part.get_filename() == "Site_Record.xlsx"
    assert part.get_content() == b"PK\x03\x04"
