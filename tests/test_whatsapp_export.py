"""WhatsApp "Export chat" parsing used by the backfill script.

Pure text handling — no network, no database. The cases that matter are the
ones that would silently corrupt a backfill: multi-line site logs getting
split, and DD/MM being read as MM/DD (which would scatter rows across the
wrong dates).
"""
from datetime import date, datetime

import pytest

from whatsapp_export import (
    detect_date_order,
    is_placeholder,
    parse_export,
    strip_attachment_marker,
)

ANDROID_LOG = """10/07/2026, 09:15 - Rajesh: Main Location: Zone 3, S2-2
Sub Location: GL A-B/20
Description: Honeycomb rectification works
Manpower: Worker - 1
10/07/2026, 09:20 - Siti: noted
"""

IOS_LOG = """[10/07/2026, 09:15:32] Rajesh: Main Location: Zone 3
[10/07/2026, 09:20:01] Siti: noted
"""


def test_parses_android_format():
    messages = parse_export(ANDROID_LOG)
    assert len(messages) == 2
    assert messages[0].sender == "Rajesh"
    assert messages[0].timestamp == datetime(2026, 7, 10, 9, 15)


def test_parses_ios_format():
    messages = parse_export(IOS_LOG)
    assert len(messages) == 2
    assert messages[0].sender == "Rajesh"
    assert messages[0].timestamp == datetime(2026, 7, 10, 9, 15, 32)


def test_multiline_message_stays_one_message():
    # A site log spans four lines; splitting it would destroy the record.
    body = parse_export(ANDROID_LOG)[0].text
    assert body.startswith("Main Location: Zone 3, S2-2")
    assert "Manpower: Worker - 1" in body
    assert len(body.splitlines()) == 4


def test_twelve_hour_times():
    messages = parse_export("10/07/2026, 2:05 pm - Rajesh: afternoon pour done\n")
    assert messages[0].timestamp == datetime(2026, 7, 10, 14, 5)


def test_midnight_and_noon_meridiem():
    messages = parse_export(
        "10/07/2026, 12:30 am - A: midnight shift start\n"
        "10/07/2026, 12:30 pm - A: noon shift start\n"
    )
    assert messages[0].timestamp.hour == 0
    assert messages[1].timestamp.hour == 12


def test_system_notices_are_dropped():
    # No "Sender: " prefix — these are group events, not messages.
    text = (
        "10/07/2026, 09:00 - Messages and calls are end-to-end encrypted.\n"
        "10/07/2026, 09:01 - Rajesh added Siti\n"
        "10/07/2026, 09:02 - Rajesh: Main Location: Zone 3 works ongoing\n"
    )
    messages = parse_export(text)
    assert len(messages) == 1
    assert messages[0].sender == "Rajesh"


def test_system_notice_does_not_absorb_following_lines():
    # A notice must terminate the previous message, not extend it.
    text = (
        "10/07/2026, 09:02 - Rajesh: Main Location: Zone 3\n"
        "10/07/2026, 09:03 - Siti joined using this group's invite link\n"
        "stray continuation line\n"
    )
    messages = parse_export(text)
    assert len(messages) == 1
    assert "stray continuation" not in messages[0].text


def test_media_placeholders_are_dropped():
    text = (
        "10/07/2026, 09:00 - Rajesh: <Media omitted>\n"
        "10/07/2026, 09:01 - Rajesh: This message was deleted\n"
        "10/07/2026, 09:02 - Rajesh: Main Location: Zone 3 works ongoing\n"
    )
    assert len(parse_export(text)) == 1


@pytest.mark.parametrize("text", ["  ", "This message was deleted", "null"])
def test_is_placeholder(text):
    assert is_placeholder(text)


def test_media_marker_is_not_a_placeholder():
    # It is stripped, not dropped — otherwise the caption behind it is lost.
    # An uncaptioned photo becomes empty and is dropped a step later.
    assert not is_placeholder("<Media omitted>")


# ── Captions survive their media ──────────────────────────────────────────────
# WhatsApp exports the caption on the line AFTER the media marker, and keeps it
# even when the photo itself is long gone from the phone. Dropping the whole
# message because line 1 says "<Media omitted>" destroyed 356 lines of real site
# logs across the July outage — the caption is the only thing that matters here.

def test_caption_after_media_omitted_is_kept():
    text = (
        "13/07/2026, 10:56 - Amira: <Media omitted>\n"
        "P33: Curing for crosshead in progress\n"
    )
    messages = parse_export(text)
    assert len(messages) == 1
    assert messages[0].text == "P33: Curing for crosshead in progress"


def test_multiline_caption_after_media_omitted_is_kept():
    text = (
        "21/07/2026, 09:00 - Amira: <Media omitted>\n"
        "CCW2 (South side)\n"
        "U3-39: Rebar works in progress\n"
        "Manpower: 8 men\n"
    )
    assert parse_export(text)[0].text == (
        "CCW2 (South side)\nU3-39: Rebar works in progress\nManpower: 8 men"
    )


