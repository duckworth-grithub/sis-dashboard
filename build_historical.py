"""Historical (2025-26) coverage layer: which schools had a Phones in Focus report last school year, per wave.

    python3 build_historical.py \
        --fall  "inputs/phonebans/use this/Phones+in+Focus+-+Fall+2025_July+2,+2026_07.33 (1).csv" \
        --spring "inputs/phonebans/use this/Phones in Focus_October 1, 2025_16.32.csv" \
        --illinois "inputs/phonebans/use this/Illinois Survey Data 7.7.26.xlsx" \
        --dropdown-ids inputs/phonebans/nces_dropdown_cities.csv \
        --directory inputs/phonebans/nces_directory_latest.csv [--out data]

Writes
  data/hist_2025.csv            ncessch, wave, reports, match_method   (build_schools.py --historical sums the waves)
  data/hist_2025_unmatched.csv  wave, state_name, school_string, zip, responses, candidates   (for review; no respondents)

Waves (team naming: Spring and Summer 2025 · Fall 2025–Spring 2026 (Qualtrics fall file + Illinois state survey) · Summer 2026) and how each is matched (exact only; the research team's cleaning rules from match_pif_nces.R, ported to common.clean_string):
  fall_2025           Qualtrics export; FormattedSchool is the dropdown string "NAME - CITY" -> exact (state, string) join to the
                      dropdown-with-ids file (nces_dropdown_cities), case-insensitive.
  spring_summer_2025  Qualtrics export; school name typed/selected without city -> (state, cleaned name) to the NCES directory,
                      then (state, cleaned name, zip) for the rest. Names shared by several schools in a state stay unmatched.
  illinois_2025_26    Illinois state survey (xlsx); "Name | street | zip" -> (cleaned name, zip) to the NCES directory for IL,
                      then (cleaned name) alone when unique in the state.
A response counts when it is finished (or Submitted), not a preview, and names a school.
"""
import argparse
import collections
import csv
import os
import re

from common import HERE, norm_state, read_csv, write_csv, STATE_CODE_NAMES, clean_string

fold = lambda s: re.sub(r"\s+", " ", (s or "")).strip().casefold()


def read_qualtrics_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh)
        header = [h.replace("\xa0", " ") for h in next(rd)]
        row2 = next(rd)
        row3 = next(rd)
        rows = []
        if not (row3 and row3[0].startswith("{")):
            rows.extend(dict(zip(header, r)) for r in (row2, row3))
        rows.extend(dict(zip(header, r)) for r in rd)
    return rows


def first(r, cols):
    for c in cols:
        v = (r.get(c) or "").strip()
        if v:
            return v
    return ""


