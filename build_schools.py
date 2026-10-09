"""Build the canonical school table (schools.csv) and the per-survey state codebook (state_codes.csv).

    python3 -I build_schools.py --dropdown inputs/SchoolDropdown26-27.1.csv --dropdown inputs/SchoolDropdown26-27.2.csv \
        [--ccd-directory ccd_sch_029_*.csv] [--ccd-membership ccd_sch_052_*.csv] [--ccd-geo EDGE_GEOCODE_PUBLICSCH_*.csv] \
        [--historical data/hist_2025.csv] [--qsf student=path.qsf --qsf educator=path.qsf --qsf librarian=path.qsf] \
        [--config config.json] [--out data]

Inputs
- dropdown CSVs: the files behind the Qualtrics Supplemental Data Sources (st, state_name, school_name, ncessch).
  school_name is the exact string Qualtrics stores, so it is kept verbatim.
- CCD directory / membership / geocode (optional): NCES Common Core of Data. Column names are matched
  case-insensitively: NCESSCH, LEAID, LEA_NAME, LEVEL, GSLO, GSHI, SY_STATUS, SCH_TYPE, CHARTER_TEXT;
  membership TOTAL_INDICATOR + STUDENT_COUNT; geocode LOCALE.
- historical (optional): ncessch, reports  (from build_historical.py).
- qsf (optional): regenerates state_codes.csv from the surveys' state dropdown RecodeValues.

Only the base columns are written here. The run-time columns (cur_valid_reports, status, ...) are added by
coverage_export.py.
"""
import argparse
import collections
import html
import json
import os
import re

from common import HERE, STATES_50_DC, DEFAULT_STATE_CODES, load_config, norm_state, read_csv, write_csv, to_int

BASE_COLUMNS = [
    "ncessch", "st", "state_name", "school_name", "name_ambiguous", "leaid", "district", "level", "grade_lo",
    "grade_hi", "urbanicity", "enrollment", "charter", "in_ccd", "ccd_status", "ccd_type", "eligible",
    "ineligible_reason", "hist_reports_2025", "hist_represented", "hist_waves",
]

LEVEL_MAP = {
    "1": "elementary", "2": "middle", "3": "high", "4": "other", "n": "other",
    "elementary": "elementary", "primary": "elementary", "middle": "middle", "high": "high",
    "secondary": "high", "prekindergarten": "other", "adult education": "other", "ungraded": "other",
    "other": "other", "not applicable": "other", "not reported": "unknown", "": "unknown",
}
OPEN_STATUS = {"1", "3", "4", "5", "8"}  # open, new, added, changed agency, reopened


def ci(row, *names):
    """Case-insensitive column lookup."""
    low = {k.lower(): k for k in row}
    for n in names:
        if n.lower() in low:
            return row[low[n.lower()]]
    return ""


def locale_bucket(code):
    c = str(code or "").strip()[:1]
    return {"1": "city", "2": "suburb", "3": "town", "4": "rural"}.get(c, "")


PANEL_LEVEL = {"elementary": "elementary", "middle": "middle", "high": "high", "secondary": "high", "other": "other",
               "prekindergarten": "other", "ungraded": "other", "adult education": "other", "not applicable": "other",
               "not reported": "unknown", "": "unknown"}
PANEL_OPEN = {"open", "new", "reopened", "added", "changed boundary/agency"}


def read_panel(path):
    """The research team's NCES panel, reduced to one latest-year row per school (inputs/phonebans/nces_schools_latest.csv,
    made from nces_panel.dta). Same output shape as read_ccd()."""
    ccd = {}
    for r in read_csv(path):
        sid = (r.get("ncessch") or "").strip().zfill(12)
        if not sid:
            continue
        status = (r.get("updated_status_text") or "").strip().lower()
        stype = (r.get("sch_type_text") or "").strip().lower()
        enr = to_int(r.get("tot_enr"))
        ccd[sid] = {
            "leaid": (r.get("leaid") or "").strip(), "district": (r.get("lea_name") or "").strip(),
            "level": PANEL_LEVEL.get((r.get("level") or "").strip().lower(), "unknown"),
            "grade_lo": "", "grade_hi": "",
            "ccd_status": "1" if status in PANEL_OPEN else ("2" if status else ""),
            "ccd_type": "1" if stype == "regular school" else ("4" if stype else ""),
            "charter": "true" if (r.get("charter_text") or "").strip().lower() == "yes" else "",
            "enrollment": str(enr) if enr is not None else "",
            "urbanicity": locale_bucket(r.get("locale")),
            "panel_year": (r.get("year") or "").split(".")[0], "city": (r.get("lcity") or "").strip(),
        }
    return ccd


