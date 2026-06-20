"""Routing and MIME-type helpers in the WhatsApp transport layer."""
import whatsapp_client as wc


class TestIsGroup:
    def test_group_jid_is_group(self):
        assert wc._is_group("120363041234567890@g.us") is True

    def test_bare_phone_number_is_not_group(self):
        assert wc._is_group("6588257614") is False

    def test_individual_jid_is_not_group(self):
        assert wc._is_group("6588257614@s.whatsapp.net") is False


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