def finished(r):
    """Completion rule from the research team: a response counts once it named a school (the school page sat at the end of
    the 2025 surveys), whatever Qualtrics' Finished flag says. Previews and the team's own test rows ("tktk" anywhere) are dropped."""
    if r.get("Status") == "Survey Preview":
        return False
    return not any("tktk" in str(v).lower() for v in r.values() if v)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fall")
    ap.add_argument("--spring")
    ap.add_argument("--illinois")
    ap.add_argument("--summer2026", help="PIF Summer 2026 survey export: its own cycle, carried forward like the 2025-26 waves")
    ap.add_argument("--matches", help="the team's pif_nces_matches.dta (response_id, nces_pif_id): first pass for the spring/summer 2025 wave")
    ap.add_argument("--dropdown-ids", default=os.path.join(HERE, "inputs", "phonebans", "nces_dropdown_cities.csv"))
    ap.add_argument("--directory", default=os.path.join(HERE, "inputs", "phonebans", "nces_directory_latest.csv"))
    ap.add_argument("--out", default=os.path.join(HERE, "data"))
    a = ap.parse_args()

    dd_key = collections.defaultdict(set)          # (state_name, folded "NAME - CITY") -> ids
    for r in read_csv(a.dropdown_ids):
        st = STATE_CODE_NAMES.get(r["st"].strip().upper(), r["st"])
        dd_key[(norm_state(st), fold(r["school_name"]))].add(r["ncessch"].strip().zfill(12))
    by_name = collections.defaultdict(set)         # (state_name, cleaned name) -> ids
    by_name_zip = collections.defaultdict(set)     # (state_name, cleaned name, zip5) -> ids
    for r in read_csv(a.directory):
        st = norm_state(STATE_CODE_NAMES.get(r["st"].strip().upper(), r["st"]))
        n = clean_string(r["sch_name"])
        z = (r.get("lzip") or "").strip()[:5]
        sid = r["ncessch"].strip().zfill(12)
        by_name[(st, n)].add(sid)
        by_name_zip[(st, n, z)].add(sid)

    reports = collections.Counter()
    method = {}
    unmatched = collections.Counter()
    cand = {}

    def record(wave, state, string, zip5, ids, m):
        src = m.split(":")[0] if ":" in m else "qualtrics"
        if ids and len(ids) == 1:
            sid = next(iter(ids))
            reports[(sid, wave, src)] += 1
            method[(sid, wave, src)] = m
        else:
            unmatched[(wave, state, string, zip5)] += 1
            cand[(wave, state, string, zip5)] = "|".join(sorted(ids)) if ids else ""

    stats = {}
    if a.fall:
        S = ["Q23StatePol", "Q183StateNew", "Q34StateEnf", "Q45StateNon"]
        n_fin = n_sch = 0
        for r in read_qualtrics_csv(a.fall):
            if not finished(r):
                continue
            n_fin += 1
            s = (r.get("FormattedSchool") or "").strip()
            if not s:
                continue
            n_sch += 1
            st = norm_state(first(r, S))
            record("fall2025_spring2026", st, s, "", dd_key.get((st, fold(s))), "qualtrics_fall_2025:dropdown_ids")
        stats["fall2025_spring2026 (Qualtrics)"] = (n_fin, n_sch)

    matches = {}
    if a.matches:
        import pandas as pd
        mdf = pd.read_stata(a.matches, convert_categoricals=False)
        for rid, sid in zip(mdf["response_id"].astype(str), mdf["nces_pif_id"].astype(str)):
            sid = sid.strip().split(".")[0]
            if rid.strip() and sid and sid.lower() not in ("nan", "none", ""):
                matches[rid.strip()] = sid.zfill(12)
        print(f"matches file: {len(matches)} response -> school pairs")
    if a.spring:
        S = ["Q23 school state pol", "Q183 schl state new", "Q34 school state enf", "Q45 school state non"]
        N = ["FormattedSchool", "Q20 school name pol_1", "Q188 schl name new _1", "Q31 school name enf_1", "Q42 school name non_1"]
        Z = ["Q60 zip code pol", "Q189 zip code new", "Q59 zip code enf", "Q61 zip code non"]
        n_fin = n_sch = 0
        for r in read_qualtrics_csv(a.spring):
            if not finished(r):
                continue
            n_fin += 1
            st, s, z = norm_state(first(r, S)), first(r, N), re.sub(r"\D", "", first(r, Z))[:5]
            if not (st and s):
                continue
            n_sch += 1
            rid = (r.get("ResponseId") or "").strip()
            if rid in matches:
                record("spring_summer_2025", st, s, z, {matches[rid]}, "team_matches_file")
                continue
            n = clean_string(s)
            ids = by_name.get((st, n))
            m = "directory_name"
            if not ids or len(ids) != 1:
                ids2 = by_name_zip.get((st, n, z)) if z else None
                if ids2:
                    ids, m = ids2, "directory_name_zip"
            record("spring_summer_2025", st, s, z, ids, m)
        stats["spring_summer_2025"] = (n_fin, n_sch)

    if a.illinois:
        import pandas as pd
        x = pd.read_excel(a.illinois, dtype=str).fillna("")
        n_fin = n_sch = 0
        for _, r in x.iterrows():
            if str(r.get("ResponseStatus", "")).strip() not in ("Submitted", ""):
                continue
            if any("tktk" in str(v).lower() for v in r.values() if v):
                continue
            n_fin += 1
            s = str(r.get("Q20NameNCESPolSchool", "")).strip()
            if not s:
                continue
            n_sch += 1
            bits = [b.strip() for b in s.split("|")]
            name = clean_string(bits[0])
            z = re.sub(r"\D", "", bits[-1])[:5] if len(bits) > 1 else ""
            ids = by_name_zip.get(("Illinois", name, z)) if z else None
            m = "directory_name_zip"
            if not ids or len(ids) != 1:
                ids2 = by_name.get(("Illinois", name))
                if ids2 and len(ids2) == 1:
                    ids, m = ids2, "directory_name"
            record("fall2025_spring2026", "Illinois", s, z, ids, "illinois_state_survey:" + m)
        stats["fall2025_spring2026 (Illinois)"] = (n_fin, n_sch)

    if a.summer2026:
        POL = ["Q20New25When", "Q24New26When", "Q13AgreewithModal", "Q14Known25When", "Q17Known26When", "Q41NewSchl25Policy",
               "Q22Writein25Policy", "Q44Known25Policy", "Q16Known26Policy"]
        n_fin = n_sch = 0
        for r in read_qualtrics_csv(a.summer2026):
            if r.get("Status") == "Survey Preview" or any("tktk" in str(v).lower() for v in r.values() if v):
                continue
            if "district-wide" in (r.get("Q240LibType") or "").lower():
                continue                                   # district-path librarians: circulation only, no school
            if not any((r.get(c) or "").strip() for c in POL):
                continue                                   # no phone-policy answers = not a coverage report
            n_fin += 1
            s = (r.get("Q3NameNCESSummer_1") or "").strip()
            if not s:
                continue
            n_sch += 1
            st = norm_state(r.get("Q2StateSummer") or "")
            ids = dd_key.get((st, fold(s)))
            m = "dropdown_ids"
            if not ids:
                ids2 = by_name.get((st, clean_string(s.rsplit(" - ", 1)[0])))
                if ids2 and len(ids2) == 1:
                    ids, m = ids2, "directory_name"
            record("summer_2026", st, s, "", ids, m)
        stats["summer_2026"] = (n_fin, n_sch)

    os.makedirs(a.out, exist_ok=True)
    write_csv(os.path.join(a.out, "hist_2025.csv"),
              [{"ncessch": k[0], "wave": k[1], "source": k[2], "reports": v, "match_method": method[k]} for k, v in sorted(reports.items())],
              ["ncessch", "wave", "source", "reports", "match_method"])
    write_csv(os.path.join(a.out, "hist_2025_unmatched.csv"),
              [{"wave": k[0], "state_name": k[1], "school_string": k[2], "zip": k[3], "responses": v, "candidates": cand[k]}
               for k, v in sorted(unmatched.items(), key=lambda x: (x[0][0], -x[1]))],
              ["wave", "state_name", "school_string", "zip", "responses", "candidates"])
    for label, (n_fin, n_sch) in stats.items():
        wave = label.split(" ")[0]
        src = "illinois_state_survey" if "Illinois" in label else "qualtrics"
        m_resp = sum(v for k, v in reports.items() if k[1] == wave and (k[2] == src or (src == "qualtrics" and k[2].startswith("qualtrics"))))
        m_sch = len({k[0] for k in reports if k[1] == wave and (k[2] == src or (src == "qualtrics" and k[2].startswith("qualtrics")))})
        u = sum(v for k, v in unmatched.items() if k[0] == wave and ((k[1] == "Illinois") == (src == "illinois") if src else True))
        print(f"{label}: {n_fin} usable, {n_sch} with a school; matched {m_resp} responses ({m_resp / max(n_sch, 1):.1%}) to {m_sch} schools; unmatched {u} responses")
    print(f"schools with any prior-cycle report: {len({k[0] for k in reports})}")


if __name__ == "__main__":
    main()
