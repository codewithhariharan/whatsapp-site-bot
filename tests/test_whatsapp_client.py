"""MIME-type helpers in the WhatsApp transport layer.

The group/1:1 routing tests that used to live here went with `_is_group()`:
every recipient is a group now, so there is nothing left to route between.
"""
import whatsapp_client as wc


class TestMimeFor:
    def test_xlsx_maps_to_openxml_regardless_of_platform(self):
        # Regression guard: mimetypes.guess_type() returns None for .xlsx on a
        # bare Linux container, which made WhatsApp reject the upload. The
        # explicit map must win.
        assert wc._mime_for("Site_Report_June_2026.xlsx") == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    def test_extension_is_case_insensitive(self):
        assert wc._mime_for("REPORT.XLSX") == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    def test_pdf_mime(self):
        assert wc._mime_for("doc.pdf") == "application/pdf"

    def test_unknown_extension_falls_back_to_octet_stream(self):
        assert wc._mime_for("mystery.zzz") == "application/octet-stream"


class TestSendDocumentTimeout:
    def test_document_send_gets_a_longer_timeout_than_text(self):
        """The bridge uploads to WhatsApp before replying, so the document
        timeout has to cover that upload. The full /excel export is ~1.7 MB and
        was silently lost under the 30s default."""
        import inspect

        default = inspect.signature(wc._bridge_post).parameters["timeout"].default
        assert default == 30

        src = inspect.getsource(wc.send_document)
        assert "timeout=180" in src
