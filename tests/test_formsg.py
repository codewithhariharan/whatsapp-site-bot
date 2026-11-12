"""FormSG webhook → Documents, with keys made here in FormSG's format."""
import base64
import io
import json
import time
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nacl.public import Box, PrivateKey
from nacl.signing import SigningKey
from nacl.utils import random

import documents
import formsg
from config import settings

b64 = lambda b: base64.b64encode(bytes(b)).decode()

FORM_KEY = PrivateKey.generate()          # the form's keypair
FORMSG_SIGNER = SigningKey.generate()     # stands in for FormSG's signing key
URL = "https://example.sslip.io/formsg/webhook"
FORM_ID = "64aaaaaaaaaaaaaaaaaaaaaa"
PDF = b"%PDF-1.7\nhello\n%%EOF\n"


def encrypt_content(obj) -> str:
    """FormSG's encryptMessage: '<pub>;<nonce>:<cipher>'."""
    one_off = PrivateKey.generate()
    nonce = random(24)
    cipher = Box(one_off, FORM_KEY.public_key).encrypt(json.dumps(obj).encode(), nonce).ciphertext
    return f"{b64(one_off.public_key)};{b64(nonce)}:{b64(cipher)}"


def encrypt_file(data: bytes) -> dict:
    one_off = PrivateKey.generate()
    nonce = random(24)
    cipher = Box(one_off, FORM_KEY.public_key).encrypt(data, nonce).ciphertext
    return {"encryptedFile": {"submissionPublicKey": b64(one_off.public_key),
                              "nonce": b64(nonce), "binary": b64(cipher)}}


def signature(submission, form=FORM_ID, url=URL, epoch=None, signer=FORMSG_SIGNER):
    epoch = epoch or int(time.time() * 1000)
    sig = signer.sign(f"{url}.{submission}.{form}.{epoch}".encode()).signature
    return f"t={epoch},s={submission},f={form},v1={b64(sig)}"


RESPONSES = [
    {"_id": "f1", "question": "Your name", "fieldType": "textfield", "answer": "Priya"},
    {"_id": "f2", "question": "Email", "fieldType": "email", "answer": "p@lta.gov.sg"},
    {"_id": "f3", "question": "Note", "fieldType": "textarea", "answer": "Rev B drawings"},
    {"_id": "f4", "question": "File", "fieldType": "attachment", "answer": "U3 plan.pdf"},
    {"_id": "f5", "question": "Other file", "fieldType": "attachment", "answer": "virus.exe"},
]


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(settings, "FORMSG_FORM_ID", FORM_ID)
    monkeypatch.setattr(settings, "FORMSG_SECRET_KEY", b64(FORM_KEY))
    monkeypatch.setattr(settings, "FORMSG_WEBHOOK_URL", URL)
    monkeypatch.setattr(settings, "FORMSG_SIGNING_PUBLIC_KEY", b64(FORMSG_SIGNER.verify_key))
    filed = []
    monkeypatch.setattr(formsg, "file_submission", lambda data: filed.append(data))
    app = FastAPI()
    app.include_router(formsg.router)
    c = TestClient(app)
    c.filed = filed
    return c


def _post(client, submission="sub1", header=None, data=None):
    data = data or {"formId": FORM_ID, "submissionId": submission,
                    "encryptedContent": encrypt_content(RESPONSES), "version": 1}
    return client.post("/formsg/webhook", content=json.dumps({"data": data}),
                       headers={"X-FormSG-Signature": signature(submission) if header is None else header,
                                "Content-Type": "application/json"})


def test_a_signed_delivery_is_accepted(setup):
    assert _post(setup).status_code == 200
    assert setup.filed[0]["submissionId"] == "sub1"


def test_off_until_configured(setup, monkeypatch):
    monkeypatch.setattr(settings, "FORMSG_SECRET_KEY", "")
    assert _post(setup).status_code == 404