def read_ccd(directory, membership, geo):
    ccd = {}
    if directory:
        for r in read_csv(directory, encoding="latin-1"):
            sid = ci(r, "NCESSCH").strip().zfill(12)
            if not sid:
                continue
            lvl = ci(r, "LEVEL").strip().lower()
            ccd[sid] = {
                "leaid": ci(r, "LEAID").strip(), "district": ci(r, "LEA_NAME").strip(),
                "level": LEVEL_MAP.get(lvl, "unknown"), "grade_lo": ci(r, "GSLO").strip(), "grade_hi": ci(r, "GSHI").strip(),
                "ccd_status": ci(r, "SY_STATUS").strip(), "ccd_type": ci(r, "SCH_TYPE").strip(),
                "charter": "true" if ci(r, "CHARTER_TEXT").strip().lower() in ("yes", "1") else "",
            }
    if membership:
        for r in read_csv(membership, encoding="latin-1"):
            if ci(r, "TOTAL_INDICATOR").strip().lower() not in ("education unit total", ""):
                continue
            sid = ci(r, "NCESSCH").strip().zfill(12)
            n = to_int(ci(r, "STUDENT_COUNT", "MEMBER"))
            if sid and n is not None:
                ccd.setdefault(sid, {})["enrollment"] = str(n)
    if geo:
        for r in read_csv(geo, encoding="latin-1"):
            sid = ci(r, "NCESSCH").strip().zfill(12)
            if sid:
                ccd.setdefault(sid, {})["urbanicity"] = locale_bucket(ci(r, "LOCALE", "ULOCALE"))
    return ccd


