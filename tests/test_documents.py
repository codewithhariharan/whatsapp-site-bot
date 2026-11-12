import io
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import documents
from config import settings

PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\n%%EOF\n"


def _xlsx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", "<workbook/>")
    return buf.getvalue()


USER = {"id": 7, "email": "sam@lta.gov.sg", "name": "Sam", "is_admin": False,
        "must_change_password": False}


class FakeTable:
    """dashboard_documents in memory, answering the few statements documents.py runs."""

    def __init__(self):
        self.rows: list[dict] = []

    def fetch(self, query, params=()):
        if query.startswith("SELECT COALESCE(SUM"):
            return [{"n": sum(r["size_bytes"] for r in self.rows)}]
        if query.startswith("INSERT"):
            keys = ("filename", "kind", "size_bytes", "sha256", "storage_key", "note",
                    "uploaded_by_id", "uploaded_by")
            row = dict(zip(keys, params), id=len(self.rows) + 1, uploaded_at="2026-11-12T10:00:00+08:00")
            self.rows.append(row)
            return [row]
        if "WHERE id = %s" in query:
            return [r for r in self.rows if r["id"] == params[0]]
        return list(self.rows)

    def execute(self, query, params=()):
        assert query.startswith("DELETE")
        self.rows = [r for r in self.rows if r["id"] != params[0]]


@pytest.fixture
def table(monkeypatch, tmp_path):
    t = FakeTable()
    monkeypatch.setattr(documents.db, "_fetch", t.fetch)
    monkeypatch.setattr(documents.db, "_execute", t.execute)
    monkeypatch.setattr(settings, "DASHBOARD_DOCS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DASHBOARD_DOCS_MIN_FREE_MB", 0)
    monkeypatch.setattr(documents, "_upload_limiter", documents.RateLimiter(100, 600, 1000))
    t.dir = tmp_path
    return t


def _client(user=None):
    app = FastAPI()
    app.include_router(documents.router)
    if user:
        app.dependency_overrides[documents.current_user] = lambda: user
        app.dependency_overrides[documents.optional_user] = lambda: user
    return TestClient(app)


def _upload(client, name, data, note=""):
    return client.post("/api/documents", params={"filename": name, "note": note}, content=data,
                       headers={"Content-Type": "application/octet-stream"})


def test_upload_needs_a_login(table):
    assert _upload(_client(), "a.pdf", PDF).status_code == 401
    assert table.rows == []


def test_upload_list_and_download(table):
    c = _client(USER)
    r = _upload(c, "Rev B drawings.pdf", PDF, note="U3")
    assert r.status_code == 200, r.text
    doc = r.json()["document"]
    assert doc["filename"] == "Rev B drawings.pdf" and doc["can_delete"]

    # Stored under a random name, not the visitor's.
    [stored] = list(table.dir.iterdir())
    assert stored.name == table.rows[0]["storage_key"] and stored.read_bytes() == PDF

    # Anyone can list and fetch it; a visitor cannot remove it.
    public = _client()
    listed = public.get("/api/documents").json()["documents"]
    assert [d["filename"] for d in listed] == ["Rev B drawings.pdf"]
    assert listed[0]["can_delete"] is False
    got = public.get(f"/api/documents/{doc['id']}")
    assert got.status_code == 200 and got.content == PDF
    assert got.headers["content-type"] == "application/pdf"
    assert got.headers["content-disposition"].startswith("inline")
    assert public.get(f"/api/documents/{doc['id']}?download=1").headers[
        "content-disposition"].startswith("attachment")


def test_excel_is_always_a_download(table):
    c = _client(USER)
    doc = _upload(c, "progress.xlsx", _xlsx()).json()["document"]
    r = c.get(f"/api/documents/{doc['id']}")
    assert r.headers["content-disposition"].startswith("attachment")


@pytest.mark.parametrize("name,data", [
    ("setup.exe", b"MZ\x90\x00"),
    ("page.html", b"<script>alert(1)</script>"),
    ("fake.pdf", b"MZ\x90\x00 not a pdf"),
    ("fake.xlsx", b"PK\x03\x04 not really a zip"),
    ("other.xlsx", None),          # a real zip, but not a workbook
    ("empty.pdf", b""),
])
def test_only_real_pdfs_and_workbooks(table, name, data):
    if data is None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("evil.js", "alert(1)")
        data = buf.getvalue()
    r = _upload(_client(USER), name, data)
    assert r.status_code == 400
    assert table.rows == [] and list(table.dir.iterdir()) == []


def test_size_cap_while_streaming(table, monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_DOC_MAX_MB", 1)
    big = PDF + b"0" * (1024 * 1024)
    r = _upload(_client(USER), "big.pdf", big)
    assert r.status_code == 413
    assert table.rows == [] and list(table.dir.iterdir()) == []


def test_total_cap(table, monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_DOCS_TOTAL_MB", 0)
    assert _upload(_client(USER), "a.pdf", PDF).status_code == 507


def test_only_uploader_or_admin_removes(table):
    doc = _upload(_client(USER), "a.pdf", PDF).json()["document"]
    other = dict(USER, id=8, email="kim@lta.gov.sg")
    assert _client(other).delete(f"/api/documents/{doc['id']}").status_code == 403
    admin = dict(other, is_admin=True)
    assert _client(admin).delete(f"/api/documents/{doc['id']}").status_code == 200
    assert table.rows == [] and list(table.dir.iterdir()) == []


@pytest.mark.parametrize("raw,clean", [
    ("../../etc/passwd.pdf", "passwd.pdf"),
    ("C:\\Users\\x\\report.xlsx", "report.xlsx"),
    ("a\x00b\nc.pdf", "abc.pdf"),
    ("x" * 300 + ".pdf", "x" * 146 + ".pdf"),
])
def test_clean_filename(raw, clean):
    assert documents.clean_filename(raw) == clean


def test_formsg_link_only_for_people_who_may_upload(table, monkeypatch):
    monkeypatch.setattr(settings, "FORMSG_FORM_ID", "64abc")
    assert _client().get("/api/documents").json()["form_url"] is None
    assert _client(USER).get("/api/documents").json()["form_url"] == "https://form.gov.sg/64abc"
    monkeypatch.setattr(settings, "FORMSG_FORM_ID", "")
    assert _client(USER).get("/api/documents").json()["form_url"] is None


def _docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<document/>")
    return buf.getvalue()


@pytest.mark.parametrize("name,data", [
    ("minutes.docx", None),
    ("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100),
    ("notes.txt", "Pour at U3 — 14:00\r\nRebar ✓\n".encode()),
    ("bom.txt", b"\xef\xbb\xbfhello"),
])
def test_word_and_text_are_accepted(table, name, data):
    r = _upload(_client(USER), name, _docx() if data is None else data)
    assert r.status_code == 200, r.text
    got = _client().get(f"/api/documents/{r.json()['document']['id']}")
    assert got.headers["content-disposition"].startswith("attachment")


@pytest.mark.parametrize("name,data", [
    ("binary.txt", b"MZ\x90\x00\x03"),
    ("latin1.txt", "caf\xe9".encode("latin-1")),
    ("workbook.docx", None),       # an Excel zip renamed .docx
])
def test_fake_word_and_text_are_refused(table, name, data):
    r = _upload(_client(USER), name, _xlsx() if data is None else data)
    assert r.status_code == 400