@pytest.mark.parametrize("header", [
    "",
    "t=1,s=sub1,f=x",                                                  # malformed
    signature("sub1", signer=SigningKey.generate()),                   # not FormSG
    signature("sub1", url="https://elsewhere/formsg/webhook"),         # another URL
    signature("sub1", epoch=int(time.time() * 1000) - 10 * 60 * 1000), # replayed
])
def test_unsigned_or_stale_deliveries_are_refused(setup, header):
    assert _post(setup, header=header).status_code == 401
    assert setup.filed == []


def test_another_form_is_refused(setup):
    r = _post(setup, header=signature("sub1", form="64bbbbbbbbbbbbbbbbbbbbbb"))
    assert r.status_code == 403


def test_payload_must_match_the_signature(setup):
    assert _post(setup, submission="sub1", header=signature("sub2")).status_code == 400


def test_decrypt_and_describe():
    responses = formsg.decrypt_content(b64(FORM_KEY), encrypt_content(RESPONSES))
    assert responses == RESPONSES
    assert formsg.describe(responses) == ("Priya (FormSG)", "Rev B drawings")
    assert formsg.attachment_names(responses) == {"f4": "U3 plan.pdf", "f5": "virus.exe"}
    assert formsg.decrypt_attachment(b64(FORM_KEY), encrypt_file(PDF)) == PDF
    with pytest.raises(ValueError):
        formsg.decrypt_content(b64(PrivateKey.generate()), encrypt_content(RESPONSES))


def test_file_submission_downloads_and_files(monkeypatch):
    monkeypatch.setattr(settings, "FORMSG_SECRET_KEY", b64(FORM_KEY))
    served = {"https://s3/a": encrypt_file(PDF), "https://s3/b": encrypt_file(b"MZ\x90")}

    class Resp:
        def __init__(self, url): self.url = url
        def raise_for_status(self): pass
        def json(self): return served[self.url]

    class Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url): return Resp(url)

    stored = []
    monkeypatch.setattr(formsg.httpx, "Client", Client)
    monkeypatch.setattr(documents, "store_bytes",
                        lambda name, data, note, by, source: stored.append(
                            (name, data, note, by, source)) or {"id": 1})
    n = formsg.file_submission({"submissionId": "sub1",
                                "encryptedContent": encrypt_content(RESPONSES),
                                "attachmentDownloadUrls": {"f4": "https://s3/a",
                                                           "f5": "https://s3/b"}})
    assert n == 2   # store_bytes (stubbed) decides; the real one refuses the .exe
    assert stored[0] == ("U3 plan.pdf", PDF, "Rev B drawings", "Priya (FormSG)",
                         "formsg:sub1:f4")


def test_store_bytes_applies_upload_rules(monkeypatch, tmp_path):
    rows = []

    def fetch(q, p=()):
        if q.startswith("SELECT 1"):
            return [1] if any(r["source"] == p[0] for r in rows) else []
        if q.startswith("SELECT COALESCE"):
            return [{"n": 0}]
        if q.startswith("INSERT"):
            rows.append({"source": p[7], "id": len(rows) + 1, "filename": p[0]})
            return [rows[-1]]
        raise AssertionError(q)

    monkeypatch.setattr(documents.db, "_fetch", fetch)
    monkeypatch.setattr(settings, "DASHBOARD_DOCS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DASHBOARD_DOCS_MIN_FREE_MB", 0)

    assert documents.store_bytes("virus.exe", b"MZ", "", "x", "s:1") is None
    assert documents.store_bytes("fake.pdf", b"MZ not pdf", "", "x", "s:2") is None
    assert documents.store_bytes("plan.pdf", PDF, "n", "Priya (FormSG)", "s:3")["id"] == 1
    assert documents.store_bytes("plan.pdf", PDF, "n", "Priya (FormSG)", "s:3") is None  # repeat
    assert len(list(tmp_path.iterdir())) == 1
