"""SYNTHETIC test data: fake Qualtrics CSV exports for the three surveys, drawn from the real school list, so the
whole pipeline (matching, validity, status, KPIs, dashboard) can be exercised before launch.

    python3 make_sample_data.py --schools data/schools.csv --out sample --start 2026-08-17 --days 54 --seed 7

Writes sample/exports/{student,educator,librarian}.csv in the shape of a Qualtrics CSV export with numeric values
(3 header rows: export tag, question text, ImportId) and sample/config.json (a copy of config.json with
cycle_start = --start). Everything in here is invented: emails are @example.com, IPs are 10.x, response ids
start with R_SYN. Nothing is drawn from real respondents.
"""
import argparse
import csv
import hashlib
import json
import os
import random
from datetime import datetime, timedelta, timezone

from common import HERE, load_config, read_csv, load_state_codes

META = ["StartDate", "EndDate", "Status", "IPAddress", "Progress", "Duration (in seconds)", "Finished", "RecordedDate",
        "ResponseId", "RecipientLastName", "RecipientFirstName", "RecipientEmail", "ExternalReference", "LocationLatitude",
        "LocationLongitude", "DistributionChannel", "UserLanguage"]
EMB = ["utm_source", "utm_medium", "utm_campaign", "adgroup", "Q_URL", "Referer"]

COLS = {
    "student": META + ["s_consent", "s_grade", "s_school_state", "s_school_name_nces1_1", "s_school_name_nces2_1",
                       "s_school_in_nces", "s_school_name_typed", "s_school_zip", "s_policy_when", "s_policy_where", "src"],
    "educator": META + ["e_role_type", "e_school_state", "e_school_name_nces1_1", "e_school_name_nces2_1", "e_school_in_nces",
                        "e_school_name_typed", "e_school_zip", "e_role_level", "e_policy_when", "e_policy_where", "e_tech_access", "e_close_email"] + EMB,
    "librarian": META + ["l_role_level", "l_school_state", "l_school_name_nces1_1", "l_school_name_nces2_1", "l_school_in_nces",
                         "l_school_name_typed", "l_school_zip", "l_serves", "l_policy_when", "l_policy_where", "l_dist_state", "l_close_email_s"] + EMB,
}
SDS2_STATES = {"Texas", "Utah", "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
               "American Samoa", "Guam", "Northern Mariana Islands", "Puerto Rico", "U.S. Virgin Islands"}

