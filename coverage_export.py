"""Screens in Schools — coverage pipeline: survey responses → matched schools → status, KPIs, lists, dashboard JSON.

    # from Qualtrics (daily job):   env QUALTRICS_TOKEN, QUALTRICS_DATACENTER
    python3 coverage_export.py --from-qualtrics [--include-in-progress] --out out

    # from CSV exports (numeric values, 3 header rows) or the synthetic sample:
    python3 coverage_export.py --from-csv student=sample/exports/student.csv --from-csv educator=... --from-csv librarian=... \
        --config sample/config.json --partners sample/partners.csv --out out

Reads   data/schools.csv, data/state_codes.csv (build_schools.py), inputs/channels.csv, inputs/manual_matches.csv,
        inputs/test_responses.txt, partners.csv
Writes  out/coverage-data.json   everything the dashboard shows
        out/schools.csv          canonical table + run-time columns (status, counts, dates, sources)
        out/responses.csv        one row per response: match, channel, validity, coverage_rank (hashes only, no PII)
        out/lists/*.csv          actionable school lists;  out/queue/unmatched.csv  manual-match queue
        out/reports/week-<ISO>.md  the Monday report
Definitions: COVERAGE-DESIGN.md. Only whitelisted fields are requested from Qualtrics; email addresses and IP addresses are never requested.
"""
import argparse
import collections
import csv
import io
import json
import os
import re
import statistics
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone

from common import HERE, load_config, load_state_codes, norm_state, parse_dt, iso, read_csv, write_csv, to_int, truthy, glob_match

LEVELS = ["elementary", "middle", "high", "other", "unknown"]
EMBEDDED = ["utm_source", "utm_medium", "utm_campaign", "src", "Referer", "Q_URL"]            # names read from a row
EMBEDDED_BY_SURVEY = {"student": ["src"], "educator": ["utm_source", "utm_medium", "utm_campaign", "Referer", "Q_URL"],
                      "librarian": ["utm_source", "utm_medium", "utm_campaign", "Referer", "Q_URL"]}   # names each survey defines
METADATA = ["startDate", "recordedDate", "finished", "progress", "duration", "distributionChannel"]   # never ipAddress

# logical field -> (QID for the API json export, export tag for CSV exports)
FIELDS = {
    "student": {
        "consent": ("QID1", "s_consent"), "grade": ("QID5", "s_grade"), "state": ("QID6", "s_school_state"),
        "nces1": ("QID7", "s_school_name_nces1"), "nces2": ("QID64", "s_school_name_nces2"), "in_nces": ("QID8", "s_school_in_nces"),
        "typed": ("QID10", "s_school_name_typed"), "zip": ("QID11", "s_school_zip"),
        "pol_when": ("QID13", "s_policy_when"), "pol_where": ("QID14", "s_policy_where"),   # the school's phone policy
    },
    "educator": {
        "role_type": ("QID4", "e_role_type"), "state": ("QID7", "e_school_state"), "nces1": ("QID8", "e_school_name_nces1"),
        "nces2": ("QID14", "e_school_name_nces2"), "in_nces": ("QID9", "e_school_in_nces"), "typed": ("QID11", "e_school_name_typed"),
        "zip": ("QID12", "e_school_zip"), "level": ("QID44", "e_role_level"),
        "pol_when": ("QID58", "e_policy_when"), "pol_where": ("QID59", "e_policy_where"),   # middle/high policy
        "pol_tech": ("QID20", "e_tech_access"),                                              # elementary: device access (its policy block)
    },
    "librarian": {
        "role_level": ("QID258", "l_role_level"), "state": ("QID279", "l_school_state"), "nces1": ("QID281", "l_school_name_nces1"),
        "nces2": ("QID286", "l_school_name_nces2"), "in_nces": ("QID282", "l_school_in_nces"), "typed": ("QID284", "l_school_name_typed"),
        "zip": ("QID285", "l_school_zip"), "serves": ("QID411", "l_serves"), "dist_state": ("QID259", "l_dist_state"),
        "pol_when": ("QID295", "l_policy_when"), "pol_where": ("QID296", "l_policy_where"),  # middle/high school librarians only
    },
}
FIELDS["summer"] = {
    "role_type": ("QID1", "Q1role"), "lib_type": ("QID240", "Q240LibType"), "state": ("QID2", "Q2StateSummer"),
    "nces1": ("QID3", "Q3NameNCESSummer"), "in_nces": ("QID4", "Q4InNCESSummer"), "typed": ("QID6", "Q6NameTypedSummer"),
    "zip": ("QID7", "Q7ZipCodeSummer"), "dist_state": ("QID259", "Q259DistrictState"),
    # any of these answered = the respondent gave phone-policy data (librarians on the district path never do)
    "pol_a": ("QID20", "Q20New25When"), "pol_b": ("QID24", "Q24New26When"), "pol_c": ("QID13", "Q13AgreewithModal"),
    "pol_d": ("QID14", "Q14Known25When"), "pol_e": ("QID17", "Q17Known26When"), "pol_f": ("QID41", "Q41NewSchl25Policy"),
    "pol_g": ("QID22", "Q22Writein25Policy"), "pol_h": ("QID44", "Q44Known25Policy"), "pol_i": ("QID16", "Q16Known26Policy"),
}
INVALID_ORDER = ["preview", "before_cycle", "unfinished", "test", "ineligible_role", "district_only", "no_policy_data", "no_school",
                 "unmatched", "ambiguous", "speeder"]
RESPONSE_COLUMNS = ["response_id", "survey", "start_at", "recorded_at", "finished", "finished_qualtrics", "progress", "duration_s",
                    "distribution_channel", "role", "respondent_level", "state_code", "state_name", "school_string",
                    "school_found", "typed_name_present", "zip_present", "match_status", "ncessch", "match_method",
                    "candidates", "utm_source", "utm_medium", "utm_campaign", "src", "referer_host", "channel", "partner",
                    "valid", "invalid_reason", "flags", "coverage_rank", "week"]
SCHOOL_RUN_COLUMNS = ["cur_valid_reports", "carried_reports_2025", "past_reports_uncounted", "total_reports", "cur_reports_by_role",
                      "first_report_at", "second_report_at", "last_report_at", "newly_represented_at", "reached_two_at",
                      "first_report_channel", "first_report_partner", "second_report_channel", "second_report_partner",
                      "status", "needs_refresh", "pending_ambiguous", "flags"]



def get(values, key):
    """Qualtrics puts text-entry answers under KEY, KEY_TEXT or KEY_1 depending on export type."""
    for k in (key, key + "_TEXT", key + "_1"):
        v = values.get(k)
        if v not in (None, "", []):
            return v
    return ""


def host(url):
    url = (url or "").strip()
    if "://" in url:
        url = url.split("://", 1)[1]
    return url.split("/", 1)[0].lower()


# ----------------------------------------------------------------------------- readers
def read_qualtrics_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh)
        header = [h.replace("\xa0", " ") for h in next(rd)]
        row2 = next(rd)
        row3 = next(rd)
        rows = []
        if not (row3 and row3[0].startswith("{")):   # not a 3-row Qualtrics header: treat rows 2-3 as data
            rows.extend(dict(zip(header, r)) for r in (row2, row3))
        rows.extend(dict(zip(header, r)) for r in rd)
    return rows


