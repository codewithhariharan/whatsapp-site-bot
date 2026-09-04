"""The site's calendar day is Singapore's, not the container's UTC.

The containers run UTC while the site is UTC+8, so date.today() returned
*yesterday* for the whole 00:00-08:00 local window — the night shift and the
start of the day shift. Entries logged then were filed under the previous day,
/daily built the wrong report, and /ask answered for the wrong date.
"""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import sitetime


def _at_utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


class TestSiteToday:
    def _today_when_utc_is(self, moment):
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment.astimezone(tz) if tz else moment

        with patch.object(sitetime, "datetime", FrozenDatetime):
            return sitetime.site_today()

    def test_the_bug_midnight_to_8am_local_is_a_new_day(self):
        # 01:00 Singapore on the 4th is 17:00 UTC on the 3rd.
        moment = _at_utc(2026, 9, 3, 17, 0)
        assert moment.date() == date(2026, 9, 3)          # what UTC thinks
        assert self._today_when_utc_is(moment) == date(2026, 9, 4)   # what the site thinks

    def test_just_before_local_midnight_is_still_the_old_day(self):
        # 23:59 Singapore on the 3rd is 15:59 UTC on the 3rd.
        assert self._today_when_utc_is(_at_utc(2026, 9, 3, 15, 59)) == date(2026, 9, 3)

    def test_exactly_local_midnight_rolls_over(self):
        # 00:00 Singapore on the 4th is 16:00 UTC on the 3rd.
        assert self._today_when_utc_is(_at_utc(2026, 9, 3, 16, 0)) == date(2026, 9, 4)

    def test_daytime_agrees_with_utc(self):
        # 11:00 Singapore on the 4th is 03:00 UTC the same day — the window in
        # which the bug was invisible, which is why it survived so long.
        assert self._today_when_utc_is(_at_utc(2026, 9, 4, 3, 0)) == date(2026, 9, 4)

    def test_month_boundary(self):
        # 00:30 Singapore on 1 Oct is 16:30 UTC on 30 Sep — /excel with no
        # argument would have exported September.
        assert self._today_when_utc_is(_at_utc(2026, 9, 30, 16, 30)) == date(2026, 10, 1)

    def test_year_boundary(self):
        assert self._today_when_utc_is(_at_utc(2026, 12, 31, 16, 0)) == date(2027, 1, 1)


class TestOffset:
    def test_is_utc_plus_8(self):
        assert sitetime.SITE_TZ.utcoffset(None) == timedelta(hours=8)

    def test_site_now_is_timezone_aware(self):
        # A naive datetime here would silently compare wrong against DB values.
        assert sitetime.site_now().tzinfo is not None


class TestCallersUseIt:
    """The helper is worthless if a caller still reaches for date.today()."""

    def test_no_module_calls_date_today_directly(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.glob("*.py"):
            if path.name == "sitetime.py":
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if "date.today()" in code:
                    offenders.append(f"{path.name}:{i}")
        assert not offenders, f"use sitetime.site_today() instead: {offenders}"
