"""Explicit source dates. Numeric dates use UK day/month/year or ISO year-month-day."""

import calendar
import re
from datetime import date

MONTH_NUMBERS = {
    name.lower(): i
    for i in range(1, 13)
    for name in (calendar.month_name[i], calendar.month_abbr[i])
}
MONTH_NUMBERS["sept"] = 9
MONTH_PATTERN = "(?:" + "|".join(sorted(MONTH_NUMBERS, key=len, reverse=True)) + ")"
DATE_PATTERN = re.compile(
    r"(?<![\w/.-])(?:"
    r"(?P<iy>20\d{2})\s*-\s*(?P<im>\d{1,2})\s*-\s*(?P<id>\d{1,2})"
    r"|(?P<d>\d{1,2})\s*(?P<sep>[/.-])\s*(?P<m>\d{1,2})\s*(?P=sep)\s*(?P<y>20\d{2})"
    r"|(?P<wd>\d{1,2})\s+(?P<month>" + MONTH_PATTERN + r")\.?\s+(?P<wy>20\d{2})"
    r")(?![\w/.-])",
    re.I,
)


def date_matches(text):
    for match in DATE_PATTERN.finditer(text):
        g = match.groupdict()
        try:
            if g["iy"]:
                value = date(int(g["iy"]), int(g["im"]), int(g["id"]))
            elif g["y"]:
                value = date(int(g["y"]), int(g["m"]), int(g["d"]))
            else:
                value = date(int(g["wy"]), MONTH_NUMBERS[g["month"].lower()], int(g["wd"]))
        except ValueError:
            continue
        yield match, value.isoformat()


def full_dates(text):
    return [value for _, value in date_matches(text)]


def standalone_date(text):
    text = text.strip()
    return next(
        (value for m, value in date_matches(text) if m.start() == 0 and m.end() == len(text)), None
    )


def labelled_period_dates(text):
    """Cover filing/receipt stamps do not establish the accounts period."""
    return [
        value
        for m, value in date_matches(text)
        if re.search(
            r"(?:year|period)\s+ended\s*:?\s*$|(?:as\s+at|to)\s*:?\s*$",
            text[max(0, m.start() - 45) : m.start()],
            re.I,
        )
    ]
