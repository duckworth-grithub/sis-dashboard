#!/usr/bin/env python3
"""Match a state's own list of eligible schools to NCES ids.

Input: the spreadsheets states send (school names, sometimes a district/division, a responded flag).
Output: inputs/state_lists/<ST>.csv (ncessch, list_name, responded, match_method, score) — ids only, safe to commit —
plus a review file per state with the names that could not be matched cleanly and their best candidates.

build_schools.py treats a state with a list as "definitive": NCES schools in that state that are not on the list
become ineligible (reason not_on_state_list). Levels still come from the NCES rule.

Usage:
  python3 build_state_lists.py --state ND --file "ND_... .csv" --format responded_grid
  python3 build_state_lists.py --state VA --file "Virginia ... .csv" --format va_schools
"""
import argparse
import collections
import csv
import difflib
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
from common import STATE_CODE_NAMES  # noqa: E402

ABBR = {
    "elem": "elementary", "el": "elementary", "es": "elementary", "jr": "junior", "sr": "senior", "sch": "school", "schs": "school",
    "ctr": "center", "cntr": "center", "acad": "academy", "hs": "high", "ms": "middle", "jhs": "junior high", "mt": "mount",
    "st": "saint", "ft": "fort", "intermed": "intermediate", "int": "intermediate", "tech": "technical", "voc": "vocational",
    "co": "county", "pub": "public", "sec": "secondary", "k8": "k 8", "k12": "k 12",
}
DROP = {"school", "schools", "public", "the", "of"}


def norm(s, keep_school=False):
    s = (s or "").lower().replace("&", " and ").replace("/", " ").replace("-", " ").replace("'", "")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    toks = [ABBR.get(t, t) for t in s.split()]
    if not keep_school:
        toks = [t for t in toks if t not in DROP]
    return " ".join(toks)