def normalize_csv(survey, rows):
    tags = {k: t for k, (q, t) in FIELDS[survey].items()}
    out = []
    for r in rows:
        meta = {
            "response_id": r.get("ResponseId", ""), "start_at": parse_dt(r.get("StartDate")), "recorded_at": parse_dt(r.get("RecordedDate")),
            "finished": truthy(r.get("Finished")), "progress": to_int(r.get("Progress")),
            "duration_s": to_int(r.get("Duration (in seconds)", r.get("Duration"))),
            "distribution_channel": (r.get("DistributionChannel") or "").lower() or ("preview" if r.get("Status") == "Survey Preview" else ""),
        }
        ans = {k: get(r, t) for k, t in tags.items()}
        emb = {k: r.get(k, "") for k in EMBEDDED}
        out.append(build(survey, meta, ans, emb))
    return out


def normalize_api(survey, responses):
    out = []
    for x in responses:
        v = x.get("values", {})
        meta = {
            "response_id": x.get("responseId", ""), "start_at": parse_dt(v.get("startDate")), "recorded_at": parse_dt(v.get("recordedDate")),
            "finished": truthy(v.get("finished")), "progress": to_int(v.get("progress")), "duration_s": to_int(v.get("duration")),
            "distribution_channel": (v.get("distributionChannel") or "").lower(),
        }
        ans = {k: get(v, q) for k, (q, t) in FIELDS[survey].items()}
        emb = {k: v.get(k, "") for k in EMBEDDED}
        out.append(build(survey, meta, ans, emb))
    return out


def build(survey, meta, ans, emb):
    lvl = "unknown"
    role = survey
    if survey == "educator":
        lvl = {1: "elementary", 4: "middle", 5: "high"}.get(to_int(ans.get("level")), "unknown")
    elif survey == "student":
        g = to_int(ans.get("grade"))
        lvl = "middle" if g in (1, 2, 3) else "high" if g in (4, 5, 6, 7) else "unknown"
    elif survey == "summer":
        rt = str(ans.get("role_type") or "")
        role = "librarian" if ("LIBRARIAN" in rt.upper() or to_int(rt) == 2) else "educator"
        lt = str(ans.get("lib_type") or "")
        if role == "librarian" and ("district-wide" in lt.lower() or to_int(lt) == 2):
            role = "district_librarian"
        lvl = "unknown"
    elif survey == "librarian":
        if to_int(ans.get("role_level")) == 0:
            role = "district_librarian"
        s = ans.get("serves")
        picks = set(str(p).strip() for p in (s if isinstance(s, list) else str(s or "").split(","))) - {""}
        lvl = {frozenset({"1"}): "elementary", frozenset({"2"}): "middle", frozenset({"3"}): "high"}.get(frozenset(picks), "mixed" if len(picks) > 1 else "unknown")
    # "tktk" anywhere in the pulled fields is the team's marker for its own test runs
    test_marker = any("tktk" in str(v).lower() for v in list(ans.values()) + list(emb.values()) if v not in (None, ""))
    policy_ok = any(str(ans.get(k) or "").strip() for k in ans if k.startswith("pol_"))
    meta["finished_qualtrics"] = meta["finished"]
    # Complete = named a school AND answered a phone-policy question. Qualtrics' Finished flag is recorded but not required.
    meta["finished"] = policy_ok and bool(str(ans.get("nces1") or ans.get("nces2") or ans.get("typed") or "").strip())
    return {
        **meta, "survey": survey, "role": role, "respondent_level": lvl, "test_marker": test_marker,
        "state_code": str(ans.get("state") or ans.get("dist_state") or "").strip(),
        "school_string": str(ans.get("nces1") or ans.get("nces2") or "").strip(),
        "school_found": (1 if str(ans.get("in_nces") or "").lower().startswith("yes") else 0 if str(ans.get("in_nces") or "").lower().startswith("no")
                         else (1 if to_int(ans.get("in_nces")) == 6 and survey == "summer" else to_int(ans.get("in_nces")))),
        "policy_answered": policy_ok,
        "typed_name_present": bool(str(ans.get("typed") or "").strip()),
        "zip_present": bool(str(ans.get("zip") or "").strip()),
        "consent": bool(str(ans.get("consent") or "").strip()), "role_type": to_int(ans.get("role_type")),
        "utm_source": (emb.get("utm_source") or "").strip().lower(), "utm_medium": (emb.get("utm_medium") or "").strip().lower(),
        "utm_campaign": (emb.get("utm_campaign") or "").strip().lower(), "src": (emb.get("src") or "").strip().lower(),
        "referer_host": host(emb.get("Referer")),
    }


def qualtrics_export(survey, survey_id, in_progress):
    import requests
    token, dc = os.environ["QUALTRICS_TOKEN"], os.environ["QUALTRICS_DATACENTER"]
    base, hdr = f"https://{dc}.qualtrics.com/API/v3", {"X-API-TOKEN": token, "Content-Type": "application/json"}
    body = {"format": "json", "compress": True, "questionIds": [q for q, t in FIELDS[survey].values()],
            "embeddedDataIds": EMBEDDED_BY_SURVEY.get(survey, []), "surveyMetadataIds": METADATA}
    if in_progress:
        body["exportResponsesInProgress"] = True

    def check(r, what):
        if not r.ok:
            try:
                msg = r.json()["meta"]["error"]["errorMessage"]
            except Exception:
                msg = "(no message)"
            sys.exit(f"Qualtrics returned {r.status_code} while {what} ({survey}): {msg}")

    r = requests.post(f"{base}/surveys/{survey_id}/export-responses", headers=hdr, json=body, timeout=60)
    check(r, "starting the export")
    pid = r.json()["result"]["progressId"]
    deadline = time.time() + 900
    while True:
        pr = requests.get(f"{base}/surveys/{survey_id}/export-responses/{pid}", headers=hdr, timeout=60)
        check(pr, "checking progress")
        p = pr.json()["result"]
        if p["status"] == "complete":
            break
        if p["status"] == "failed" or time.time() > deadline:
            sys.exit(f"export failed or timed out for {survey}")
        time.sleep(2)
    f = requests.get(f"{base}/surveys/{survey_id}/export-responses/{p['fileId']}/file", headers=hdr, timeout=300)
    check(f, "downloading the file")
    with zipfile.ZipFile(io.BytesIO(f.content)) as z:
        return json.loads(z.read(z.namelist()[0]))["responses"]


# ----------------------------------------------------------------------------- channels
def load_channel_rules(path, partner_slugs):
    rules = []
    for r in read_csv(path):
        rules.append((to_int(r.get("priority")) or 999, r))
    rules.sort(key=lambda x: x[0])

    def assign(resp):
        for _, r in rules:
            ok = True
            for col in ("utm_source", "utm_medium", "utm_campaign", "src", "referer_host"):
                pat = (r.get(col) or "").strip()
                if not pat:
                    continue
                if pat == "@partners":
                    ok = resp["utm_source"] in partner_slugs
                else:
                    ok = glob_match(pat, resp.get(col, ""))
                if not ok:
                    break
            if ok:
                partner = resp["utm_source"] if r["channel"] == "partner" else ""
                return r["channel"], partner
        return "unknown", ""
    return assign


# ----------------------------------------------------------------------------- time helpers
def week_start(d):
    d = d.astimezone(timezone.utc).date()
    return d - timedelta(days=d.weekday())