@pytest.mark.parametrize("marker", [
    "<Media omitted>", "image omitted", "video omitted", "sticker omitted",
    "audio omitted", "document omitted", "GIF omitted", "Contact card omitted",
])
def test_all_media_markers_yield_their_caption(marker):
    text = f"13/07/2026, 10:56 - Amira: {marker}\nP33: Curing in progress\n"
    assert parse_export(text)[0].text == "P33: Curing in progress"


@pytest.mark.parametrize("marker", ["<Media omitted>", "image omitted"])
def test_uncaptioned_media_is_still_dropped(marker):
    assert parse_export(f"13/07/2026, 10:56 - Amira: {marker}\n") == []


def test_deleted_messages_have_no_caption_to_recover():
    # A tombstone is not a media marker: nothing follows it worth keeping.
    text = "13/07/2026, 15:04 - Amira: This message was deleted\n"
    assert parse_export(text) == []


def test_caption_mentioning_media_omitted_mid_text_is_untouched():
    text = "13/07/2026, 10:56 - Amira: note: <Media omitted> appears in exports\n"
    assert parse_export(text)[0].text == "note: <Media omitted> appears in exports"


# ── Captions on photos ────────────────────────────────────────────────────────
# In this project the site logs are typed as photo captions, so an export made
# *with* media is the only one that carries them. The attachment marker must be
# stripped while the caption survives intact.

def test_android_photo_caption_is_kept():
    text = (
        "27/07/2026, 16:02 - Hariharan: IMG-20260727-WA0012.jpg (file attached)\n"
        "CCW\n"
        "South side\n"
        "Hacking of tunnel eye\n"
        "5 men\n"
    )
    messages = parse_export(text)
    assert len(messages) == 1
    assert messages[0].text == "CCW\nSouth side\nHacking of tunnel eye\n5 men"


def test_ios_photo_caption_is_kept():
    text = (
        "[27/07/2026, 16:02:11] Hariharan: ‎<attached: 00000042-PHOTO-2026-07-27.jpg>\n"
        "CCW\n"
        "North side\n"
    )
    messages = parse_export(text)
    assert len(messages) == 1
    assert messages[0].text == "CCW\nNorth side"


@pytest.mark.parametrize("marker", [
    "IMG-20260727-WA0012.jpg (file attached)",
    "VID-20260727-WA0003.mp4 (file attached)",
    "<attached: 00000042-PHOTO-2026-07-27.jpg>",
])
def test_uncaptioned_attachments_are_dropped(marker):
    assert parse_export(f"27/07/2026, 16:02 - Hariharan: {marker}\n") == []


def test_strip_attachment_marker_leaves_plain_text_alone():
    assert strip_attachment_marker("Main Location: Zone 3") == "Main Location: Zone 3"


def test_caption_mentioning_a_filename_is_not_mistaken_for_a_marker():
    # Only a line that is *nothing but* a marker should be stripped.
    text = "27/07/2026, 16:02 - Hariharan: see report.pdf for the panel details\n"
    assert parse_export(text)[0].text == "see report.pdf for the panel details"


def test_detects_day_first_order():
    assert detect_date_order(["27/07/2026, 09:15 - A: hi"]) == "DMY"


def test_detects_month_first_order():
    assert detect_date_order(["07/27/2026, 09:15 - A: hi"]) == "MDY"


def test_ambiguous_dates_default_to_day_first():
    # 07/07 reads the same either way; DMY is the non-US default.
    assert detect_date_order(["07/07/2026, 09:15 - A: hi"]) == "DMY"


def test_date_order_applies_to_ambiguous_dates():
    # The whole file shares one order, decided by the unambiguous 27/07 line.
    text = "05/07/2026, 09:15 - A: first\n27/07/2026, 09:15 - A: second\n"
    assert [m.log_date for m in parse_export(text)] == [
        date(2026, 7, 5), date(2026, 7, 27),
    ]


def test_explicit_date_order_overrides_detection():
    messages = parse_export("05/07/2026, 09:15 - A: hi there team", date_order="MDY")
    assert messages[0].log_date == date(2026, 5, 7)


def test_two_digit_year():
    messages = parse_export("10/07/26, 09:15 - A: hi there team\n")
    assert messages[0].log_date == date(2026, 7, 10)


def test_ios_directional_marks_are_stripped():
    # iOS exports embed U+200E around the timestamp and before attachments.
    messages = parse_export("‎[10/07/2026, 09:15:32] Rajesh: ‎Main Location: Zone 3\n")
    assert messages[0].sender == "Rajesh"
    assert messages[0].text == "Main Location: Zone 3"


def test_empty_input():
    assert parse_export("") == []
