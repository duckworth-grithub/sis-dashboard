"""Typed-school matching for the coverage export: the team's three stages (Luca), run on every refresh.

Stage 1  exact cleaned name within the state            -> applied as a match (typed_exact)
Stage 2  exact cleaned name within the state and ZIP     -> applied as a match (typed_zip)
Stage 3  Jaro-Winkler within the same ZIP, distance < 0.18 -> PROPOSED ONLY, with score and runner-up, for a check pass
         (Nov 16, 2026: a fifth of stage-3 picks were wrong, private schools and sibling campuses forced onto the nearest public school)

Cleaning is common.clean_string on both sides. Candidate ZIPs come from inputs/phonebans/nces_schools_latest.csv (lzip, latest row).
"""
import collections
import csv
import os
import re

from common import clean_string

NON_SCHOOL = re.compile(r"\b(SCHOOL DISTRICT|PUBLIC SCHOOLS|CENTRAL OFFICE|DISTRICT OFFICE|BOARD MEMBER|ITINERANT|UNIVERSITY|COMMUNITY COLLEGE|COLLEGE OF)\b")
SCHOOL_WORD = re.compile(r"\b(HIGH|MIDDLE|ELEMENTARY|ACADEMY|JUNIOR|INTERMEDIATE|PRIMARY|CHARTER|SCHOOL)\b")
JW_CUTOFF = 0.82          # similarity; the team's "distance < 0.18"


def jaro_winkler(a, b):
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    w = max(la, lb) // 2 - 1
    ma, mb, m = [False] * la, [False] * lb, 0
    for i in range(la):
        for j in range(max(0, i - w), min(lb, i + w + 1)):
            if not mb[j] and a[i] == b[j]:
                ma[i] = mb[j] = True
                m += 1
                break
    if not m:
        return 0.0
    t = k = 0
    for i in range(la):
        if ma[i]:
            while not mb[k]:
                k += 1
            if a[i] != b[k]:
                t += 1
            k += 1
    j = (m / la + m / lb + (m - t / 2) / m) / 3
    p = 0
    for i in range(min(4, la, lb)):
        if a[i] == b[i]:
            p += 1
        else:
            break
    return j + p * 0.1 * (1 - j)


class Matcher:
    def __init__(self, schools, inputs_dir):
        self.zip_of = {}
        p = os.path.join(inputs_dir, "phonebans", "nces_schools_latest.csv")
        if os.path.exists(p):
            latest = {}
            with open(p, encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    y = r.get("year") or ""
                    if r["ncessch"] not in latest or y > latest[r["ncessch"]][0]:
                        latest[r["ncessch"]] = (y, re.sub(r"\D", "", r.get("lzip") or "")[:5])
            self.zip_of = {k: v[1] for k, v in latest.items()}
        self.by_state = collections.defaultdict(list)
        for s in schools:
            if not s["eligible"]:
                continue
            base = s["school_name"].rsplit(" - ", 1)[0]
            self.by_state[s["state_name"]].append((s["ncessch"], s["school_name"], clean_string(base), self.zip_of.get(s["ncessch"], "")))

    def run(self, typed, state_name, zip5):
        """Returns dict(stage, ncessch, school, score, runner_up, note). stage 1/2 are safe to apply; 3 is a proposal; '' is no match."""
        out = dict(stage="", ncessch="", school="", score="", runner_up="", note="")
        cl = clean_string(typed)
        if not cl:
            return out
        if NON_SCHOOL.search(cl) and not SCHOOL_WORD.search(cl.replace("SCHOOL DISTRICT", "").replace("PUBLIC SCHOOLS", "")):
            out["note"] = "not a school (district, office, college)"
            return out
        zip5 = re.sub(r"\D", "", zip5 or "")[:5]
        cands = self.by_state.get(state_name, [])
        s1 = [c for c in cands if c[2] == cl]
        if len(s1) == 1:
            out.update(stage="1", ncessch=s1[0][0], school=s1[0][1], score="1.000")
            return out
        s2 = [c for c in (s1 or cands) if zip5 and c[3] == zip5 and c[2] == cl]
        if len(s2) == 1:
            out.update(stage="2", ncessch=s2[0][0], school=s2[0][1], score="1.000")
            return out
        if len(s1) > 1:
            out["note"] = "exact name matches %d schools in the state; no ZIP tie-break" % len(s1)
            return out
        pool = [c for c in cands if zip5 and c[3] == zip5]
        if not pool:
            out["note"] = "no ZIP given or no school in that ZIP" if not zip5 else "no school in ZIP %s" % zip5
            return out
        scored = sorted(((jaro_winkler(cl, c[2]), c) for c in pool), key=lambda x: -x[0])
        best, bc = scored[0]
        if best >= JW_CUTOFF:
            out.update(stage="3", ncessch=bc[0], school=bc[1], score="%.3f" % best,
                       runner_up=("%s %.3f" % (scored[1][1][1], scored[1][0])) if len(scored) > 1 else "")
        else:
            out["note"] = "best in ZIP %s %.3f, below cutoff" % (bc[1], best)
        return out