def state_codes_from_qsf(paths):
    """{survey: {recode: state_name}} from each survey's state dropdown (first MC/DL question asking for the state)."""
    out = {}
    strip = lambda s: re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))).strip()
    for survey, p in paths.items():
        d = json.load(open(p, encoding="utf-8"))
        for e in d["SurveyElements"]:
            if e["Element"] != "SQ":
                continue
            q = e["Payload"]
            tag = (q.get("DataExportTag") or "").lower()
            if q.get("QuestionType") == "MC" and q.get("Selector") == "DL" and tag.endswith("_school_state"):
                rec = q.get("RecodeValues") or {}
                out[survey] = {str(rec.get(cid, cid)): strip(c.get("Display")) for cid, c in q["Choices"].items()}
                break
        if survey not in out:
            raise SystemExit(f"{p}: no *_school_state dropdown found")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dropdown", action="append", required=True)
    ap.add_argument("--ccd-directory")
    ap.add_argument("--ccd-membership")
    ap.add_argument("--ccd-geo")
    ap.add_argument("--nces-panel-csv", help="NCES directory or panel reduced to one latest-year row per school (preferred over the CCD files)")
    ap.add_argument("--nces-overlay-csv", help="optional second file (the panel) supplying locale and enrollment where the master lacks them")
    ap.add_argument("--historical")
    ap.add_argument("--qsf", action="append", default=[], help="survey=path.qsf")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "data"))
    a = ap.parse_args()
    cfg = load_config(a.config)

    rows = []
    for p in a.dropdown:
        for r in read_csv(p):
            sid = (r.get("ncessch") or "").strip()
            if not sid or not (r.get("state_name") or "").strip():
                print(f"skipping row without state/id in {os.path.basename(p)}: {r}")
                continue
            rows.append({"ncessch": sid.zfill(12), "st": r.get("st", "").strip(),
                         "state_name": norm_state(r["state_name"]), "school_name": r["school_name"]})
    ids = collections.Counter(r["ncessch"] for r in rows)
    dupes = [k for k, v in ids.items() if v > 1]
    if dupes:
        raise SystemExit(f"{len(dupes)} ncessch appear more than once across the dropdown files, e.g. {dupes[:5]}")

    key_count = collections.Counter((r["state_name"], r["school_name"]) for r in rows)
    ccd = read_panel(a.nces_panel_csv) if a.nces_panel_csv else read_ccd(a.ccd_directory, a.ccd_membership, a.ccd_geo)
    if a.nces_overlay_csv:
        for sid, extra in read_panel(a.nces_overlay_csv).items():
            base = ccd.setdefault(sid, dict(extra))
            for k in ("urbanicity", "enrollment"):
                if not base.get(k) and extra.get(k):
                    base[k] = extra[k]
    hist, hist_waves = {}, collections.defaultdict(set)
    if a.historical:
        for r in read_csv(a.historical):
            sid = r["ncessch"].zfill(12)
            hist[sid] = hist.get(sid, 0) + (to_int(r.get("reports")) or 0)
            if r.get("wave"):
                hist_waves[sid].add(r["wave"])

    eligible_states = set(STATES_50_DC) if cfg.get("eligible_states", "50_plus_dc") == "50_plus_dc" else None
    out = []
    for r in rows:
        c = ccd.get(r["ncessch"], {})
        row = dict(r)
        row["name_ambiguous"] = "true" if key_count[(r["state_name"], r["school_name"])] > 1 else ""
        for k in ("leaid", "district", "grade_lo", "grade_hi", "urbanicity", "enrollment", "charter", "ccd_status", "ccd_type"):
            row[k] = c.get(k, "")
        row["level"] = c.get("level", "unknown")
        row["in_ccd"] = "true" if c else ""
        reason = ""
        if eligible_states is not None and r["state_name"] not in eligible_states:
            reason = "bie_dodea" if r["state_name"] in ("Bureau of Indian Education", "Department of Defense Education Activity") else "territory"
        elif ccd:
            if not c and cfg.get("require_in_nces_panel", True):
                reason = "not_in_ccd"
            elif c and cfg.get("require_open_school", True) and c.get("ccd_status") and c["ccd_status"] not in OPEN_STATUS:
                reason = "closed"
            elif c and cfg.get("require_regular_school", False) and c.get("ccd_type") and c["ccd_type"] != "1":
                reason = "not_regular_school"
        row["eligible"] = "" if reason else "true"
        row["ineligible_reason"] = reason
        row["hist_reports_2025"] = str(hist.get(r["ncessch"], 0))
        row["hist_represented"] = "true" if hist.get(r["ncessch"], 0) > 0 else ""
        row["hist_waves"] = ";".join(sorted(hist_waves.get(r["ncessch"], ())))
        out.append(row)
    out.sort(key=lambda x: (x["state_name"], x["school_name"], x["ncessch"]))
    os.makedirs(a.out, exist_ok=True)
    write_csv(os.path.join(a.out, "schools.csv"), out, BASE_COLUMNS)

    codes = DEFAULT_STATE_CODES
    if a.qsf:
        codes = state_codes_from_qsf(dict(x.split("=", 1) for x in a.qsf))
    sc = [{"survey": s, "code": k, "state_name": norm_state(v)} for s, m in codes.items() for k, v in sorted(m.items(), key=lambda x: int(x[0]))]
    write_csv(os.path.join(a.out, "state_codes.csv"), sc, ["survey", "code", "state_name"])

    n = len(out)
    elig = sum(1 for r in out if r["eligible"])
    amb = sum(1 for r in out if r["name_ambiguous"])
    print(f"schools.csv: {n} schools, {elig} eligible, {n - elig} out of scope "
          f"({collections.Counter(r['ineligible_reason'] for r in out if r['ineligible_reason'])})")
    print(f"  name-ambiguous schools: {amb} ({amb / n:.1%}); levels: {dict(collections.Counter(r['level'] for r in out))}")
    print(f"  CCD rows matched: {sum(1 for r in out if r['in_ccd'])}; historical 2025-26 represented: {sum(1 for r in out if r['hist_represented'])}")
    print(f"state_codes.csv: {len(sc)} rows ({'from QSF' if a.qsf else 'built-in defaults from the Oct 2026 exports'})")


if __name__ == "__main__":
    main()
