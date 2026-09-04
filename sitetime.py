"""The site's calendar day.

The containers run UTC; the site is in Singapore, UTC+8. `date.today()` therefore
returns *yesterday* for the whole 00:00-08:00 local window — which on a
construction site is not a quiet period, it is the night shift and the start of
the day shift. Every use of it was silently wrong for those eight hours:

  - a message logged at 07:00 local was stored under the previous day's
    log_date, so it never appeared in that day's /daily report;
  - /daily before 08:00 built yesterday's report;
  - /excel with no argument exported the wrong month for the first eight hours
    of the 1st;
  - /ask answered "what happened today" for the wrong day.

Singapore has not observed daylight saving since 1935 and its offset is fixed at
+08:00, so a fixed offset is exact here. It also needs no tzdata package, which
python:3.12-slim does not ship. If this bot is ever deployed somewhere with DST,
replace this with zoneinfo.ZoneInfo and add tzdata to the image.
"""

from datetime import date, datetime, timedelta, timezone

SITE_TZ = timezone(timedelta(hours=8), name="Asia/Singapore")


def site_today() -> date:
    """Today's date at the site, not in UTC."""
    return datetime.now(SITE_TZ).date()


def site_now() -> datetime:
    """The current site-local time, timezone-aware."""
    return datetime.now(SITE_TZ)