def read_state_file(path, fmt):
    """Return [(name, district_or_division, responded_bool_or_None)]."""
    rows = list(csv.reader(open(path, encoding="utf-8-sig")))
    out = []
    if fmt == "va_schools":
        hdr = rows[0]
        i_name, i_div, i_resp = hdr.index("School Name"), hdr.index("Division Name"), hdr.index("Responded")
        for r in rows[1:]:
            if len(r) > i_name and r[i_name].strip():
                out.append((r[i_name].strip(), r[i_div].strip(), r[i_resp].strip().lower() == "yes"))
    elif fmt == "responded_grid":
        hi = next(i for i, r in enumerate(rows) if "Schools_Responded" in r)
        hdr = rows[hi]
        i_yes, i_no = hdr.index("Schools_Responded"), hdr.index("Schools_NOT_Responded")
        for r in rows[hi + 1:]:
            for idx, flag in ((i_yes, True), (i_no, False)):
                if len(r) > idx:
                    v = r[idx].strip()
                    if v and v != "#N/A" and not re.fullmatch(r"\d+", v):
                        out.append((v, "", flag))
    else:
        raise SystemExit(f"unknown format {fmt}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True, help="two-letter code")
    ap.add_argument("--file", required=True)
    ap.add_argument("--format", required=True, choices=["va_schools", "responded_grid"])
    ap.add_argument("--schools", default=os.path.join(HERE, "data", "schools.csv"))
    ap.add_argument("--out", default=os.path.join(HERE, "inputs", "state_lists"))
    ap.add_argument("--review", default=os.path.join(HERE, "..", "coverage-docs", "state-list-review"))
    ap.add_argument("--min-score", type=float, default=0.86)
    a = ap.parse_args()

    st = a.state.upper()
    state_name = STATE_CODE_NAMES[st]
    ours = [r for r in csv.DictReader(open(a.schools, encoding="utf-8")) if r["st"] == st]
    for r in ours:
        base = r["school_name"].rsplit(" - ", 1)[0]
        r["_n"] = norm(base)
        r["_nk"] = norm(base, keep_school=True)
        r["_d"] = norm(r.get("district", ""))
    by_n = collections.defaultdict(list)
    for r in ours:
        by_n[r["_n"]].append(r)
    all_n = [r["_n"] for r in ours]

    entries = read_state_file(a.file, a.format)
    # names that belong to another state entirely (e.g. NC rows inside the ND file) are set aside, not matched
    results, review = [], []
    seen = set()
    for name, div, resp in entries:
        key = (norm(name), norm(div))
        if key in seen:
            continue
        seen.add(key)
        n = norm(name)
        cands = by_n.get(n, [])
        method, score = "exact", 1.0
        if len(cands) > 1 and div:
            d = norm(div)
            tied = [c for c in cands if c["_d"] and (c["_d"] in d or d in c["_d"])]
            if len(tied) == 1:
                cands, method = tied, "exact+division"
        if len(cands) > 1:
            # the directory spans 2017-2025, so a renamed district or re-issued id leaves two entries for one school: keep the open one
            open_ = [c for c in cands if c.get("ccd_status") == "1"]
            if len(open_) == 1:
                cands, method = open_, method + "+open"
        if not cands:
            # truncated names (Arkansas cuts at 30 chars): prefix match
            if len(name) >= 29:
                pre = [r for r in ours if r["_nk"].startswith(norm(name[:28], keep_school=True))]
                if len(pre) == 1:
                    cands, method, score = pre, "prefix", 0.95
        if not cands:
            scored = sorted(((difflib.SequenceMatcher(None, n, m).ratio(), m) for m in set(all_n)), reverse=True)[:5]
            if scored and scored[0][0] >= a.min_score and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.03 or by_n[scored[0][1]] and len(by_n[scored[0][1]]) == 1 and scored[0][0] >= 0.93):
                cands, method, score = by_n[scored[0][1]], "fuzzy", round(scored[0][0], 3)
                if len(cands) > 1 and div:
                    d = norm(div)
                    tied = [c for c in cands if c["_d"] and (c["_d"] in d or d in c["_d"])]
                    if len(tied) == 1:
                        cands, method = tied, "fuzzy+division"
                if len(cands) > 1:
                    open_ = [c for c in cands if c.get("ccd_status") == "1"]
                    if len(open_) == 1:
                        cands, method = open_, method + "+open"
            else:
                review.append({"state": st, "list_name": name, "division": div, "responded": resp, "problem": "no match",
                               "candidates": " | ".join(f"{m} ({s:.2f})" for s, m in scored[:3])})
                continue
        if len(cands) > 1:
            # the state sheets list each distinct NAME once, so "Lincoln Elementary" stands for every Lincoln Elementary in the state:
            # keep all of them eligible (open ones if any are open) and flag that the responded flag is name-level, not school-level
            open_ = [c for c in cands if c.get("ccd_status") == "1"] or cands
            for c in open_:
                results.append({"ncessch": c["ncessch"], "st": st, "list_name": name, "division": div, "responded": "yes" if resp else "no",
                                "match_method": f"name-shared ({len(open_)})", "score": 0.5, "nces_name": c["school_name"], "nces_district": c["district"]})
            review.append({"state": st, "list_name": name, "division": div, "responded": resp, "problem": f"shared name ({len(open_)} schools kept)",
                           "candidates": " | ".join(f"{c['school_name']} [{c['district']}] {c['ncessch']}" for c in open_[:6])})
            continue
        c = cands[0]
        results.append({"ncessch": c["ncessch"], "st": st, "list_name": name, "division": div, "responded": "yes" if resp else "no",
                        "match_method": method, "score": score, "nces_name": c["school_name"], "nces_district": c["district"]})

    # one NCES school claimed by two list names: keep the better score, send the other to review
    by_id = collections.defaultdict(list)
    for r in results:
        by_id[r["ncessch"]].append(r)
    kept = []
    for sid, rs in by_id.items():
        rs.sort(key=lambda x: -float(x["score"]))
        kept.append(rs[0])
        for extra in rs[1:]:
            review.append({"state": st, "list_name": extra["list_name"], "division": extra["division"], "responded": extra["responded"],
                           "problem": "duplicate target", "candidates": f"also matched by {rs[0]['list_name']} -> {sid}"})
    kept.sort(key=lambda x: x["list_name"].lower())

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, f"{st}.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ncessch", "st", "list_name", "division", "responded", "match_method", "score", "nces_name", "nces_district"])
        w.writeheader(); w.writerows(kept)
    os.makedirs(a.review, exist_ok=True)
    with open(os.path.join(a.review, f"{st}_review.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["state", "list_name", "division", "responded", "problem", "candidates"])
        w.writeheader(); w.writerows(review)
    meth = collections.Counter(r["match_method"] for r in kept)
    prob = collections.Counter(r["problem"].split(" ")[0] for r in review)
    print(f"{state_name}: {len(entries)} list rows -> {len(kept)} matched ({dict(meth)}); review {len(review)} ({dict(prob)}); "
          f"NCES schools in state {len(ours)}, on list {len(kept)}, would become ineligible {len(ours) - len(kept)}")


if __name__ == "__main__":
    main()
