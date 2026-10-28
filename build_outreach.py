#!/usr/bin/env python3
"""Turn the team's outreach tracker (an .xlsx export of the Google Sheet) into inputs/outreach.csv.

Only what the dashboard needs is kept: date, state, type, audience, a generic label (kind of send) and a campaign id. The sheet's own wording is not published.
Owner and Notes never leave the spreadsheet. Rows without a real date (planned sends) are skipped.

Usage: python3 build_outreach.py "/path/to/tracker.xlsx"   or   python3 build_outreach.py tracker.csv  (the sheet published as CSV)
"""
import csv
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import STATE_CODE_NAMES  # noqa: E402

NAME_TO_CODE = {v.lower(): k for k, v in STATE_CODE_NAMES.items()}


def state_of(text):
    t = (text or "").lower()
    for name, code in sorted(NAME_TO_CODE.items(), key=lambda kv: -len(kv[0])):
        if re.search(r"\b" + re.escape(name) + r"\b", t):
            return code
    return ""


def main():
    src = sys.argv[1]
    if src.lower().endswith(".csv"):
        with open(src, newline="", encoding="utf-8-sig") as f:
            sheets = [[r for r in csv.reader(f) if any(c.strip() for c in r)]]
    else:
        import openpyxl   # only needed for a local .xlsx snapshot; the Action fetches the sheet as CSV
        wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
        sheets = [[r for r in ws.iter_rows(values_only=True) if any(c not in (None, "") for c in r)] for ws in wb.worksheets]
    out = []
    for rows in sheets:
        if not rows:
            continue
        hdr = [str(c or "").strip().lower() for c in rows[0]]
        col = lambda *names: next((i for i, h in enumerate(hdr) if any(h.startswith(n) for n in names)), None)
        i_content, i_type, i_aud, i_date, i_link, i_id = col("content"), col("type"), col("intended audience", "audience"), col("estimated date", "date"), col("link"), col("campaign")
        for r in rows[1:]:
            get = lambda i: (r[i] if i is not None and i < len(r) else None)
            d = get(i_date)
            if isinstance(d, datetime):
                date = d.date().isoformat()
            else:
                m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(d or ""))
                if not m:
                    m2 = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(d or ""))   # Google Sheets CSV dates: 10/19/2026
                    date = f"{m2.group(3)}-{int(m2.group(1)):02d}-{int(m2.group(2)):02d}" if m2 else ""
                else:
                    date = m.group(0)
            if not date:
                continue   # planned, date unknown
            content = str(get(i_content) or "").strip()
            aud = str(get(i_aud) or "").strip().lower()
            audience = "librarian" if "librar" in aud else "student" if "student" in aud else "educator" if "educ" in aud or "teacher" in aud else aud or "all"
            typ = str(get(i_type) or "").strip()
            link = str(get(i_link) or "").strip()
            camp = str(get(i_id) or "").strip() or (re.search(r"utm_campaign=([^&\s]+)", link).group(1) if "utm_campaign=" in link else "")
            # generic label only (the page is public): kind of send, never the sheet's own wording
            c = (content + " " + typ).lower()
            kind = ("Online forum post" if "reddit" in c or ("forum" in c and "partner" not in typ.lower()) else
                    "Partner newsletter" if "newsletter" in c else "Partner forums" if "forum" in c else "Social post" if "social" in c or "post" in c
                    else "Conference" if "conference" in c or "webinar" in c else "State email" if "state" in typ.lower() or "email" in c
                    else "Partner comms" if "partner" in typ.lower() else (typ or "Outreach"))
            out.append({"date": date, "state": state_of(content), "type": typ, "audience": audience, "label": kind, "campaign": camp})
    out.sort(key=lambda x: (x["date"], x["label"]))
    path = os.path.join(HERE, "inputs", "outreach.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "state", "type", "audience", "label", "campaign"])
        w.writeheader(); w.writerows(out)
    print(f"outreach.csv: {len(out)} dated sends ({sum(1 for x in out if x['state'])} with a state); planned rows without a date skipped")


if __name__ == "__main__":
    main()