# (utm_source, utm_medium, utm_campaign, weight, new-school bias). Bias > 1 = more likely to hit uncovered schools.
CHANNELS = [
    ("", "", "", 30, 1.0),                                  # unknown / organic
    ("sis", "social", "2026-09-launch", 14, 0.8),
    ("angela", "social", "2026-09-angela", 12, 0.7),
    ("sis", "email", "2026-09-educator-list", 10, 1.0),
    ("nassp", "email", "nassp-oct-newsletter", 8, 1.6),     # partner
    ("pta-pa", "email", "2026-10-pa-pta", 6, 1.9),          # partner
    ("meta", "paid", "2026-09-meta-teachers", 7, 1.1),
    ("share", "referral", "educator", 6, 1.3),
    ("district", "email", "ohiodirect", 4, 2.2),
    ("", "qr", "2026-10-conference-qr", 3, 1.4),
]
PARTNERS = {"nassp": ("NASSP", "org", "2026-08-20", "2026-09-05", "2026-09-29", "middle|high", ""),
            "pta-pa": ("Pennsylvania PTA", "org", "2026-09-01", "2026-09-20", "2026-10-01", "elementary|middle", "PA")}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--schools", default=os.path.join(HERE, "data", "schools.csv"))
    ap.add_argument("--out", default=os.path.join(HERE, "sample"))
    ap.add_argument("--start", default="2026-08-17")
    ap.add_argument("--days", type=int, default=54)
    ap.add_argument("--n", type=int, default=6000, help="total synthetic responses")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rnd = random.Random(a.seed)

    schools = [r for r in read_csv(a.schools) if r["eligible"]]
    codes = load_state_codes(os.path.join(HERE, "data"))
    inv = {s: {v: k for k, v in m.items()} for s, m in codes.items()}
    start = datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc)

    # A fixed pool of "popular" schools so some reach 2+ reports; the rest are singletons.
    pool = rnd.sample(schools, 2200)
    popular = pool[:500]

    rows = {"student": [], "educator": [], "librarian": []}
    n_id = 0
    for i in range(a.n):
        n_id += 1
        # Growth over time: more responses later; a weekly rhythm (weekdays).
        day = int(min(a.days - 1, (rnd.random() ** 0.6) * a.days))
        t = start + timedelta(days=day, hours=rnd.randint(6, 21), minutes=rnd.randint(0, 59))
        if t.weekday() >= 5 and rnd.random() < 0.6:
            t += timedelta(days=2)
        survey = rnd.choices(["student", "educator", "librarian"], [45, 45, 10])[0]
        src, med, camp, _, bias = rnd.choices(CHANNELS, [c[3] for c in CHANNELS])[0]
        sch = rnd.choice(popular) if rnd.random() < 0.35 / bias else rnd.choice(pool)
        state = sch["state_name"]
        code = inv[survey].get(state, "")
        dur = rnd.randint(150, 900)
        finished = "1"
        progress = "100"
        status = "IP Address"
        channel = "anonymous"
        r = {k: "" for k in COLS[survey]}
        # Edge cases, in proportions that look like last year's data.
        u = rnd.random()
        if u < 0.04:   # abandoned
            finished, progress, dur = "0", str(rnd.randint(5, 80)), rnd.randint(10, 200)
        elif u < 0.045:  # preview
            status, channel = "Survey Preview", "preview"
        elif u < 0.05:   # recorded before the cycle
            t = start - timedelta(days=rnd.randint(1, 20))
        elif u < 0.06:   # speeder
            dur = rnd.randint(15, 50)
        in_nces = "1"
        typed = ""
        if rnd.random() < 0.06:  # school not found: typed name
            in_nces, typed = "0", sch["school_name"].split(" - ")[0] + " (typed)"
        r.update({"StartDate": (t - timedelta(seconds=dur)).strftime("%Y-%m-%d %H:%M:%S"), "EndDate": t.strftime("%Y-%m-%d %H:%M:%S"),
                  "RecordedDate": t.strftime("%Y-%m-%d %H:%M:%S"), "Status": status, "Progress": progress,
                  "Duration (in seconds)": str(dur), "Finished": finished, "ResponseId": f"R_SYN{n_id:06d}",
                  "IPAddress": f"10.{rnd.randint(0, 40)}.{rnd.randint(0, 255)}.{rnd.randint(1, 254)}",
                  "DistributionChannel": channel, "UserLanguage": "EN"})
        p = {"student": "s", "educator": "e", "librarian": "l"}[survey]
        r[f"{p}_school_state"] = code
        name_col = f"{p}_school_name_nces{'2' if state in SDS2_STATES else '1'}_1"
        if in_nces == "1":
            r[name_col] = sch["school_name"]
        r[f"{p}_school_in_nces"] = in_nces
        r[f"{p}_school_name_typed"] = typed
        r[f"{p}_school_zip"] = str(rnd.randint(10000, 99999)) if typed else ""
        email = f"person{rnd.randint(1, 4000)}@example.com" if rnd.random() < 0.6 else ""
        answered_policy = finished == "1" or rnd.random() < 0.5   # abandoned after the policy page still counts
        if survey == "student":
            r["s_consent"] = "1"
            if answered_policy:
                r["s_policy_when"], r["s_policy_where"] = str(rnd.randint(1, 3)), str(rnd.randint(1, 7))
            r["s_grade"] = str(rnd.randint(1, 7))
            r["src"] = "share" if src == "share" else (src or "")
        elif survey == "educator":
            r["e_role_type"] = "1"
            r["e_role_level"] = rnd.choice(["1", "4", "5"])
            if answered_policy:
                if r["e_role_level"] == "1":
                    r["e_tech_access"] = "1"
                else:
                    r["e_policy_when"], r["e_policy_where"] = str(rnd.randint(1, 3)), str(rnd.randint(1, 7))
            r["e_close_email"] = email
        else:
            district_only = rnd.random() < 0.15
            r["l_role_level"] = "0" if district_only else "1"
            if district_only:
                r["l_school_state"] = ""
                r[name_col] = ""
                r["l_school_in_nces"] = ""
                r["l_dist_state"] = code
            r["l_serves"] = rnd.choice(["1", "2,3", "1,2", "3", "1,2,3"])
            if not district_only and answered_policy and r["l_serves"] != "1":
                r["l_policy_when"], r["l_policy_where"] = str(rnd.randint(1, 3)), str(rnd.randint(1, 7))
            r["l_close_email_s"] = email
        if survey != "student":
            r.update({"utm_source": src, "utm_medium": med, "utm_campaign": camp,
                      "Referer": "https://screensinschools.org/" if not src and rnd.random() < 0.6 else ""})
        rows[survey].append(r)
        # Occasionally the same person submits twice (same email, same school, same day).
        if email and rnd.random() < 0.03 and finished == "1":
            n_id += 1
            d = dict(r)
            d["ResponseId"] = f"R_SYN{n_id:06d}"
            t2 = t + timedelta(minutes=rnd.randint(5, 300))
            d["RecordedDate"] = d["EndDate"] = t2.strftime("%Y-%m-%d %H:%M:%S")
            rows[survey].append(d)

    os.makedirs(os.path.join(a.out, "exports"), exist_ok=True)
    for survey, rs in rows.items():
        rs.sort(key=lambda x: x["RecordedDate"])
        cols = COLS[survey]
        with open(os.path.join(a.out, "exports", f"{survey}.csv"), "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            w.writerow(cols)  # question text row (stand-in)
            w.writerow([json.dumps({"ImportId": c}) for c in cols])
            for r in rs:
                w.writerow([r.get(c, "") for c in cols])
        print(f"{survey}: {len(rs)} synthetic rows")
    cfg = load_config()
    cfg.pop("_path", None)
    cfg["cycle_start"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    cfg["_synthetic"] = "SYNTHETIC DATA: generated by make_sample_data.py, not survey responses"
    with open(os.path.join(a.out, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    with open(os.path.join(a.out, "partners.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["partner", "display_name", "type", "owner", "contacted_on", "agreed_on", "activated_on", "expected_levels", "expected_states", "cost_usd", "staff_hours", "note"])
        for slug, (name, typ, c, ag, act, lv, st) in PARTNERS.items():
            w.writerow([slug, name, typ, "", c, ag, act, lv, st, "", "", "synthetic"])
        w.writerow(["ala", "American Library Association", "org", "", "2026-09-10", "", "", "elementary|middle|high", "", "", "", "synthetic: contacted, not yet agreed"])
    print("wrote", os.path.join(a.out, "config.json"), "and partners.csv")


if __name__ == "__main__":
    main()