def pct(a, b):
    return round(100.0 * a / b, 1) if b else None


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--data", default=os.path.join(HERE, "data"))
    ap.add_argument("--inputs", default=os.path.join(HERE, "inputs"))
    ap.add_argument("--partners", default=None)
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    ap.add_argument("--from-csv", action="append", default=[], help="survey=path (student|educator|librarian)")
    ap.add_argument("--from-qualtrics", action="store_true")
    ap.add_argument("--include-in-progress", action="store_true", help="also pull in-progress responses (starts)")
    ap.add_argument("--today", default=None, help="UTC date for 'now' (testing)")
    ap.add_argument("--allow-empty", action="store_true", help="run with no current-cycle responses (before launch): earlier waves only")
    ap.add_argument("--publish-dir", default=None, help="also write the PUBLISHABLE subset here: index.html, assets/, coverage-data.json, lists/state/*.json (never responses.csv)")
    a = ap.parse_args()
    cfg = load_config(a.config)
    now = parse_dt(a.today) if a.today else datetime.now(timezone.utc)
    if a.today and len(a.today) == 10:
        now = now.replace(hour=23, minute=59, second=59)
    cycle_start = parse_dt(cfg["cycle_start"])
    test_mode = bool(cfg.get("test_mode"))
    if test_mode:
        cycle_start = parse_dt("2000-01-01T00:00:00Z")   # pre-launch: everything in Qualtrics is test data; show it all
    synthetic = bool(cfg.get("_synthetic"))
    alerts = []   # only for things that are broken or make the data inaccurate (pipeline errors, survey edits, stale inputs, test data)

    # ---- reference data
    schools = read_csv(os.path.join(a.data, "schools.csv"))
    by_id = {s["ncessch"]: s for s in schools}
    fold = lambda t: re.sub(r"\s+", " ", (t or "")).strip().casefold()
    by_key = collections.defaultdict(set)
    for s in schools:
        by_key[(s["state_name"], fold(s["school_name"]))].add(s["ncessch"])
    dd25 = os.path.join(a.inputs, "phonebans", "nces_dropdown_cities.csv")   # last year's dropdown strings (summer survey still uses them)
    if os.path.exists(dd25):
        from common import STATE_CODE_NAMES
        for r in read_csv(dd25):
            st = norm_state(STATE_CODE_NAMES.get(r["st"].strip().upper(), r["st"]))
            sid = r["ncessch"].strip().zfill(12)
            if sid in by_id:
                by_key[(st, fold(r["school_name"]))].add(sid)
    state_codes = load_state_codes(a.data)
    partners_path = a.partners or os.path.join(a.inputs, "partners.csv")
    partners = {r["partner"].strip().lower(): r for r in read_csv(partners_path) if r.get("partner", "").strip() and r["partner"] != "example-org"} if os.path.exists(partners_path) else {}
    assign_channel = load_channel_rules(os.path.join(a.inputs, "channels.csv"), set(partners))
    manual = {r["response_id"]: r["ncessch"].zfill(12) for r in read_csv(os.path.join(a.inputs, "manual_matches.csv")) if r.get("response_id")}
    test_ids = set()
    tp = os.path.join(a.inputs, "test_responses.txt")
    if os.path.exists(tp):
        for line in open(tp, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "@" not in line:
                test_ids.add(line)

    # ---- responses
    responses = []
    if a.from_qualtrics:
        for survey, sid in cfg["surveys"].items():
            rows = qualtrics_export(survey, sid, False)
            if a.include_in_progress:
                rows += qualtrics_export(survey, sid, True)
            if not rows:
                alerts.append(f"{survey}: Qualtrics returned 0 responses")
            responses += normalize_api(survey, rows)
            print(f"{survey}: {len(rows)} responses pulled")
    for spec in a.from_csv:
        survey, path = spec.split("=", 1)
        rows = read_qualtrics_csv(path)
        responses += normalize_csv(survey, rows)
        print(f"{survey}: {len(rows)} rows read from {os.path.basename(path)}")
    if not responses and not a.allow_empty:
        sys.exit("no responses: pass --from-qualtrics or --from-csv (or --allow-empty before launch)")

    # ---- state, match, channel
    codebook_misses = collections.Counter()
    for r in responses:
        book = state_codes.get(r["survey"], {})
        r["state_name"] = norm_state(book.get(r["state_code"], "")) if r["state_code"] else ""
        if r["state_code"] and not r["state_name"]:
            from common import state_code_of, STATE_CODE_NAMES
            code = state_code_of(r["state_code"])             # a label instead of a code (choice-text export)
            if code:
                r["state_name"] = norm_state(STATE_CODE_NAMES[code])
            else:
                codebook_misses[(r["survey"], r["state_code"])] += 1
        r["match_status"], r["ncessch"], r["match_method"], r["candidates"] = "", "", "", ""
        if r["role"] == "district_librarian":
            r["match_status"] = "district_only"
        elif r["response_id"] in manual:
            r["match_status"], r["ncessch"], r["match_method"] = "matched", manual[r["response_id"]], "manual"
        elif r["school_string"]:
            sid = r["school_string"].rsplit("[", 1)[-1].rstrip("]").strip() if r["school_string"].endswith("]") else ""
            if len(sid) == 12 and sid.isdigit() and sid in by_id:
                r["match_status"], r["ncessch"], r["match_method"] = "matched", sid, "id_in_string"
            else:
                cands = sorted(by_key.get((r["state_name"], fold(r["school_string"])), ()))
                if len(cands) == 1:
                    r["match_status"], r["ncessch"], r["match_method"] = "matched", cands[0], "exact"
                elif len(cands) > 1:
                    r["match_status"], r["candidates"] = "ambiguous", "|".join(cands)
                else:
                    r["match_status"] = "unmatched"   # string not in the list: the CSV on disk is stale
        elif r["typed_name_present"]:
            r["match_status"] = "unmatched_typed"
        else:
            r["match_status"] = "no_school"
        r["channel"], r["partner"] = assign_channel(r)
        if r["role"] == "student" and not r["utm_source"] and not r["src"] and r["referer_host"].endswith("qualtrics.com"):
            r["channel"] = "redirect_from_educator"
    if codebook_misses:
        alerts.append("state codes not in state_codes.csv (survey edited?): " + ", ".join(f"{s}:{c}×{n}" for (s, c), n in codebook_misses.most_common(5)))
    unmatched_strings = sum(1 for r in responses if r["match_status"] == "unmatched" and r["school_found"] == 1 and r["finished"])
    if unmatched_strings:
        alerts.append(f"{unmatched_strings} completes picked a dropdown school whose string is not in schools.csv: the dropdown CSV on disk is stale")

    # ---- validity
    responses.sort(key=lambda r: (r["recorded_at"] or datetime.max.replace(tzinfo=timezone.utc), r["response_id"]))
    for r in responses:
        reason, flags = "", []
        if r["distribution_channel"] == "preview":
            reason = "preview"
        elif r["recorded_at"] and r["recorded_at"] < cycle_start:
            reason = "before_cycle"
        elif not r["finished"]:
            reason = "unfinished"
        elif r["response_id"] in test_ids or (r.get("test_marker") and not test_mode):
            reason = "test"
        elif (r["survey"] == "educator" and r["role_type"] not in (1, None)) or (r["survey"] == "student" and not r["consent"]):
            reason = "ineligible_role"
        elif r["match_status"] == "district_only":
            reason = "district_only"
        elif not r.get("policy_answered", True):
            reason = "no_policy_data"   # unreachable once "complete" requires policy; kept for clarity
        elif r["match_status"] == "no_school":
            reason = "no_school"
        elif r["match_status"] in ("unmatched", "unmatched_typed"):
            reason = "unmatched"
        elif r["match_status"] == "ambiguous":
            reason = "ambiguous"
        elif r["duration_s"] is not None and r["duration_s"] < cfg.get("speeder_seconds", 60):
            reason = "speeder"
        # Duplicate detection is by Qualtrics response id only (each row is one response); email and IP are never pulled.
        sch = by_id.get(r["ncessch"])
        if sch and sch["level"] not in ("unknown", "other") and r["respondent_level"] not in ("unknown", "mixed", sch["level"]):
            flags.append("respondent_level_mismatch")
        if sch and not sch["eligible"]:
            flags.append("school_out_of_scope")
        r["valid"] = not reason
        r["invalid_reason"] = reason
        r["flags"] = ";".join(flags)
        r["week"] = str(week_start(r["recorded_at"])) if r["recorded_at"] else ""

    # ---- coverage rank per school (chronological), carrying 2025-26 reports forward except for fresh-start levels
    fresh_levels = set(cfg.get("fresh_start_levels", ["elementary"]))
    per_school = collections.defaultdict(list)
    for r in responses:
        if r["valid"]:
            per_school[r["ncessch"]].append(r)
    base_of = {}
    for s in schools:
        hist_n = to_int(s.get("hist_reports_2025")) or 0
        base_of[s["ncessch"]] = 0 if s["level"] in fresh_levels else hist_n
    for sid, rs in per_school.items():
        base = base_of.get(sid, 0)
        for i, r in enumerate(rs, 1):
            r["coverage_rank"] = base + i       # 1 = this report made the school represented for the first time
    pending = collections.Counter()
    for r in responses:
        if r["invalid_reason"] == "ambiguous":
            for c in r["candidates"].split("|"):
                pending[c] += 1

    # ---- school run-time columns
    for s in schools:
        rs = per_school.get(s["ncessch"], [])
        n = len(rs)
        hist_n = to_int(s.get("hist_reports_2025")) or 0
        fresh = s["level"] in fresh_levels
        base = 0 if fresh else hist_n
        total = base + n
        s["cur_valid_reports"] = str(n)
        s["carried_reports_2025"] = str(base)
        s["past_reports_uncounted"] = str(hist_n if fresh else 0)
        s["total_reports"] = str(total)
        s["cur_reports_by_role"] = ";".join(f"{k}:{v}" for k, v in sorted(collections.Counter(r["role"] for r in rs).items()))
        s["first_report_at"] = iso(rs[0]["recorded_at"]) if n else ""
        s["second_report_at"] = iso(rs[1]["recorded_at"]) if n > 1 else ""
        s["last_report_at"] = iso(rs[-1]["recorded_at"]) if n else ""
        # when the school became represented (0 -> 1) and when it reached 2+, counting carried reports
        s["newly_represented_at"] = iso(rs[0]["recorded_at"]) if (n and base == 0) else ""
        s["reached_two_at"] = iso(rs[1 - base]["recorded_at"]) if (base < 2 and n >= 2 - base and n > 0) else ""
        s["first_report_channel"] = rs[0]["channel"] if n else ""
        s["first_report_partner"] = rs[0]["partner"] if n else ""
        s["second_report_channel"] = rs[1]["channel"] if n > 1 else ""
        s["second_report_partner"] = rs[1]["partner"] if n > 1 else ""
        if not s["eligible"]:
            s["status"] = "OUT_OF_SCOPE"
        elif total >= 2:
            s["status"] = "COVERED_2_PLUS"
        elif total == 1:
            s["status"] = "COVERED_1"
        elif fresh and hist_n > 0:
            s["status"] = "STALE_NEEDS_REFRESH"
        else:
            s["status"] = "UNCOVERED"
        s["needs_refresh"] = "true" if (fresh and hist_n > 0 and n == 0 and s["eligible"]) else ""
        s["pending_ambiguous"] = str(pending.get(s["ncessch"], 0))
        fl = []
        if s.get("name_ambiguous"):
            fl.append("name_ambiguous")
        if any(r["match_method"] == "manual" for r in rs):
            fl.append("manual_match")
        if any("respondent_level_mismatch" in r["flags"] for r in rs):
            fl.append("respondent_level_mismatch")
        s["flags"] = ";".join(fl)

    # ---- aggregates
    elig = [s for s in schools if s["eligible"]]
    this_wk = week_start(now)
    last_wk = this_wk - timedelta(days=7)

    def block(ss):
        n = len(ss)
        tot = lambda s: int(s["total_reports"])
        cov = [s for s in ss if tot(s) >= 1]
        two = sum(1 for s in ss if tot(s) >= 2)
        carried_only = sum(1 for s in ss if int(s["carried_reports_2025"]) >= 1 and int(s["cur_valid_reports"]) == 0)
        stale = sum(1 for s in ss if s["status"] == "STALE_NEEDS_REFRESH")
        cur_cov = sum(1 for s in ss if int(s["cur_valid_reports"]) >= 1)
        return {"eligible": n, "covered": len(cov), "remaining": n - len(cov), "pct": pct(len(cov), n),
                "covered_this_cycle": cur_cov, "pct_this_cycle": pct(cur_cov, n),
                "carried_only": carried_only, "stale": stale, "past_uncounted": sum(1 for s in ss if int(s["past_reports_uncounted"]) > 0),
                "depth": {"0": n - len(cov), "1": len(cov) - two, "2plus": two},
                "depth_pct": {"0": pct(n - len(cov), n), "1": pct(len(cov) - two, n), "2plus": pct(two, n)},
                "pending_ambiguous": sum(1 for s in ss if int(s["pending_ambiguous"]) > 0 and tot(s) == 0)}

    coverage = {"overall": block(elig), "by_level": {lv: block([s for s in elig if s["level"] == lv]) for lv in LEVELS},
                "mshs": block([s for s in elig if s["level"] in ("middle", "high")])}
    coverage["out_of_scope"] = dict(collections.Counter(s["ineligible_reason"] for s in schools if not s["eligible"]))
    role_sch = collections.defaultdict(set)
    for r in responses:
        if r["valid"]:
            role_sch[r["role"]].add(r["ncessch"])
    by_role = {}
    for role in ("student", "educator", "librarian"):
        ids = role_sch.get(role, set())
        rs = [r for r in responses if r["valid"] and r["role"] == role]
        by_role[role] = {"valid_reports": len(rs), "schools": len(ids),
                         "by_level": {lv: sum(1 for sid in ids if by_id.get(sid, {}).get("level") == lv) for lv in LEVELS},
                         "new_schools": sum(1 for r in rs if r.get("coverage_rank") == 1),
                         "this_week": sum(1 for r in rs if r["week"] == str(this_wk)), "last_week": sum(1 for r in rs if r["week"] == str(last_wk)),
                         "by_survey": dict(collections.Counter(r["survey"] for r in rs)),
                         "top_states": [{"st": st, "schools": n} for st, n in collections.Counter(by_id[sid]["st"] for sid in ids if sid in by_id).most_common(8)]}
    by_role["district_librarian"] = {"reports": sum(1 for r in responses if r["role"] == "district_librarian" and r["finished"] and r["distribution_channel"] != "preview")}
    lib_rows = [r for r in responses if r["survey"] == "librarian" and r["distribution_channel"] != "preview"
                and not (r["recorded_at"] and r["recorded_at"] < cycle_start) and (r["school_string"] or r["typed_name_present"] or r["role"] == "district_librarian")]
    by_role["librarian"]["by_branch"] = {
        "district": sum(1 for r in lib_rows if r["role"] == "district_librarian"),
        "elementary_only": sum(1 for r in lib_rows if r["role"] == "librarian" and r["respondent_level"] == "elementary"),
        "mshs": sum(1 for r in lib_rows if r["role"] == "librarian" and r["respondent_level"] in ("middle", "high", "mixed")),
        "valid_mshs": sum(1 for r in lib_rows if r["valid"]),
    }
    coverage["by_role"] = by_role

    # new schools (first report) by week / day / level / state / channel
    firsts = [s for s in elig if s["newly_represented_at"]]
    for s in firsts:
        s["_first"] = parse_dt(s["newly_represented_at"])
    twos = [s for s in elig if s["reached_two_at"]]
    for s in twos:
        s["_second"] = parse_dt(s["reached_two_at"])
    for s in firsts:
        s.setdefault("_second", parse_dt(s["reached_two_at"]) if s["reached_two_at"] else None)
    weeks_n = cfg.get("weekly_chart_weeks", 12)
    week_list = [w for w in (this_wk - timedelta(days=7 * i) for i in range(weeks_n, -1, -1)) if w >= week_start(cycle_start)]
    weekly = []
    for w in week_list:
        in_w = [s for s in firsts if week_start(s["_first"]) == w]
        weekly.append({"week_start": str(w), "partial": w == this_wk, "new": len(in_w),
                       "by_level": {lv: sum(1 for s in in_w if s["level"] == lv) for lv in LEVELS},
                       "second_reports": sum(1 for s in twos if week_start(s["_second"]) == w),
                       "valid_responses": sum(1 for r in responses if r["valid"] and r["week"] == str(w))})
    daily = []
    for i in range(29, -1, -1):
        d = (now - timedelta(days=i)).date()
        daily.append({"date": str(d), "new": sum(1 for s in firsts if s["_first"].date() == d),
                      "valid_responses": sum(1 for r in responses if r["valid"] and r["recorded_at"] and r["recorded_at"].date() == d)})
    complete_weeks = [w for w in weekly if not w["partial"]][-cfg.get("rolling_weeks", 4):]
    rolling = statistics.mean([w["new"] for w in complete_weeks]) if complete_weeks else 0.0
    new_this = next((w["new"] for w in weekly if w["week_start"] == str(this_wk)), 0)
    new_last = next((w["new"] for w in weekly if w["week_start"] == str(last_wk)), 0)
    prev_wk = next((w["new"] for w in weekly if w["week_start"] == str(last_wk - timedelta(days=7))), 0)

    def weeks_to_full(remaining, rate):
        return round(remaining / rate, 1) if rate else None

    level_rolling = {lv: (statistics.mean([w["by_level"][lv] for w in complete_weeks]) if complete_weeks else 0.0) for lv in LEVELS}
    target = parse_dt(cfg.get("target_date")) if cfg.get("target_date") else None
    weeks_left = max(0.0, (target - now).total_seconds() / (7 * 86400)) if target else None
    needed = round(coverage["overall"]["remaining"] / weeks_left) if weeks_left else None
    pace = {"target_date": cfg.get("target_date"), "weeks_left": round(weeks_left, 1) if weeks_left is not None else None,
            "needed_per_week": needed, "current_per_week": round(rolling),
            "pct_of_needed": pct(rolling, needed) if needed else None,
            "by_level": {lv: {"needed_per_week": (round(coverage["by_level"][lv]["remaining"] / weeks_left) if weeks_left else None),
                              "current_per_week": round(level_rolling[lv])} for lv in LEVELS}}
    velocity = {
        "pace": pace,
        "this_week": {"week_start": str(this_wk), "new": new_this, "days_elapsed": (now.date() - this_wk).days + 1},
        "last_week": {"week_start": str(last_wk), "new": new_last, "pct_change_vs_prev": pct(new_last - prev_wk, prev_wk) if prev_wk else None},
        "rolling_avg_weekly_new": round(rolling, 1),
        "weeks_to_full": {"overall": weeks_to_full(coverage["overall"]["remaining"], rolling),
                          "by_level": {lv: weeks_to_full(coverage["by_level"][lv]["remaining"], level_rolling[lv]) for lv in LEVELS}},
        "weekly": weekly, "daily": daily,
    }
    gaps = [(s["_second"] - s["_first"]).total_seconds() / 86400 for s in firsts if s.get("_second")]
    depth = {"one_report": coverage["overall"]["depth"]["1"], "two_plus": coverage["overall"]["depth"]["2plus"],
             "new_two_plus_this_week": sum(1 for s in twos if week_start(s["_second"]) == this_wk),
             "new_two_plus_last_week": sum(1 for s in twos if week_start(s["_second"]) == last_wk),
             "median_days_first_to_second": round(statistics.median(gaps), 1) if gaps else None}

    # states
    states = []
    for st_name in sorted({s["state_name"] for s in elig}):
        ss = [s for s in elig if s["state_name"] == st_name]
        b = block(ss)
        states.append({"state": st_name, "st": ss[0]["st"], **{k: b[k] for k in ("eligible", "covered", "remaining", "pct", "stale")},
                       "one": b["depth"]["1"], "two_plus": b["depth"]["2plus"],
                       "new_this_week": sum(1 for s in ss if s["newly_represented_at"] and week_start(parse_dt(s["newly_represented_at"])) == this_wk),
                       "new_last_week": sum(1 for s in ss if s["newly_represented_at"] and week_start(parse_dt(s["newly_represented_at"])) == last_wk),
                       "by_level": {lv: {"eligible": sum(1 for s in ss if s["level"] == lv), "covered": sum(1 for s in ss if s["level"] == lv and int(s["total_reports"]) >= 1),
                                         "two_plus": sum(1 for s in ss if s["level"] == lv and int(s["total_reports"]) >= 2),
                                         "stale": sum(1 for s in ss if s["level"] == lv and s["status"] == "STALE_NEEDS_REFRESH")} for lv in LEVELS}})
    nat = coverage["overall"]["pct"] or 0
    lagging = {x["state"] for x in states if x["pct"] is not None and x["pct"] < nat - cfg.get("lagging_state_gap_points", 10)}
    for x in states:
        x["lagging"] = x["state"] in lagging

    # channels / partners funnel
    def funnel(rs):
        comp = [r for r in rs if r["finished"] and r["distribution_channel"] != "preview" and (not r["recorded_at"] or r["recorded_at"] >= cycle_start)]
        val = [r for r in comp if r["valid"]]
        new = sum(1 for r in val if r.get("coverage_rank") == 1 and by_id[r["ncessch"]]["eligible"])
        sec = sum(1 for r in val if r.get("coverage_rank") == 2 and by_id[r["ncessch"]]["eligible"])
        red = sum(1 for r in val if (r.get("coverage_rank") or 0) >= 3)
        return {"starts": len(rs), "completes": len(comp), "valid": len(val), "matched": sum(1 for r in val if r["ncessch"]),
                "new_schools": new, "second_reports": sec, "redundant": red,
                "conv_start_complete": pct(len(comp), len(rs)), "conv_complete_valid": pct(len(val), len(comp)), "conv_valid_new": pct(new, len(val)),
                "by_level_new": {lv: sum(1 for r in val if r.get("coverage_rank") == 1 and by_id[r["ncessch"]]["level"] == lv) for lv in LEVELS}}
    in_cycle = [r for r in responses if not (r["recorded_at"] and r["recorded_at"] < cycle_start) and r["distribution_channel"] != "preview"]
    channels = []
    for ch in sorted({r["channel"] for r in in_cycle}):
        rs = [r for r in in_cycle if r["channel"] == ch]
        channels.append({"channel": ch, **funnel(rs), "new_this_week": sum(1 for r in rs if r["valid"] and r.get("coverage_rank") == 1 and r["week"] == str(this_wk)),
                         "new_last_week": sum(1 for r in rs if r["valid"] and r.get("coverage_rank") == 1 and r["week"] == str(last_wk))})
    channels.sort(key=lambda c: (-c["new_schools"], -c["valid"]))
    partner_rows = []
    for slug, p in partners.items():
        rs = [r for r in in_cycle if r["partner"] == slug]
        f = funnel(rs)
        stage = "activated" if p.get("activated_on") else "agreed" if p.get("agreed_on") else "contacted" if p.get("contacted_on") else "identified"
        val = [r for r in rs if r["valid"]]
        cost = float(p["cost_usd"]) if p.get("cost_usd") else None
        hours = float(p["staff_hours"]) if p.get("staff_hours") else None
        partner_rows.append({"partner": slug, "display_name": p.get("display_name") or slug, "type": p.get("type", ""), "owner": p.get("owner", ""),
                             "stage": stage, "contacted_on": p.get("contacted_on", ""), "agreed_on": p.get("agreed_on", ""), "activated_on": p.get("activated_on", ""),
                             "expected_levels": p.get("expected_levels", ""), "expected_states": p.get("expected_states", ""), **f,
                             "states_reached": sorted({by_id[r["ncessch"]]["st"] for r in val if r["ncessch"]}),
                             "levels_reached": sorted({by_id[r["ncessch"]]["level"] for r in val if r["ncessch"]}),
                             "cost_usd": cost, "staff_hours": hours,
                             "cost_per_valid": round(cost / f["valid"], 2) if cost and f["valid"] else None,
                             "cost_per_new_school": round(cost / f["new_schools"], 2) if cost and f["new_schools"] else None,
                             "hours_per_new_school": round(hours / f["new_schools"], 2) if hours and f["new_schools"] else None})
    partner_rows.sort(key=lambda x: (-x["new_schools"], x["partner"]))

    for role in ("student", "educator", "librarian"):
        by_role[role]["weekly"] = [{"week_start": w["week_start"], "partial": w["partial"],
                                    "reports": sum(1 for r in responses if r["valid"] and r["role"] == role and r["week"] == w["week_start"])} for w in weekly]
    # ---- overview extras: pace detail, 4-week sources, callouts (rule engine, COVERAGE-DESIGN.md §7)
    pace["pts_per_week"] = round(100.0 * rolling / coverage["overall"]["eligible"], 2) if coverage["overall"]["eligible"] else None
    pace["projected_full_date"] = (now + timedelta(weeks=coverage["overall"]["remaining"] / rolling)).strftime("%Y-%m-%d") if rolling else None
    four_wk_start = this_wk - timedelta(days=28)
    recent = [r for r in in_cycle if r["recorded_at"] and r["recorded_at"].date() >= four_wk_start]
    channels_4w = []
    for ch in sorted({r["channel"] for r in recent}):
        rs = [r for r in recent if r["channel"] == ch]
        f = funnel(rs)
        channels_4w.append({"channel": ch, "valid": f["valid"], "new_schools": f["new_schools"], "conv_valid_new": f["conv_valid_new"]})
    channels_4w.sort(key=lambda c: (-c["new_schools"], -c["valid"]))
    callouts = []
    # (a) average daily valid responses, last 7 days vs the 7 before
    valid_by_day = {x["date"]: x["valid_responses"] for x in daily}
    last7 = [valid_by_day.get(str((now - timedelta(days=i)).date()), 0) for i in range(0, 7)]
    prev7 = [valid_by_day.get(str((now - timedelta(days=i)).date()), 0) for i in range(7, 14)]
    avg7, avgp = sum(last7) / 7.0, sum(prev7) / 7.0
    if sum(last7) or sum(prev7):
        delta = pct(avg7 - avgp, avgp) if avgp else None
        callouts.append({"rule": "avg_daily_responses", "at_stake": 0, "stat": f"{avg7:,.0f}",
                         "title": "valid reports per day, last 7 days.",
                         "body": (f"{'Up' if delta >= 0 else 'Down'} {abs(delta):.0f}% on the 7 days before." if delta is not None else "First week of responses.") + f" {sum(last7):,} this week.",
                         "action": "Data quality", "target": "quality"})
    # (b) spike by place: a state whose valid reports in the last 7 days are at least 3x the 7 days before (min 20)
    def in_window(r, lo, hi):
        return r["recorded_at"] and lo <= r["recorded_at"].date() <= hi
    d7, d14 = (now - timedelta(days=6)).date(), (now - timedelta(days=13)).date()
    by_state_now = collections.Counter(r["state_name"] for r in responses if r["valid"] and in_window(r, d7, now.date()))
    by_state_prev = collections.Counter(r["state_name"] for r in responses if r["valid"] and in_window(r, d14, d7 - timedelta(days=1)))
    spikes = []
    for st_name, n in by_state_now.items():
        prev = by_state_prev.get(st_name, 0)
        if n >= 20 and n >= 3 * max(prev, 1):
            spikes.append((n / max(prev, 1), st_name, n, prev))
    if spikes:
        ratio, st_name, n, prev = max(spikes)
        rs_st = [r for r in responses if r["valid"] and r["state_name"] == st_name and in_window(r, d7, now.date())]
        top_d = collections.Counter(by_id[r["ncessch"]].get("district") or "" for r in rs_st if r["ncessch"]).most_common(1)
        top_c = collections.Counter(by_id[r["ncessch"]].get("city") or "" for r in rs_st if r["ncessch"]).most_common(1)
        where = ", ".join(x for x in [top_d[0][0] if top_d and top_d[0][0] else "", top_c[0][0] if top_c and top_c[0][0] else ""] if x)
        callouts.append({"rule": "response_spike", "at_stake": 0, "stat": f"{n:,}",
                         "title": f"reports from {st_name} in the last 7 days, {ratio:.0f}x the week before.",
                         "body": (f"Most from {where}. " if where else "") + "Worth matching to that week's outreach so the source gets credit.",
                         "action": f"Gaps: {st_name}", "target": f"gaps?state={by_id[rs_st[0]['ncessch']]['st'] if rs_st and rs_st[0]['ncessch'] else ''}"})
    elif sum(by_state_now.values()) or sum(by_state_prev.values()):
        callouts.append({"rule": "response_spike", "at_stake": 0, "stat": "—", "title": "No state spiked this week.",
                         "body": "A spike is a state with at least 20 valid reports in 7 days and 3x the week before.", "action": "Gaps", "target": "gaps"})
    # (c) new schools this week vs the pace the target needs
    if pace.get("needed_per_week") and (new_this or new_last or rolling):
        callouts.append({"rule": "pace_vs_needed", "at_stake": 0, "stat": f"+{new_this:,}",
                         "title": "new schools this week so far." if new_this else "new schools this week so far.",
                         "body": f"Last week {new_last:,}. The target date needs about {pace['needed_per_week']:,} per week; 4-week average is {rolling:,.0f}.",
                         "action": "Gaps", "target": "gaps"})
    callouts = callouts[:3]

    # ---- funnel (visits come from inputs/traffic.csv when the team pastes analytics in; Qualtrics has no page views)
    traffic = []
    tpath = os.path.join(a.inputs, "traffic.csv")
    if os.path.exists(tpath):
        for r in read_csv(tpath):
            dt_ = parse_dt(r.get("date"))
            n_ = to_int(r.get("visits"))
            if dt_ and n_ is not None and dt_ >= cycle_start:
                traffic.append((dt_, n_))
    def funnel_block(rs, since=None):
        f = funnel(rs)
        vis = sum(n_ for dt_, n_ in traffic if since is None or dt_.date() >= since) if traffic else None
        return {"visits": vis, "starts": f["starts"], "completes": f["completes"], "valid": f["valid"], "matched": f["matched"],
                "new_schools": f["new_schools"], "second_reports": f["second_reports"],
                "conv_visit_start": pct(f["starts"], vis) if vis else None, "conv_start_complete": f["conv_start_complete"],
                "conv_complete_valid": f["conv_complete_valid"], "conv_valid_new": f["conv_valid_new"]}
    funnel_data = {"cycle": funnel_block(in_cycle), "last_4w": funnel_block(recent, four_wk_start), "visits_loaded": bool(traffic)}

    # data quality
    cyc = [r for r in responses if not (r["recorded_at"] and r["recorded_at"] < cycle_start)]
    completes = [r for r in cyc if r["finished"] and r["distribution_channel"] != "preview"]
    valid = [r for r in cyc if r["valid"]]
    reached_school = [r for r in completes if r["match_status"] not in ("no_school", "district_only")]
    queue = [r for r in completes if r["match_status"] in ("unmatched_typed", "unmatched", "ambiguous") and r["response_id"] not in manual]
    oldest = min((r["recorded_at"] for r in queue if r["recorded_at"]), default=None)
    dq = {"submissions": len(cyc), "completes": len(completes), "valid": len(valid),
          "by_survey": {sv: {"submissions": sum(1 for r in cyc if r["survey"] == sv), "completes": sum(1 for r in completes if r["survey"] == sv),
                             "valid": sum(1 for r in valid if r["survey"] == sv)} for sv in FIELDS if any(r["survey"] == sv for r in cyc)},
          "excluded": {k: sum(1 for r in cyc if r["invalid_reason"] == k) for k in INVALID_ORDER if any(r["invalid_reason"] == k for r in cyc)},
          "before_cycle_total": sum(1 for r in responses if r["invalid_reason"] == "before_cycle"),
          "match_rate": pct(sum(1 for r in reached_school if r["match_status"] == "matched"), len(reached_school)),
          "queue": {"size": len(queue), "oldest_days": (now - oldest).days if oldest else None,
                    "by_type": dict(collections.Counter(r["match_status"] for r in queue))},
          "pending_ambiguous_responses": sum(1 for r in completes if r["match_status"] == "ambiguous"),
          "pending_ambiguous_schools_uncounted": coverage["overall"]["pending_ambiguous"],
          "unknown_channel_share": pct(sum(1 for r in valid if r["channel"] == "unknown"), len(valid)),
          "redirect_from_educator": sum(1 for r in valid if r["channel"] == "redirect_from_educator"),
          "district_reports": sum(1 for r in completes if r["role"] == "district_librarian"),
          "respondent_level_mismatch": sum(1 for r in valid if "respondent_level_mismatch" in r["flags"]),
          "codebook_misses": sum(codebook_misses.values()), "manual_matches_applied": sum(1 for r in responses if r["match_method"] == "manual"),
          "needs_review": sum(1 for r in completes if r["invalid_reason"] in ("unmatched", "ambiguous", "speeder"))}
    if all(s["level"] == "unknown" for s in schools):
        alerts.append("NCES CCD not loaded: school levels unknown, level tiles are empty (see COVERAGE-DESIGN.md §10.6)")
    if not any(s.get("hist_represented") == "true" for s in schools):
        alerts.append("no historical (2025-26) layer loaded: STALE_NEEDS_REFRESH cannot be computed")

    # ---- lists
    out_lists = os.path.join(a.out, "lists")
    os.makedirs(out_lists, exist_ok=True)
    os.makedirs(os.path.join(a.out, "queue"), exist_ok=True)
    os.makedirs(os.path.join(a.out, "reports"), exist_ok=True)
    LIST_COLS = ["ncessch", "school_name", "district", "state_name", "st", "level", "urbanicity", "enrollment", "status",
                 "total_reports", "cur_valid_reports", "carried_reports_2025", "past_reports_uncounted", "hist_reports_2025",
                 "first_report_at", "first_report_channel", "pending_ambiguous"]
    lists = {}
    def emit(name, rows):
        write_csv(os.path.join(out_lists, name), rows, LIST_COLS)
        lists[name] = len(rows)
    remaining = [s for s in elig if s["status"] in ("UNCOVERED", "STALE_NEEDS_REFRESH")]
    emit("remaining_all.csv", remaining)
    emit("stale_elementary.csv", [s for s in elig if s["needs_refresh"] and s["level"] == "elementary"])
    emit("stale_all_levels.csv", [s for s in elig if s["needs_refresh"]])
    emit("one_report.csv", [s for s in elig if s["status"] == "COVERED_1"])
    emit("lagging_states_uncovered.csv", [s for s in remaining if s["state_name"] in lagging])
    emit("pending_ambiguous.csv", [s for s in elig if int(s["pending_ambiguous"]) > 0 and int(s["cur_valid_reports"]) == 0])
    for p in partner_rows:
        want_lv = set(filter(None, (p["expected_levels"] or "").split("|")))
        want_st = set(filter(None, (p["expected_states"] or "").split("|")))
        rows = [s for s in remaining if (not want_lv or s["level"] in want_lv or s["level"] == "unknown") and (not want_st or s["st"] in want_st)]
        emit(f"partner_remaining_{p['partner']}.csv", rows)
    write_csv(os.path.join(a.out, "queue", "unmatched.csv"),
              sorted(({"response_id": r["response_id"], "survey": r["survey"], "recorded_at": iso(r["recorded_at"]), "state_name": r["state_name"],
                       "match_status": r["match_status"], "school_found": r["school_found"], "typed_name_present": r["typed_name_present"],
                       "zip_present": r["zip_present"], "candidates": r["candidates"]} for r in queue), key=lambda x: x["recorded_at"]),
              ["response_id", "survey", "recorded_at", "state_name", "match_status", "school_found", "typed_name_present", "zip_present", "candidates"])

    pending_launch = len(cyc) == 0
    # ---- outputs
    for r in responses:
        r["start_at"], r["recorded_at"] = iso(r["start_at"]), iso(r["recorded_at"])
        r["finished"] = "true" if r["finished"] else ""
        r["finished_qualtrics"] = "true" if r.get("finished_qualtrics") else ""
        r["valid"] = "true" if r["valid"] else ""
        r["typed_name_present"] = "true" if r["typed_name_present"] else ""
        r["zip_present"] = "true" if r["zip_present"] else ""
        r["coverage_rank"] = r.get("coverage_rank", "")
    write_csv(os.path.join(a.out, "responses.csv"), responses, RESPONSE_COLUMNS)
    base_cols = [c for c in schools[0].keys() if not c.startswith("_")]
    write_csv(os.path.join(a.out, "schools.csv"), schools, base_cols)

    data = {
        "generated_at": iso(datetime.now(timezone.utc)), "as_of": iso(now), "synthetic": synthetic, "test_mode": test_mode,
        "pending_launch": pending_launch,
        "cycle": {"name": cfg["cycle_name"], "start": iso(cycle_start)}, "fresh_start_levels": sorted(fresh_levels),
        "ccd_loaded": any(s["level"] != "unknown" for s in schools), "historical_loaded": any(s.get("hist_represented") == "true" for s in schools),
        "alerts": alerts, "levels": LEVELS, "coverage": coverage, "depth": depth, "velocity": velocity, "states": states,
        "channels": channels, "channels_4w": channels_4w, "funnel": funnel_data, "callouts": callouts, "partners": partner_rows, "dq": dq, "lists": lists,
        "report_path": f"reports/week-{this_wk.isocalendar()[0]}-W{this_wk.isocalendar()[1]:02d}.md", "config": {k: v for k, v in cfg.items() if not k.startswith("_")},
    }
    with open(os.path.join(a.out, "coverage-data.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    # per-state rosters: schools / districts in the data, schools / districts not yet (the team's four-column view)
    os.makedirs(os.path.join(a.out, "lists", "state"), exist_ok=True)
    rosters = {}
    for st_name in sorted({s["state_name"] for s in elig}):
        ss = [s for s in elig if s["state_name"] == st_name]
        in_ids = [s for s in ss if int(s["total_reports"]) >= 1]
        dist_in = sorted({(s["district"] or "(no district)") for s in in_ids})
        dist_all = sorted({(s["district"] or "(no district)") for s in ss})
        roster = {"state": st_name, "st": ss[0]["st"],
                  "schools_in": sorted(s["school_name"] for s in in_ids),
                  "schools_out": sorted(s["school_name"] for s in ss if int(s["total_reports"]) == 0),
                  "districts_in": dist_in, "districts_out": [d_ for d_ in dist_all if d_ not in set(dist_in)]}
        roster["counts"] = {k: len(roster[k]) for k in ("schools_in", "schools_out", "districts_in", "districts_out")}
        with open(os.path.join(a.out, "lists", "state", f"{ss[0]['st']}.json"), "w", encoding="utf-8") as fh:
            json.dump(roster, fh)
        rosters[ss[0]["st"]] = roster["counts"]
    data["rosters"] = rosters
    with open(os.path.join(a.out, "coverage-data.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    if a.publish_dir:
        import shutil as _sh
        pub = a.publish_dir
        if os.path.isdir(pub):
            _sh.rmtree(pub)
        os.makedirs(os.path.join(pub, "lists"), exist_ok=True)
        _sh.copy(os.path.join(a.out, "coverage-data.json"), pub)
        # the page and its assets come straight from dashboard/, so a clean checkout publishes them too
        _sh.copy(os.path.join(HERE, "dashboard", "index.html"), pub)
        if os.path.isdir(os.path.join(HERE, "dashboard", "assets")):
            _sh.copytree(os.path.join(HERE, "dashboard", "assets"), os.path.join(pub, "assets"))
        _sh.copytree(os.path.join(a.out, "lists", "state"), os.path.join(pub, "lists", "state"))
        for must in ("index.html", "coverage-data.json", os.path.join("assets", "us-states-paths.json")):
            if not os.path.exists(os.path.join(pub, must)):
                sys.exit(f"publish failed: {must} missing from {pub}")
        print("publishable copy:", pub, "(page, aggregates, per-state school rosters; no response-level file)")
    import shutil
    dash = os.path.join(HERE, "dashboard", "index.html")
    if os.path.exists(dash):
        shutil.copy(dash, os.path.join(a.out, "index.html"))
    assets = os.path.join(HERE, "dashboard", "assets")
    if os.path.isdir(assets):
        shutil.copytree(assets, os.path.join(a.out, "assets"), dirs_exist_ok=True)
    o = coverage["overall"]
    print(f"coverage: {o['covered']}/{o['eligible']} eligible schools ({o['pct']}%), {o['remaining']} remaining, "
          f"{o['depth']['2plus']} with 2+; new this week {new_this}, last week {new_last}, 4-wk avg {rolling:.1f}")
    print(f"responses: {dq['submissions']} in cycle, {dq['completes']} completes, {dq['valid']} valid; excluded {dq['excluded']}; match rate {dq['match_rate']}%")
    for al in alerts:
        print("ALERT:", al)
    print("wrote", os.path.join(a.out, "coverage-data.json"))


def write_weekly_report(d, path):
    c, v, dq = d["coverage"], d["velocity"], d["dq"]
    o = c["overall"]
    fmt = lambda x: "—" if x is None else (f"{x:,}" if isinstance(x, int) else f"{x}")
    lines = [f"# Screens in Schools coverage — week of {v['this_week']['week_start']}", "",
             f"*As of {d['as_of'][:10]}. Cycle {d['cycle']['name']}.*" + (" **SYNTHETIC DATA.**" if d["synthetic"] else ""), ""]
    if d["alerts"]:
        lines += ["**Attention:** " + " · ".join(d["alerts"]), ""]
    lines += ["## 1. Coverage now", f"- Overall: **{fmt(o['pct'])}%** ({fmt(o['covered'])} of {fmt(o['eligible'])} eligible schools), {fmt(o['remaining'])} remaining. This cycle alone: {fmt(o['pct_this_cycle'])}%; {fmt(o['carried_only'])} schools count only through 2025-26 reports (middle/high carry forward)."]
    for lv in ("elementary", "middle", "high"):
        b = c["by_level"][lv]
        if b["eligible"]:
            lines.append(f"- {lv.title()}: {fmt(b['pct'])}% ({fmt(b['covered'])}/{fmt(b['eligible'])}), {fmt(b['remaining'])} remaining, {fmt(b['stale'])} stale.")
    lw = v["last_week"]
    lines += ["", "## 2. Velocity",
              f"- New schools last week: **{fmt(lw['new'])}** ({'+' if (lw['pct_change_vs_prev'] or 0) >= 0 else ''}{fmt(lw['pct_change_vs_prev'])}% vs the week before). This week so far: {fmt(v['this_week']['new'])} ({v['this_week']['days_elapsed']} days).",
              f"- 4-week average: {fmt(v['rolling_avg_weekly_new'])} new schools/week → {fmt(v['weeks_to_full']['overall'])} weeks to 100% at this pace.",
              "", "## 3. Depth",
              f"- Schools with exactly one report: {fmt(d['depth']['one_report'])}. Moved to 2+ last week: {fmt(d['depth']['new_two_plus_last_week'])}. Median days first → second: {fmt(d['depth']['median_days_first_to_second'])}.",
              "", "## 4. What is driving growth"]
    for ch in d["channels"][:3]:
        lines.append(f"- {ch['channel']}: {fmt(ch['new_schools'])} new schools from {fmt(ch['valid'])} valid reports (valid→new {fmt(ch['conv_valid_new'])}%).")
    for p in [p for p in d["partners"] if p["stage"] == "activated"][:3]:
        st = p['states_reached']
        lines.append(f"- Partner {p['display_name']}: {fmt(p['new_schools'])} new schools, {fmt(p['valid'])} valid, {len(st)} states" + (f" ({', '.join(st[:8])}{'…' if len(st) > 8 else ''})" if st else "") + ".")
    big = [s for s in d["states"] if s["eligible"] >= 500 and s["pct"] is not None]
    low = sorted(big, key=lambda s: s["pct"])[:5]
    gain = sorted(d["states"], key=lambda s: -s["new_last_week"])[:5]
    lines += ["", "## 5. Gaps",
              "- Lowest coverage (≥500 schools): " + ", ".join(f"{s['st']} {fmt(s['pct'])}%" for s in low),
              "- Biggest gainers last week: " + ", ".join(f"{s['st']} +{s['new_last_week']}" for s in gain if s["new_last_week"]),
              "", "## 6. Refresh",
              f"- Elementary schools that reported in 2025-26 and need the new {d['cycle']['name']} survey before they count: {fmt(c['by_level']['elementary']['stale'])}.",
              "", "## 7. Data quality",
              f"- {fmt(dq['submissions'])} submissions, {fmt(dq['completes'])} completes, {fmt(dq['valid'])} valid. Excluded: " + ", ".join(f"{k} {v_}" for k, v_ in dq["excluded"].items()) + ".",
              f"- Match rate {fmt(dq['match_rate'])}%. Manual queue: {fmt(dq['queue']['size'])} (oldest {fmt(dq['queue']['oldest_days'])} days). Unknown channel: {fmt(dq['unknown_channel_share'])}% of valid.",
              "", "## 8. Decisions needed"]
    lines += [f"- {al}" for al in d["alerts"]] or ["- none flagged"]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
