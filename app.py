"""Salesforce live dashboard backend (FastAPI). Auth = OAuth 2.0 Client Credentials (Connected App) + REST API.
Credentials come from environment variables / the local .env file (never commit or share it):
  SF_CLIENT_ID, SF_CLIENT_SECRET, SF_INSTANCE_URL, SF_API_VERSION

Inputs implemented (Owner by End Time):
  * Roster: fixed email list (roster.csv) -> active User.Id. POD Name in Salesforce is NOT used.
    Unmatched emails are shown with zeros + "not in Salesforce yet"; no lookalike substitution.
  * Window: rolling, (today-37) .. (today-7) in whole IST days, extended back to the Sunday on/before the start.
    UTC bounds: End_Time_of_Incident__c >= start 00:00 IST and < (end+1) 00:00 IST.
  * Main query in week-aligned slices (<= 7 days): Solved, Category__c = Software, Type != Alert,
    OwnerId IN (roster ids).  Cross-check COUNT without OwnerId filter (run before AND after).
  * Non-roster = all-owner count - roster total; top owners via GROUP BY Owner.Name.
  * Dating = End Time only; current owner only; each case once; IST day/week buckets (Sun-Sat).
"""
import base64, csv, hmac, json, os, random, ssl, threading, time, urllib.error, urllib.parse, urllib.request
import datetime as dt
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text())
MOCK = os.getenv("MOCK") == "1" or CFG.get("mock", False)
F = CFG["fields"]
TZ = dt.timezone(dt.timedelta(minutes=CFG.get("timezone_offset_minutes", 330)))  # IST
UTC = dt.timezone.utc
MOCK_MISSING = {"sarthak.j@greyorange.com"}  # mock only: pretend this user is not in Salesforce yet

app = FastAPI(title="SF Dashboard")


@app.middleware("http")
async def guard(request, call_next):
    """Optional HTTP Basic login (set DASH_USER and DASH_PASSWORD in .env) + never cache pages/API data."""
    user, pw = os.getenv("DASH_USER", ""), os.getenv("DASH_PASSWORD", "")
    if user and pw and request.url.path != "/healthz":
        ok, h = False, request.headers.get("authorization", "")
        if h.lower().startswith("basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                ok = hmac.compare_digest(u, user) and hmac.compare_digest(p, pw)
            except Exception:
                ok = False
        if not ok:
            return Response("Login required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Support Dashboard"'})
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp

_cache = {}
_roster_cache = {}


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- salesforce via OAuth 2.0 Client Credentials + REST API ----------
def _load_env():
    f = BASE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def sf_env():
    return {"id": os.getenv("SF_CLIENT_ID", "").strip(), "secret": os.getenv("SF_CLIENT_SECRET", "").strip(),
            "url": os.getenv("SF_INSTANCE_URL", "https://greyorangeorg.my.salesforce.com").strip().rstrip("/"),
            "ver": os.getenv("SF_API_VERSION", "v60.0").strip()}


_tok = {"v": None, "inst": None, "ts": 0.0}
_tok_lock = threading.Lock()


def _ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL = _ssl_ctx()


def _http(method, url, data=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            body = json.loads(body)
        except Exception:
            pass
        return e.code, body
    except urllib.error.URLError as e:
        raise HTTPException(500, f"Cannot reach Salesforce ({url.split('/services')[0]}): {e.reason}")


def get_token(force=False, stale=None):
    """Client-credentials access token, cached ~25 min and shared by all threads."""
    cfg = sf_env()
    with _tok_lock:
        if _tok["v"] and not force and time.time() - _tok["ts"] < 1500:
            return _tok["v"], _tok["inst"]
        if force and stale and _tok["v"] and _tok["v"] != stale:  # another thread already refreshed it
            return _tok["v"], _tok["inst"]
        if not cfg["id"] or not cfg["secret"]:
            raise HTTPException(500, "Missing SF_CLIENT_ID / SF_CLIENT_SECRET - fill them in the .env file and restart.")
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cfg["id"],
                                       "client_secret": cfg["secret"]}).encode()
        st, js = _http("POST", f"{cfg['url']}/services/oauth2/token", body,
                       {"Content-Type": "application/x-www-form-urlencoded"})
        if st != 200 or not isinstance(js, dict) or "access_token" not in js:
            raise HTTPException(500, f"Salesforce token request failed ({st}): {js}")
        _tok.update(v=js["access_token"], inst=(js.get("instance_url") or cfg["url"]).rstrip("/"), ts=time.time())
        return _tok["v"], _tok["inst"]


def sf_get(path_or_url):
    tok, inst = get_token()
    url = path_or_url if path_or_url.startswith("http") else inst + path_or_url
    for attempt in (0, 1):
        st, js = _http("GET", url, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
        if st == 401 and attempt == 0:  # expired token -> refresh once and retry
            tok, _ = get_token(force=True, stale=tok)
            continue
        if st != 200:
            raise HTTPException(500, f"Salesforce API error ({st}): {js}")
        return js


def sf_query(soql):
    js = sf_get(f"/services/data/{sf_env()['ver']}/query?" + urllib.parse.urlencode({"q": soql}))
    recs = list(js["records"])
    while not js.get("done", True) and js.get("nextRecordsUrl"):  # paginate
        js = sf_get(js["nextRecordsUrl"])
        recs += js["records"]
    return recs


def dget(rec, path):
    for part in path.split("."):
        if not isinstance(rec, dict):
            return None
        rec = rec.get(part)
    return rec


# ---------- SOQL builders ----------
def sq(v):
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def c_in(f, vals):
    return f"{f} = {sq(vals[0])}" if len(vals) == 1 else f"{f} IN ({', '.join(map(sq, vals))})"


def c_not(f, vals):
    return f"{f} != {sq(vals[0])}" if len(vals) == 1 else "(" + " AND ".join(f"{f} != {sq(v)}" for v in vals) + ")"


def base_where():
    return CFG.get("soql_where") or "Id != null"


def combo_not(prefix=""):
    """Compound exclusions from config 'exclude_combo', e.g. exclude tickets that are Severity 4 AND Type = Feature Request."""
    # SOQL rejects "AND NOT (...)", so use De Morgan: NOT (a AND b) == (a' OR b')
    return ["(" + " OR ".join(c_not(prefix + x["field"], x["values"]) for x in r["all"]) + ")" for r in CFG.get("exclude_combo", [])]


def filters_where():
    parts = [base_where()]
    parts += [c_in(r["field"], r["values"]) for r in CFG.get("include", [])]
    parts += [c_not(r["field"], r["values"]) for r in CFG.get("exclude", [])]
    parts += combo_not()
    return " AND ".join(parts)


def alerts_where():  # excluded because of an exclude rule (e.g. Type = Alert)
    return f"{base_where()} AND (" + " OR ".join(c_in(r["field"], r["values"]) for r in CFG.get("exclude", [])) + ")"


def hardware_where():  # excluded because they fail an include rule (e.g. Category != Software) and are not Alerts
    neg = " OR ".join(c_not(r["field"], r["values"]) for r in CFG.get("include", []))
    ex = " AND ".join(c_not(r["field"], r["values"]) for r in CFG.get("exclude", []))
    return f"{base_where()} AND ({neg})" + (f" AND {ex}" if ex else "")


def time_cond(a, b):
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return f"{F['end_time']} >= {a.strftime(fmt)} AND {F['end_time']} < {b.strftime(fmt)}"


def id_list(ids):
    return ", ".join(sq(i) for i in ids)


def q_count(where):
    r = sf_query(f"SELECT COUNT(Id) cnt FROM {CFG['object']} WHERE {where}")
    rec = r[0] if r else {}
    return int(rec.get("cnt", rec.get("expr0", 0)) or 0)


def q_group_owner(where):
    out = Counter()
    for rec in sf_query(f"SELECT Owner.Name, COUNT(Id) cnt FROM {CFG['object']} WHERE {where} GROUP BY Owner.Name"):
        o = rec.get("Owner")
        name = o.get("Name") if isinstance(o, dict) else next((v for k, v in rec.items() if k not in ("attributes", "cnt")), None)
        out[name or "(unknown)"] += int(rec.get("cnt", 0))
    return out


# ---------- window ----------
def ist_today():
    return dt.datetime.now(TZ).date()


def rolling_window():
    R = CFG.get("rolling", {"start_offset_days": 37, "end_offset_days": 7, "extend_to_week_start": True})
    today = ist_today()
    s = today - dt.timedelta(days=R["start_offset_days"])
    e = today - dt.timedelta(days=R["end_offset_days"])
    if R.get("extend_to_week_start", True):
        s -= dt.timedelta(days=(s.weekday() + 1) % 7)  # back to the Sunday on/before
    return s, e


def utc_bounds(s, e):
    """IST dates [s, e] inclusive -> (start UTC, end UTC exclusive)."""
    a = dt.datetime.combine(s, dt.time.min, TZ).astimezone(UTC)
    b = dt.datetime.combine(e + dt.timedelta(days=1), dt.time.min, TZ).astimezone(UTC)
    return a, b


def week_start(d):
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)  # Sunday


def week_slices(s, e):
    out, d = [], s
    while d <= e:
        we = min(week_start(d) + dt.timedelta(days=6), e)
        out.append((d, we))
        d = we + dt.timedelta(days=1)
    return out


def parse_ist(ts):
    t = dt.datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
    return t.astimezone(TZ).date()


# ---------- roster ----------
def pretty(email):
    parts = [p for p in email.split("@")[0].split(".") if p and p != "int"]
    return " ".join(p.capitalize() for p in parts)


def read_roster_csv():
    with open(BASE / CFG["roster_file"], newline="") as fh:
        return [{"email": r["email"].strip().lower(), "pod": r["pod"].strip(), "name": (r.get("name") or "").strip()}
                for r in csv.DictReader(fh) if r.get("email")]


def resolve_roster():
    """-> (entries, idmap {user_id: entry}, report). Step 0: shown before the long queries."""
    if "r" in _roster_cache and time.time() - _roster_cache["ts"] < 3600:
        return _roster_cache["r"]
    rows = read_roster_csv()
    by_email = defaultdict(list)
    if MOCK:
        for i, r in enumerate(rows):
            if r["email"] not in MOCK_MISSING:
                by_email[r["email"]].append({"id": f"005MOCK{i:04d}", "name": r["name"] or pretty(r["email"])})
    else:
        emails = [r["email"] for r in rows]
        for i in range(0, len(emails), 100):
            chunk = ", ".join(sq(e) for e in emails[i:i + 100])
            for u in sf_query(f"SELECT Id, Name, Email, IsActive FROM User WHERE IsActive = true AND Email IN ({chunk})"):
                by_email[(u["Email"] or "").lower()].append({"id": u["Id"], "name": u["Name"]})
    entries, idmap, unmatched, multiple = [], {}, [], []
    for r in rows:
        us = by_email.get(r["email"], [])
        status = "ok" if len(us) == 1 else "not_in_salesforce" if not us else "multiple_matches"
        entry = {"email": r["email"], "pod": r["pod"], "status": status, "ids": [u["id"] for u in us],
                 "name": us[0]["name"] if us else (r["name"] or pretty(r["email"]))}
        entries.append(entry)
        for u in us:
            idmap[u["id"]] = entry
        if not us:
            unmatched.append(r["email"])
        elif len(us) > 1:
            multiple.append({"email": r["email"], "ids": [u["id"] for u in us]})
    report = {
        "total": len(entries), "matched": sum(1 for e in entries if e["ids"]),
        "unmatched": unmatched, "multiple": multiple,
        "action": "Unmatched engineers are kept with zeros and labelled 'not in Salesforce yet' (never swapped for a lookalike). "
                  "Emails with several active users keep all those IDs under the one engineer.",
        "roster": [{"name": e["name"], "email": e["email"], "pod": e["pod"], "status": e["status"]} for e in entries],
    }
    res = (entries, idmap, report)
    _roster_cache.update(r=res, ts=time.time())
    return res


# ---------- data collection ----------
def live_collect(s, e, ids, slices):
    a, b = utc_bounds(s, e)
    flt, t_all = filters_where(), time_cond(a, b)
    idl = id_list(ids)

    def slice_job(sl):
        a1, b1 = utc_bounds(*sl)
        t = time_cond(a1, b1)
        rows = sf_query(f"SELECT Id, CaseNumber, {F['type']}, {F['owner_id']}, {F['end_time']} FROM {CFG['object']} "
                        f"WHERE {flt} AND {t} AND {F['owner_id']} IN ({idl}) ORDER BY {F['end_time']}") if ids else []
        owners = q_group_owner(f"{flt} AND {t} AND {F['owner_id']} NOT IN ({idl})") if ids else q_group_owner(f"{flt} AND {t}")
        return {"rows": rows, "owners": owners,
                "alerts": q_count(f"{alerts_where()} AND {t}"), "hardware": q_count(f"{hardware_where()} AND {t}")}

    with ThreadPoolExecutor(max_workers=6) as ex:
        f_pre = ex.submit(q_count, f"{flt} AND {t_all}")
        futs = [ex.submit(slice_job, sl) for sl in slices]
        res = [f.result() for f in futs]
        all_pre = f_pre.result()
    all_post = q_count(f"{flt} AND {t_all}")  # re-run to detect data moving during the run
    return res, all_pre, all_post


def mock_rows(s, e, entries):
    rnd = random.Random(7)
    live = [en for en in entries if en["ids"]]
    others = [("005OTH0001", "Nancy Deo"), ("005OTH0002", "Raj Mehta"), ("005OTH0003", "Ops Queue"), ("005OTH0004", "Priya N")]
    rows, d = [], s
    while d <= e:
        if d.weekday() < 5 or rnd.random() < 0.1:
            for _ in range(rnd.randint(60, 120) if d.weekday() < 5 else rnd.randint(1, 8)):
                if rnd.random() < 0.55:
                    en = rnd.choice(live) if rnd.random() < 0.85 else rnd.choice(live[:6])
                    oid, oname = en["ids"][0], en["name"]
                else:
                    oid, oname = rnd.choice(others)
                t = dt.datetime.combine(d, dt.time(rnd.randint(0, 23), rnd.randint(0, 59)), TZ).astimezone(UTC)
                rows.append({
                    "Id": f"500{len(rows):012d}", "CaseNumber": f"{len(rows):08d}", F["status"]: "Solved",
                    F["category"]: rnd.choices(["Software", "Hardware"], [8, 2])[0],
                    F["type"]: rnd.choices(["Incident", "Service Request", "Query", "Alert"], [6, 1.5, 1, 1.5])[0],
                    F["owner_id"]: oid, "Owner": {"Name": oname}, "Account": {"Name": f"Customer {(len(rows) * 7) % 40 + 1}"},
                    F["end_time"]: t.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                    "RecordType": {"Name": rnd.choices(["Ticket Form", "IM Ticket"], [9, 1])[0]},
                })
        d += dt.timedelta(days=1)
    return rows


def passes(rec):
    for r in CFG.get("include", []):
        if dget(rec, r["field"]) not in r["values"]:
            return False
    for r in CFG.get("exclude", []):
        if dget(rec, r["field"]) in r["values"]:
            return False
    for r in CFG.get("exclude_combo", []):
        if all(dget(rec, x["field"]) in x["values"] for x in r["all"]):
            return False
    return True


def mock_collect(s, e, idmap, entries, slices):
    allrows = mock_rows(s, e, entries)
    res = []
    for (a, b) in slices:
        part = [r for r in allrows if a <= parse_ist(dget(r, F["end_time"])) <= b]
        kept = [r for r in part if passes(r)]
        rr = [r for r in kept if dget(r, F["owner_id"]) in idmap]
        owners = Counter(dget(r, "Owner.Name") for r in kept if dget(r, F["owner_id"]) not in idmap)
        alerts = sum(1 for r in part if any(dget(r, x["field"]) in x["values"] for x in CFG.get("exclude", [])))
        hw = sum(1 for r in part if not any(dget(r, x["field"]) in x["values"] for x in CFG.get("exclude", []))
                 and any(dget(r, x["field"]) not in x["values"] for x in CFG.get("include", [])))
        res.append({"rows": rr, "owners": owners, "alerts": alerts, "hardware": hw})
    total = sum(1 for r in allrows if passes(r))
    return res, total, total


# ---------- summarize ----------
def summarize(start=None, end=None):
    if start and end:
        s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    else:
        s, e = rolling_window()
    entries, idmap, report = resolve_roster()
    ids = list(idmap)
    slices = week_slices(s, e)
    res, all_pre, all_post = (mock_collect(s, e, idmap, entries, slices) if MOCK else live_collect(s, e, ids, slices))
    a, b = utc_bounds(s, e)

    eng = {en["email"]: {"id": en["email"], "name": en["name"], "email": en["email"], "pod": en["pod"],
                         "status": en["status"], "days": {}} for en in entries}
    seen, dupes, outside, slice_counts = set(), 0, 0, []
    for r_ in res:
        slice_counts.append(len(r_["rows"]))
        for r in r_["rows"]:
            cid = dget(r, "Id")
            if cid in seen:
                dupes += 1
                continue
            seen.add(cid)
            d = parse_ist(dget(r, F["end_time"]))
            if not (s <= d <= e):
                outside += 1
                continue
            cell = eng[idmap[dget(r, F["owner_id"])]["email"]]["days"].setdefault(str(d), [0, 0])
            cell[0 if dget(r, F["type"]) in CFG["incident_types"] else 1] += 1

    extras, nr_total = {}, 0
    for (sa, sb), r_ in zip(slices, res):
        nr = sum(r_["owners"].values())
        nr_total += nr
        extras[str(week_start(sa))] = {"nr": nr, "owners": r_["owners"].most_common(), "alerts": r_["alerts"], "hardware": r_["hardware"]}

    engs = sorted(eng.values(), key=lambda x: (x["pod"], x["name"]))
    inc = sum(c[0] for x in engs for c in x["days"].values())
    sr = sum(c[1] for x in engs for c in x["days"].values())
    roster_total = inc + sr
    pod_sum = sum(sum(c[0] + c[1] for x in engs if x["pod"] == p for c in x["days"].values()) for p in {x["pod"] for x in engs})
    checks = [
        {"name": "Slice totals add up to roster total (no duplicates across slices)",
         "ok": sum(slice_counts) == roster_total + outside and dupes == 0,
         "detail": f"slices {slice_counts} = {sum(slice_counts)}; roster total {roster_total}; duplicates {dupes}"},
        {"name": "Every ticket falls inside the window", "ok": outside == 0, "detail": f"{outside} outside {s} - {e}"},
        {"name": "Roster total + non-roster = all-owner total", "ok": roster_total + nr_total == all_pre,
         "detail": f"{roster_total} + {nr_total} = {roster_total + nr_total} vs all-owner count {all_pre}"},
        {"name": "All-owner count re-run matches (data did not move during the run)", "ok": all_pre == all_post,
         "detail": f"first {all_pre}, re-run {all_post}"},
        {"name": "POD subtotals add up to the grand total", "ok": pod_sum == roster_total, "detail": f"{pod_sum} vs {roster_total}"},
        {"name": "Incident + SR/Query = total", "ok": inc + sr == roster_total, "detail": f"{inc} + {sr} = {inc + sr}"},
    ]
    return {
        "window": {"start": str(s), "end": str(e), "tz": "IST", "start_utc": a.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "end_utc": b.strftime("%Y-%m-%dT%H:%M:%SZ"), "slices": [[str(x), str(y)] for x, y in slices]},
        "pods": sorted({x["pod"] for x in engs}),
        "engineers": engs, "extras": extras,
        "all_owner_total": all_pre, "roster_total": roster_total, "non_roster_total": nr_total,
        "checks": checks, "roster_report": report,
        "filter_label": CFG.get("filter_label", ""),
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"), "mock": MOCK,
    }


# ---------- api ----------
# ---------- Tab 2: Owner vs Assisted ----------
import re

INTERNAL_DOMAIN = "greyorange.com"
_PHRASES = ["thank you", "thanks", "closing the ticket", "closed", "resolved", "please close", "confirm", "no further"]
_FILLER = set("hi hello hey dear team regards best warm sincerely cheers please kindly the this that ticket case is are "
              "was were be been as we you your for with now has have will and to of it on a an all any so then also "
              "issue support sir madam".split())
_CUTS = [re.compile(p, re.I) for p in (
    r"\n[ \t]*On [^\n]{0,300}(?:\n[^\n]{0,200})?wrote:",
    r"-{2,}[ \t]*Original Message[ \t]*-{2,}",
    r"\n[ \t]*From:[^\n]*\n[ \t]*(?:Sent|Date):",
    r"\n[ \t]*_{8,}[ \t]*\n",
    r"-{2,}[ \t]*Forwarded message",
    r"\n[ \t]*Sent from my ",
)]
_ASK = re.compile(r"\?|\b(?:please|kindly)\s+(?:provide|share|send|attach|let us know|confirm)\b|\b(?:could|can|would) you\b"
                  r"|\bneed (?:the |more )?(?:logs?|info|details)\b", re.I)


def strip_quoted(text):
    """Remove quoted reply text (On ... wrote:, Original Message, From/Sent headers, '>' lines, signatures-from-phone)."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    probe, cut = "\n" + t, len(t)
    for pat in _CUTS:
        m = pat.search(probe)
        if m:
            cut = min(cut, max(0, m.start() - 1))
    t = t[:cut]
    return "\n".join(l for l in t.split("\n") if not l.lstrip().startswith(">")).strip()


def word_count(t):
    return len(re.findall(r"\S+", t or ""))


def only_phrases(t):
    """True when the text is just thanks / closing / confirmation phrases (plus greetings and sign-off)."""
    s = (t or "").lower()
    for ph in _PHRASES:
        s = s.replace(ph, " ")
    return len([w for w in re.findall(r"[a-z']+", s) if w not in _FILLER]) <= 6


def qualifies(text):
    """Qualifying email test (approximation): >= 40 words, and if < 80 words not only thanks/closing phrases."""
    w = word_count(text)
    if w < 40:
        return False, w
    if w < 80 and only_phrases(text):
        return False, w
    return True, w


def has_external(to_addr):
    addrs = re.findall(r"[\w.+'-]+@[\w.-]+\.\w+", (to_addr or "").lower())
    return any(not a.endswith("@" + INTERNAL_DOMAIN) for a in addrs)


def borderline_flags(text, w):
    f = []
    if 40 <= w <= 50:
        f.append("about 40 words")
    if _ASK.search(text or ""):
        f.append("requests information")
    chars = re.sub(r"\s", "", text or "")
    if chars:
        letters = sum(c.isalpha() for c in chars)
        if letters / len(chars) < 0.6 or len(re.findall(r"[{}\[\]<>]", text)) > 20:
            f.append("mostly pasted payload")
    return f


def _chunks(lst, n):
    return [lst[i:i + n] for i in range(0, len(lst), n)]


def _flat(parts):
    return [x for p in parts for x in p]


def assisted_collect_live(s, e, entries, idmap, slices):
    ids = list(idmap)
    if not ids:
        return [], [], []
    idl, flt = id_list(ids), filters_where()
    fmt = "%Y-%m-%dT%H:%M:%SZ"

    def owner_slice(sl):  # STEP 1: owner side (same as Tab 1, plus AccountId / Account.Name)
        a, b = utc_bounds(*sl)
        return sf_query(f"SELECT Id, CaseNumber, {F['type']}, {F['owner_id']}, AccountId, Account.Name, {F['end_time']} "
                        f"FROM {CFG['object']} WHERE {flt} AND {time_cond(a, b)} AND {F['owner_id']} IN ({idl}) "
                        f"ORDER BY {F['end_time']}")

    def team_slice(sl):  # STEP 3: case team members (member must be a roster engineer)
        a, b = utc_bounds(*sl)
        pf = " AND ".join(["Parent." + base_where()]
                          + [c_in("Parent." + r["field"], r["values"]) for r in CFG.get("include", [])]
                          + [c_not("Parent." + r["field"], r["values"]) for r in CFG.get("exclude", [])] + combo_not("Parent."))
        return sf_query(f"SELECT ParentId, MemberId, TeamRoleId FROM CaseTeamMember WHERE {pf} "
                        f"AND Parent.{F['end_time']} >= {a.strftime(fmt)} AND Parent.{F['end_time']} < {b.strftime(fmt)} "
                        f"AND MemberId IN ({idl})")

    with ThreadPoolExecutor(max_workers=6) as ex:
        owner_rows = _flat(ex.map(owner_slice, slices))
        team_rows = _flat(ex.map(team_slice, slices))
        uniq = {r["Id"]: r for r in owner_rows}
        ordered = sorted(uniq, key=lambda i: dget(uniq[i], F["end_time"]) or "")
        emails = ", ".join(sq(x["email"]) for x in entries)

        def email_batch(b):  # STEP 2: batches of cases (~1-3 days of tickets) to stay under row limits
            return sf_query("SELECT ParentId, FromAddress, ToAddress, TextBody, MessageDate FROM EmailMessage "
                            f"WHERE ParentId IN ({id_list(b)}) AND FromAddress IN ({emails})")
        email_rows = _flat(ex.map(email_batch, _chunks(ordered, 200)))
    return owner_rows, email_rows, team_rows


def assisted_collect_mock(s, e, entries, idmap, slices):
    rows = mock_rows(s, e, entries)
    cases = [r for r in rows if passes(r) and dget(r, F["owner_id"]) in idmap]
    rnd = random.Random(11)
    live = [en for en in entries if en["ids"]]
    emails, team = [], []
    for r in cases:
        owner = idmap[dget(r, F["owner_id"])]
        if rnd.random() < 0.30:
            mates = [en for en in live if en["pod"] == owner["pod"] and en is not owner]
            if mates:
                en = rnd.choice(mates)
                w = rnd.choice([12, 25, 38, 41, 45, 60, 75, 95, 140, 220])
                body = " ".join(["detail"] * w)
                if rnd.random() < 0.2:
                    body = "Thank you, closing the ticket. " + " ".join(["ok"] * max(0, w - 6))
                if rnd.random() < 0.15:
                    body += " can you share the logs?"
                body += "\n\nOn Mon, Oct 5, 2026 at 10:00 AM Customer <c@client.com> wrote:\n> earlier text " + "x " * 120
                emails.append({"ParentId": dget(r, "Id"), "FromAddress": en["email"].upper() if rnd.random() < .2 else en["email"],
                               "ToAddress": rnd.choice(["c@client.com; support@greyorange.com", "support@greyorange.com", "a@greyorange.com"]),
                               "TextBody": body, "MessageDate": dget(r, F["end_time"])})
        if rnd.random() < 0.12:
            others = [en for en in live if en is not owner]
            team.append({"ParentId": dget(r, "Id"), "MemberId": rnd.choice(others)["ids"][0], "TeamRoleId": "0B7MOCK"})
    return cases, emails, team


def assisted_summarize(start=None, end=None, refresh=False):
    if start and end:
        s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    else:
        s, e = rolling_window()
    entries, idmap, report = resolve_roster()
    slices = week_slices(s, e)
    owner_rows, email_rows, team_rows = (assisted_collect_mock if MOCK else assisted_collect_live)(s, e, entries, idmap, slices)
    by_email = {en["email"]: en for en in entries}
    eng = {en["email"]: {"id": en["email"], "name": en["name"], "email": en["email"], "pod": en["pod"], "status": en["status"],
                         "owner_days": {}, "assisted_days": {}} for en in entries}

    # STEP 1: owner side (current owner, each case once, IST day)
    cases, dupes, outside = {}, 0, 0
    for r in owner_rows:
        cid = dget(r, "Id")
        if cid in cases:
            dupes += 1
            continue
        d = parse_ist(dget(r, F["end_time"]))
        owner = idmap.get(dget(r, F["owner_id"]))
        if not (s <= d <= e):
            outside += 1
            continue
        if not owner:
            continue
        inc = dget(r, F["type"]) in CFG["incident_types"]
        cases[cid] = {"num": dget(r, "CaseNumber"), "account": dget(r, "Account.Name") or dget(r, "AccountId") or "",
                      "owner": owner, "d": d, "inc": inc}
        eng[owner["email"]]["owner_days"].setdefault(str(d), [0, 0])[0 if inc else 1] += 1

    _POP['tab2'] = set(cases)
    # STEP 2 (rule 1, email) and STEP 3 (rule 2, case team); one assist per engineer per ticket
    pairs = {}
    for m in email_rows:
        c = cases.get(m.get("ParentId"))
        en = by_email.get((m.get("FromAddress") or "").strip().lower())
        if not c or not en or en["email"] == c["owner"]["email"] or en["pod"] != c["owner"]["pod"]:
            continue
        if not has_external(m.get("ToAddress")):
            continue
        text = strip_quoted(m.get("TextBody"))
        ok, w = qualifies(text)
        if not ok:
            continue
        p = pairs.setdefault((m["ParentId"], en["email"]), {"email": None, "team": False})
        if p["email"] is None or w > p["email"]["words"]:
            p["email"] = {"date": m.get("MessageDate"), "words": w, "flags": borderline_flags(text, w)}
    for t in team_rows:
        c, en = cases.get(t.get("ParentId")), idmap.get(t.get("MemberId"))
        if not c or not en or en["email"] == c["owner"]["email"]:
            continue
        pairs.setdefault((t["ParentId"], en["email"]), {"email": None, "team": False})["team"] = True

    tickets = []
    for (cid, em), p in pairs.items():
        c, en = cases[cid], by_email[em]
        eng[em]["assisted_days"][str(c["d"])] = eng[em]["assisted_days"].get(str(c["d"]), 0) + 1
        reasons = ([f"Email {str(p['email']['date'] or '')[:10]} · {p['email']['words']} words"] if p["email"] else []) \
            + (["Case team member"] if p["team"] else [])
        tickets.append({"case_id": cid, "case": c["num"], "account": c["account"], "date": str(c["d"]),
                        "owner": c["owner"]["name"], "owner_pod": c["owner"]["pod"],
                        "engineer": en["name"], "engineer_email": em, "engineer_pod": en["pod"],
                        "reasons": reasons, "flags": (p["email"] or {}).get("flags", [])})
    tickets.sort(key=lambda x: (x["date"], x["case"] or "", x["engineer"]))
    engs = sorted(eng.values(), key=lambda x: (x["pod"], x["name"]))

    # ---- validation checks ----
    t1 = get_summary(start, end, refresh)
    t1e = {x["id"]: x for x in t1["engineers"]}
    tot = lambda days: sum(c[0] + c[1] for c in days.values())
    own_tot = sum(tot(x["owner_days"]) for x in engs)
    t1_tot = sum(tot(x["days"]) for x in t1["engineers"])
    pods = sorted({x["pod"] for x in engs})
    bad_pod = [p for p in pods if sum(tot(x["owner_days"]) for x in engs if x["pod"] == p)
               != sum(tot(x["days"]) for x in t1["engineers"] if x["pod"] == p)]
    bad_eng = [x["name"] for x in engs if tot(x["owner_days"]) != tot(t1e[x["id"]]["days"])]
    wk2, wk1 = defaultdict(int), defaultdict(int)
    for x in engs:
        for d, c in x["owner_days"].items():
            wk2[str(week_start(dt.date.fromisoformat(d)))] += c[0] + c[1]
    for x in t1["engineers"]:
        for d, c in x["days"].items():
            wk1[str(week_start(dt.date.fromisoformat(d)))] += c[0] + c[1]
    bad_wk = [w for w in set(wk1) | set(wk2) if wk1[w] != wk2[w]]
    i2, s2 = sum(c[0] for x in engs for c in x["owner_days"].values()), sum(c[1] for x in engs for c in x["owner_days"].values())
    i1, s1 = sum(c[0] for x in t1["engineers"] for c in x["days"].values()), sum(c[1] for x in t1["engineers"] for c in x["days"].values())
    own_cases, asst_cases = defaultdict(int), defaultdict(set)
    for cid, c in cases.items():
        own_cases[c["owner"]["email"]] += 1
    for (cid, em) in pairs:
        asst_cases[em].add(cid)
    bad_sum = [x["name"] for x in engs if tot(x["owner_days"]) + sum(x["assisted_days"].values()) != own_cases[x["id"]] + len(asst_cases[x["id"]])]
    selfs = [k for k in pairs if cases[k[0]]["owner"]["email"] == k[1]]
    n_asst = sum(sum(x["assisted_days"].values()) for x in engs)
    t1_checks = {c["name"]: c for c in t1["checks"]}
    pick = lambda prefix: next((c for n_, c in t1_checks.items() if n_.startswith(prefix)), {"ok": False, "detail": "n/a", "name": prefix})
    checks = [
        {"name": "Tab 1 roster-owned total = Tab 2 Owner total (overall)", "ok": own_tot == t1_tot, "detail": f"Tab 2 {own_tot} vs Tab 1 {t1_tot}"},
        {"name": "... per POD", "ok": not bad_pod, "detail": "all PODs match" if not bad_pod else f"mismatch: {bad_pod}"},
        {"name": "... per engineer", "ok": not bad_eng, "detail": "all engineers match" if not bad_eng else f"mismatch: {bad_eng[:8]}"},
        {"name": "... per week", "ok": not bad_wk, "detail": "all weeks match" if not bad_wk else f"mismatch weeks: {sorted(bad_wk)}"},
        {"name": "... Incidents and SR/Query", "ok": i1 == i2 and s1 == s2, "detail": f"Incidents {i2} vs {i1}; SR/Query {s2} vs {s1}"},
        {"name": "Owner + Assisted per engineer = owner tickets + distinct assisted tickets", "ok": not bad_sum,
         "detail": "all engineers match" if not bad_sum else f"mismatch: {bad_sum[:8]}"},
        {"name": "No engineer assisted on a ticket they own", "ok": not selfs, "detail": f"{len(selfs)} found"},
        {"name": "No engineer counted twice on one ticket", "ok": n_asst == len(pairs) == len(tickets), "detail": f"{n_asst} assisted rows = {len(pairs)} unique (ticket, engineer) pairs"},
        {"name": "Every ticket falls inside the window; no duplicate cases", "ok": outside == 0 and dupes == 0, "detail": f"{outside} outside, {dupes} duplicates"},
        pick("Slice totals"), pick("Roster total + non-roster"),
    ]
    a, b = utc_bounds(s, e)
    return {
        "window": {"start": str(s), "end": str(e), "tz": "IST", "start_utc": a.strftime("%Y-%m-%dT%H:%M:%SZ"), "end_utc": b.strftime("%Y-%m-%dT%H:%M:%SZ")},
        "pods": pods, "engineers": engs, "tickets": tickets,
        "owner_total": own_tot, "assisted_total": n_asst, "distinct_assisted_tickets": len({k[0] for k in pairs}),
        "checks": checks, "roster_report": report,
        "notes": ["Rule 1 (email) filter is an approximation: quoted reply text is stripped, emails under 40 words are dropped, and emails under 80 words "
                  "that are only thanks / closing / confirmation phrases are dropped.",
                  "Assisted tickets stay in the owner's Owner count; Assisted is not added into the Tab 1 total.",
                  "Only tickets owned by a roster engineer can have Assisted; the owner is never Assisted on their own ticket; each engineer counts once per ticket."],
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"), "mock": MOCK,
    }


def get_summary(start=None, end=None, refresh=False):
    key = (start, end, str(ist_today()))
    hit = _cache.get(key)
    if hit and not refresh and time.time() - hit[0] < CFG.get("cache_seconds", 300):
        return hit[1]
    if refresh:
        _roster_cache.clear()
    data = summarize(start, end)
    _cache[key] = (time.time(), data)
    return data


@app.get("/api/summary")
def api_summary(start: str = None, end: str = None, refresh: bool = False):
    return get_summary(start, end, refresh)


@app.get("/api/assisted")
def api_assisted(start: str = None, end: str = None, refresh: bool = False):
    key = ("A", start, end, str(ist_today()))
    hit = _cache.get(key)
    if hit and not refresh and time.time() - hit[0] < CFG.get("cache_seconds", 300):
        return hit[1]
    data = assisted_summarize(start, end, refresh)
    _cache[key] = (time.time(), data)
    return data


# ---------- Tab 3: Reopened tickets ----------
import io

_POP = {}  # population case ids per tab, so Tab 3 can confirm it uses the same cases as Tab 2
_BOT_RE = re.compile(r"jira|linear|webhook|guest|integration|automat|\bbot\b|noreply|no-reply|portal", re.I)
_STILL = re.compile(r"\b(?:still|not resolved|unresolved|unable|not working|isn'?t working|same issue|same problem|facing (?:the )?same|not yet|not fixed|however)\b|\bbut\b", re.I)
_NO_CLOSE = re.compile(r"(?:do\s*not|don'?t|dont|not to)\s+(?:yet\s+)?close|keep (?:it |the ticket |this )?open|hold (?:off|on)", re.I)
_CLOSURE = re.compile(r"thank(?:s| you)|please close|can be closed|you can close|may be closed|close (?:the|this) (?:ticket|case)|"
                      r"(?:issue|problem) (?:is |has been )?(?:resolved|fixed)|\bresolved\b|\bfixed\b|working (?:fine|now|as expected)|all good|no further", re.I)
_ASKS = re.compile(r"\b(?:please|kindly|can you|could you|would you|need you to|we need|i need|let us know|let me know)\b.{0,60}?\b"
                   r"(?:add|cancel|share|provide|send|check|update|arrange|schedule|call|create|delete|remove|assign|raise|release|push|change|enable|disable|reset|restart|confirm|revert|eta|status)\b"
                   r"|\b(?:add|adding|cancel|release|push)\b.{0,40}\b(?:orders?|tasks?)\b"
                   r"|\b(?:eta|call me|schedule a call|connect (?:me|us)|set up a call|give (?:me |us )?a call|join (?:a )?call)\b"
                   r"|\b(?:from now on|going forward|in future|make sure|ensure that|you (?:should|need to|have to|must))\b", re.I)


def parse_dt(ts):
    return dt.datetime.strptime(str(ts).replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")


def norm_case(n):
    return str(n or "").strip().lstrip("0")


def overrides():
    return {norm_case(x) for x in CFG.get("reopen_overrides", ["300373", "300586", "290174", "296871", "289751"])}


def is_human_customer(c):
    em = (dget(c, "CreatedBy.Email") or "").strip().lower()
    nm = dget(c, "CreatedBy.Name") or ""
    return bool(em) and not em.endswith("@" + INTERNAL_DOMAIN) and not _BOT_RE.search(em + " " + nm)


def classify_closure(text):
    """Rule-based closure test on the last customer comment. -> (is_closure, reason)"""
    t = (text or "").strip()
    if not t:
        return False, "customer comment is empty"
    if word_count(t) > 40:
        return False, "comment is long"
    if "?" in t:
        return False, "contains a question"
    if _STILL.search(t):
        return False, "reports a remaining problem"
    if _NO_CLOSE.search(t):
        return False, "asks not to close"
    if _ASKS.search(t):
        return False, "asks for something / gives instructions"
    if not _CLOSURE.search(t):
        return False, "not a closure confirmation"
    return True, "closure confirmation"


def analyse_history(rows):
    """One ticket's Status history -> reopen info, or None. Several Pending Confirmation -> Assigned changes = ONE reopened ticket;
    the LAST one before the last change to Solved dates it."""
    ev = sorted(((parse_dt(r["CreatedDate"]), r.get("OldValue"), r.get("NewValue")) for r in rows), key=lambda x: x[0])
    solved = [t for t, o, n in ev if n == "Solved"]
    if not solved:
        return None
    reopens = [t for t, o, n in ev if o == "Pending Confirmation" and n == "Assigned" and t < solved[-1]]
    if not reopens:
        return None
    pcs = [t for t, o, n in ev if n == "Pending Confirmation" and t <= reopens[-1]]
    return {"reopen": reopens[-1], "pc": pcs[-1] if pcs else None, "n": len(reopens)}


def day_slices(s, e, n=2):
    out, d = [], s
    while d <= e:
        out.append((d, min(d + dt.timedelta(days=n - 1), e)))
        d += dt.timedelta(days=n)
    return out


def q_count_obj(obj, where):
    r = sf_query(f"SELECT COUNT(Id) cnt FROM {obj} WHERE {where}")
    rec = r[0] if r else {}
    return int(rec.get("cnt", rec.get("expr0", 0)) or 0)


def reopen_phase1_live(s, e, idmap, slices):
    ids = list(idmap)
    a, b = utc_bounds(s, e)
    flt, idl = filters_where(), id_list(ids)
    cf = " AND ".join(["Case." + base_where()] + [c_in("Case." + r["field"], r["values"]) for r in CFG.get("include", [])]
                      + [c_not("Case." + r["field"], r["values"]) for r in CFG.get("exclude", [])] + combo_not("Case.")) + f" AND Case.{F['owner_id']} IN ({idl})"
    fmt = "%Y-%m-%dT%H:%M:%SZ"

    def pop_slice(sl):  # STEP 1: population (same case IDs as Tabs 1 and 2)
        x, y = utc_bounds(*sl)
        return sf_query(f"SELECT Id, CaseNumber, {F['type']}, {F['owner_id']}, AccountId, Account.Name, {F['end_time']} FROM {CFG['object']} "
                        f"WHERE {flt} AND {time_cond(x, y)} AND {F['owner_id']} IN ({idl}) ORDER BY {F['end_time']}")

    def hist_slice(sl):  # STEP 2: Status history, 2-day slices; OldValue/NewValue cannot be filtered in SOQL, so filter in code
        x, y = utc_bounds(*sl)
        w = f"Field = 'Status' AND {cf} AND Case.{F['end_time']} >= {x.strftime(fmt)} AND Case.{F['end_time']} < {y.strftime(fmt)}"
        rows = sf_query(f"SELECT CaseId, Field, OldValue, NewValue, CreatedDate FROM CaseHistory WHERE {w} ORDER BY CaseId, CreatedDate")
        return rows, q_count_obj("CaseHistory", w)

    with ThreadPoolExecutor(max_workers=6) as ex:
        f_pre = ex.submit(q_count, f"{flt} AND {time_cond(a, b)}")
        pop = _flat(ex.map(pop_slice, slices)) if ids else []
        hres = list(ex.map(hist_slice, day_slices(s, e))) if ids else []
        all_pre = f_pre.result()
    hist = _flat(r for r, _ in hres)
    checks = [(len(r), c) for r, c in hres]
    ctx = {"flt": flt, "a": a, "b": b, "started": dt.datetime.now(UTC)}
    return pop, hist, checks, all_pre, ctx


def reopen_comments_live(ctx, case_ids):  # STEP 4: CaseComment in 4 batches
    if not case_ids:
        return []
    size = max(1, -(-len(case_ids) // 4))
    def batch(b):
        return sf_query("SELECT ParentId, CommentBody, CreatedDate, CreatedBy.Email, CreatedBy.Name FROM CaseComment "
                        f"WHERE ParentId IN ({id_list(b)})")
    with ThreadPoolExecutor(max_workers=4) as ex:
        return _flat(ex.map(batch, _chunks(case_ids, size)))


def reopen_allpost_live(ctx):
    n = q_count(f"{ctx['flt']} AND {time_cond(ctx['a'], ctx['b'])}")
    changed = []
    if True:  # which tickets moved during the run (only used when the count differs)
        changed = sf_query(f"SELECT CaseNumber, Owner.Name, LastModifiedDate FROM {CFG['object']} WHERE {ctx['flt']} AND {time_cond(ctx['a'], ctx['b'])} "
                           f"AND LastModifiedDate >= {ctx['started'].strftime('%Y-%m-%dT%H:%M:%SZ')} ORDER BY LastModifiedDate DESC LIMIT 25")
    return n, [{"case": r.get("CaseNumber"), "owner": dget(r, "Owner.Name"), "modified": r.get("LastModifiedDate")} for r in changed]


def reopen_mock(s, e, entries, idmap):
    rows = mock_rows(s, e, entries)
    pop = [r for r in rows if passes(r) and dget(r, F["owner_id"]) in idmap]
    rnd = random.Random(5)
    hist, comments, kinds = [], [], {}
    texts = {"closure": ["Thank you, issue resolved. Please close the ticket.", "Issue fixed now, ticket can be closed. Thanks", "Working fine now, thanks for the support."],
             "question": ["Thanks, can you confirm when the fix will be deployed?", "Resolved? We still need the root cause analysis document."],
             "still": ["Thanks but we are still facing the same issue on station 4.", "Not resolved yet, the error is back after restart."],
             "ask": ["Thank you. Please add 20 more orders to the queue and cancel task 5512.", "Thanks, can you share the ETA for the next release? Also set up a call."],
             "none": []}
    for r in pop:
        end, cid = parse_dt(dget(r, F["end_time"])), r["Id"]
        ev = []
        reopened = rnd.random() < 0.36
        if reopened:
            far = rnd.random() < 0.07
            rt = end - dt.timedelta(days=25 if far else 1)
            t0 = min(end - dt.timedelta(days=2), rt - dt.timedelta(hours=8))
            ev = [(t0, "New", "Assigned"), (t0 + dt.timedelta(hours=1), "Assigned", "Pending Confirmation")]
            if rnd.random() < 0.2:
                ev += [(rt - dt.timedelta(hours=6), "Pending Confirmation", "Assigned"), (rt - dt.timedelta(hours=5), "Assigned", "Pending Confirmation")]
            ev += [(rt, "Pending Confirmation", "Assigned"), (rt + dt.timedelta(hours=2), "Assigned", "Pending Confirmation"), (end, "Pending Confirmation", "Solved")]
            kind = rnd.choices(["closure", "question", "still", "ask", "none"], [45, 12, 15, 12, 16])[0]
            kinds[cid] = kind
            if texts[kind]:
                comments.append({"ParentId": cid, "CommentBody": rnd.choice(texts[kind]), "CreatedDate": (rt - dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                                 "CreatedBy": {"Email": "ops@client.com", "Name": "Client Ops"}})
            comments.append({"ParentId": cid, "CommentBody": "Thank you, issue resolved. Please close the ticket.", "CreatedDate": (rt - dt.timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                             "CreatedBy": {"Email": "eng@greyorange.com", "Name": "Support Engineer"}})
            comments.append({"ParentId": cid, "CommentBody": "Linked issue updated", "CreatedDate": (rt - dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                             "CreatedBy": {"Email": "jira@webhook.atlassian.com", "Name": "Jira Webhook User"}})
        else:
            t0 = end - dt.timedelta(days=2)
            ev = [(t0, "New", "Assigned"), (t0 + dt.timedelta(hours=1), "Assigned", "Pending Confirmation"), (end, "Pending Confirmation", "Solved")]
        for t, o, n in ev:
            hist.append({"CaseId": cid, "Field": "Status", "OldValue": o, "NewValue": n, "CreatedDate": t.strftime("%Y-%m-%dT%H:%M:%S.000+0000")})
    # relabel five closure-type reopened cases with the manual-override case numbers, to demo the override
    nums = ["00300373", "00300586", "00290174", "00296871", "00289751"]
    byid = {r["Id"]: r for r in pop}
    for cid in [c for c, k in kinds.items() if k == "closure"][:5]:
        byid[cid]["CaseNumber"] = nums.pop(0) if nums else byid[cid]["CaseNumber"]
    slice_rows = []
    for sl in day_slices(s, e):
        ids = {r["Id"] for r in pop if sl[0] <= parse_ist(dget(r, F["end_time"])) <= sl[1]}
        n = sum(1 for h in hist if h["CaseId"] in ids)
        slice_rows.append((n, n))
    total = sum(1 for r in rows if passes(r))
    return pop, hist, slice_rows, total, {"comments": comments, "total": total}


def reopened_summarize(start=None, end=None, refresh=False):
    if start and end:
        s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    else:
        s, e = rolling_window()
    entries, idmap, report = resolve_roster()
    slices = week_slices(s, e)
    if MOCK:
        pop, hist, hchecks, all_pre, ctx = reopen_mock(s, e, entries, idmap)
    else:
        pop, hist, hchecks, all_pre, ctx = reopen_phase1_live(s, e, idmap, slices)
    eng = {en["email"]: {"id": en["email"], "name": en["name"], "email": en["email"], "pod": en["pod"], "status": en["status"],
                         "solved_days": {}, "reopen_days": {}} for en in entries}
    # STEP 1: population = solved denominator (current owner, IST End Time day, each case once)
    cases, dupes, outside = {}, 0, 0
    for r in pop:
        cid = dget(r, "Id")
        if cid in cases:
            dupes += 1
            continue
        d = parse_ist(dget(r, F["end_time"]))
        owner = idmap.get(dget(r, F["owner_id"]))
        if not (s <= d <= e):
            outside += 1
            continue
        if not owner:
            continue
        cases[cid] = {"num": dget(r, "CaseNumber"), "account": dget(r, "Account.Name") or dget(r, "AccountId") or "", "owner": owner, "d": d}
        eng[owner["email"]]["solved_days"][str(d)] = eng[owner["email"]]["solved_days"].get(str(d), 0) + 1
    _POP["tab3"] = set(cases)
    # STEP 2/3: reopened definition in code
    by_case = defaultdict(list)
    for h in hist:
        if h.get("Field", "Status") == "Status" and h.get("CaseId") in cases:
            by_case[h["CaseId"]].append(h)
    info, before_start, after_end = {}, 0, 0
    for cid, rows_ in by_case.items():
        a = analyse_history(rows_)
        if not a:
            continue
        rd = a["reopen"].astimezone(TZ).date()
        if rd < s:
            before_start += 1
        elif rd > e:
            after_end += 1
        else:
            info[cid] = {**a, "rd": rd}
    # STEP 4: closure exclusion
    com_rows = reopen_comments_live(ctx, sorted(info)) if not MOCK else ctx["comments"]
    com_by = defaultdict(list)
    for c in com_rows:
        if is_human_customer(c):
            com_by[c["ParentId"]].append((parse_dt(c["CreatedDate"]), c.get("CommentBody") or ""))
    ovr = overrides()
    tickets = []
    for cid, a in info.items():
        c = cases[cid]
        lo = a["pc"] or dt.datetime.min.replace(tzinfo=UTC)
        cand = sorted(x for x in com_by.get(cid, []) if lo < x[0] <= a["reopen"])
        comment = cand[-1][1] if cand else ""
        if norm_case(c["num"]) in ovr:
            result, reason = "Counted as reopened (manual override)", "manual override"
        elif not cand:
            result, reason = "Counted as reopened", "no customer comment before the reopen"
        else:
            is_closure, reason = classify_closure(comment)
            result = "Excluded as closure" if is_closure else "Counted as reopened"
        wk = week_start(a["rd"])
        tickets.append({"case_id": cid, "case": c["num"], "account": c["account"], "owner": c["owner"]["name"], "owner_email": c["owner"]["email"],
                        "owner_pod": c["owner"]["pod"], "week": str(wk), "reopen_date": str(a["rd"]), "reopen_time": a["reopen"].astimezone(TZ).strftime("%Y-%m-%d %H:%M"),
                        "result": result, "reason": reason, "comment": comment.strip()[:600], "reopen_changes": a["n"]})
        cell = eng[c["owner"]["email"]]["reopen_days"].setdefault(str(a["rd"]), [0, 0, 0])
        cell[0] += 1
        cell[1 if result.startswith("Excluded") else 2] += 1
    tickets.sort(key=lambda x: (x["reopen_time"], x["case"] or ""))
    engs = sorted(eng.values(), key=lambda x: (x["pod"], x["name"]))

    # ---- all-owner count re-run ----
    all_post, moved = (all_pre, []) if MOCK else reopen_allpost_live(ctx)

    # ---- validation checks ----
    t1 = get_summary(start, end, refresh)
    t1e = {x["id"]: x for x in t1["engineers"]}
    tot1 = lambda d_: sum(c[0] + c[1] for c in d_.values())
    solved_tot = sum(sum(x["solved_days"].values()) for x in engs)
    t1_tot = sum(tot1(x["days"]) for x in t1["engineers"])
    pods = sorted({x["pod"] for x in engs})
    bad_pod = [p for p in pods if sum(sum(x["solved_days"].values()) for x in engs if x["pod"] == p) != sum(tot1(x["days"]) for x in t1["engineers"] if x["pod"] == p)]
    bad_eng = [x["name"] for x in engs if sum(x["solved_days"].values()) != tot1(t1e[x["id"]]["days"])]
    wk3, wk1 = defaultdict(int), defaultdict(int)
    for x in engs:
        for d_, n in x["solved_days"].items():
            wk3[str(week_start(dt.date.fromisoformat(d_)))] += n
    for x in t1["engineers"]:
        for d_, c in x["days"].items():
            wk1[str(week_start(dt.date.fromisoformat(d_)))] += c[0] + c[1]
    bad_wk = [w for w in set(wk1) | set(wk3) if wk1[w] != wk3[w]]
    counted = [t for t in tickets if not t["result"].startswith("Excluded")]
    r_eng = {x["id"]: sum(c[2] for c in x["reopen_days"].values()) for x in engs}
    r_pod = {p: sum(r_eng[x["id"]] for x in engs if x["pod"] == p) for p in pods}
    r_wk = defaultdict(int)
    for t in counted:
        r_wk[t["week"]] += 1
    over = [x["name"] for x in engs if r_eng[x["id"]] > sum(x["solved_days"].values())]
    cnt_t = defaultdict(int)
    for t in counted:
        cnt_t[t["owner_email"]] += 1
    pct_bad = [x["name"] for x in engs if cnt_t[x["id"]] != r_eng[x["id"]]]
    t2pop = _POP.get("tab2")
    same_ids = None if t2pop is None else (t2pop == set(cases))
    moved_txt = "; ".join(f"{m['case']} ({m['owner']}, modified {m['modified']})" for m in moved[:8])
    checks = [
        {"name": "Reopened per engineer is not more than that engineer's Tab 1 Owner count", "ok": not over, "detail": "all engineers within solved" if not over else f"over: {over[:6]}"},
        {"name": "Weekly total = sum of per-engineer totals = sum of per-POD totals", "ok": sum(r_wk.values()) == sum(r_eng.values()) == sum(r_pod.values()),
         "detail": f"weekly {sum(r_wk.values())}, engineers {sum(r_eng.values())}, PODs {sum(r_pod.values())}"},
        {"name": "Each reopened ticket counted once (zero duplicates)", "ok": len({t['case_id'] for t in tickets}) == len(tickets) and dupes == 0, "detail": f"{len(tickets)} tickets, {dupes} duplicate cases"},
        {"name": "Engineers match the Tab 1 roster; population is the same case IDs as Tab 1 / Tab 2",
         "ok": set(t1e) == set(eng) and same_ids is not False,
         "detail": f"{len(eng)} engineers" + ("; case IDs identical to Tab 2" if same_ids else "; open Tab 2 to compare case IDs (counts already match Tab 1)" if same_ids is None else "; CASE IDs DIFFER from Tab 2")},
        {"name": "Solved total = Tab 1 Owner count (overall)", "ok": solved_tot == t1_tot, "detail": f"Tab 3 {solved_tot} vs Tab 1 {t1_tot}"},
        {"name": "... per POD", "ok": not bad_pod, "detail": "all PODs match" if not bad_pod else f"mismatch: {bad_pod}"},
        {"name": "... per engineer", "ok": not bad_eng, "detail": "all engineers match" if not bad_eng else f"mismatch: {bad_eng[:6]}"},
        {"name": "... per week", "ok": not bad_wk, "detail": "all weeks match" if not bad_wk else f"mismatch weeks: {sorted(bad_wk)}"},
        {"name": "Reopen % = reopened / solved (engineer counts agree with the ticket list)", "ok": not pct_bad,
         "detail": f"overall {len(counted)} / {solved_tot} = " + (f"{100 * len(counted) / solved_tot:.1f}%" if solved_tot else "n/a") + ("" if not pct_bad else f"; mismatch: {pct_bad[:6]}")},
        {"name": "CaseHistory rows per slice equal the COUNT", "ok": all(a == b for a, b in hchecks), "detail": f"{sum(a for a, _ in hchecks)} rows in {len(hchecks)} slices vs COUNT {sum(b for _, b in hchecks)}"},
        {"name": "All-owner count re-run matches (no tickets changed during the run)", "ok": all_pre == all_post,
         "detail": f"first {all_pre}, re-run {all_post}" + ("" if all_pre == all_post else f". Changed: {moved_txt}")},
    ]
    a_, b_ = utc_bounds(s, e)
    ovr_rows = [t for t in tickets if "override" in t["result"]]
    return {
        "window": {"start": str(s), "end": str(e), "tz": "IST", "start_utc": a_.strftime("%Y-%m-%dT%H:%M:%SZ"), "end_utc": b_.strftime("%Y-%m-%dT%H:%M:%SZ")},
        "pods": pods, "engineers": engs, "tickets": tickets, "checks": checks, "roster_report": report,
        "outside": {"before_start": before_start, "after_end": after_end},
        "overrides": sorted(overrides()), "overrides_found": [t["case"] for t in ovr_rows],
        "notes": ["A ticket is reopened when its Status history has Pending Confirmation -> Assigned before the ticket was solved. Several such changes on one ticket count as ONE reopened ticket; "
                  "it is bucketed by the LAST one before the last change to Solved, converted to IST. Credit goes to the current case owner (roster engineers only).",
                  f"Reopens dated before the first day or after the last day of the window are not in the weekly numbers: {before_start} before {s}, {after_end} after {e}.",
                  "Closure exclusion is rule-based text classification of the last customer comment between the last move to Pending Confirmation and the reopen "
                  "(short, no question, no 'still / not resolved / unable', does not say not to close, and reads as a closure confirmation). Tickets with no customer comment stay counted. It is an approximation.",
                  "Reopen % = reopened after exclusion / solved. The numerator is bucketed by reopen week and the denominator by End Time week, so a week's % can exceed or trail what that week's own tickets show.",
                  "Manual overrides always stay counted as reopened: " + ", ".join(sorted(overrides())) + ". Add more under reopen_overrides in config.json."],
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"), "mock": MOCK,
    }


def reopened_xlsx(data):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    yellow, green = PatternFill("solid", fgColor="FFF2A8"), PatternFill("solid", fgColor="C6EFCE")
    heads = ["Case number", "Account", "Owner", "Week", "Reopen date (IST)", "Result", "Customer comment"]

    def wlabel(ws_):
        a = dt.date.fromisoformat(ws_)
        b = a + dt.timedelta(days=6)
        return f"{a.strftime('%b')} {a.day} - {b.strftime('%b')} {b.day}"

    def sheet(ws, rows):
        ws.append(heads)
        for c in ws[1]:
            c.font = Font(bold=True)
        for t in rows:
            ws.append([t["case"], t["account"], f"{t['owner']} ({t['owner_pod']})", wlabel(t["week"]), t["reopen_time"], t["result"], t["comment"]])
            fill = yellow if t["result"].startswith("Excluded") else green if "override" in t["result"] else None
            if fill:
                for c in ws[ws.max_row]:
                    c.fill = fill
        for col, w in zip("ABCDEFG", (14, 28, 28, 16, 18, 36, 80)):
            ws.column_dimensions[col].width = w
    wb = Workbook()
    sheet(wb.active, data["tickets"])
    wb.active.title = "Reopened tickets"
    sheet(wb.create_sheet("Excluded"), [t for t in data["tickets"] if t["result"].startswith("Excluded")])
    sheet(wb.create_sheet("Manual overrides"), [t for t in data["tickets"] if "override" in t["result"]])
    ws = wb.create_sheet("By engineer")
    ws.append(["Rank", "POD", "Engineer", "Solved", "Reopened (after exclusion)", "Reopen %"])
    for c in ws[1]:
        c.font = Font(bold=True)
    rows = []
    for x in data["engineers"]:
        sv, ro = sum(x["solved_days"].values()), sum(c[2] for c in x["reopen_days"].values())
        rows.append((ro, sv, x))
    for i, (ro, sv, x) in enumerate(sorted(rows, key=lambda r: (-r[0], r[2]["name"])), 1):
        ws.append([i, x["pod"], x["name"], sv, ro, f"{100 * ro / sv:.1f}%" if sv else "n/a"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def get_reopened(start=None, end=None, refresh=False):
    key = ("R", start, end, str(ist_today()))
    hit = _cache.get(key)
    if hit and not refresh and time.time() - hit[0] < CFG.get("cache_seconds", 300):
        return hit[1]
    data = reopened_summarize(start, end, refresh)
    _cache[key] = (time.time(), data)
    return data


@app.get("/api/reopened")
def api_reopened(start: str = None, end: str = None, refresh: bool = False):
    return get_reopened(start, end, refresh)


@app.get("/api/reopened.xlsx")
def api_reopened_xlsx(start: str = None, end: str = None):
    from starlette.responses import Response
    try:
        body = reopened_xlsx(get_reopened(start, end))
    except ImportError:
        raise HTTPException(501, "Excel export needs openpyxl: run  pip3 install -r requirements.txt  and restart.")
    return Response(body, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="reopened_tickets.xlsx"'})



# =====================================================================
# Tab 4: First Response (FR) milestone tracking
# Source: Salesforce CaseMilestone (milestone type "First Response"): IsViolated / IsCompleted / StartDate / CompletionDate / TargetResponseInMins
# Buckets (config.json -> "fr"):
#   crit  = Severity 1-2 and Impact >= 50%, GreyOrange scope
#   sev2  = Severity 2 and Impact < 50%,   GreyOrange scope
#   sev3  = Severity 3,                    GreyOrange scope
#   cust  = Customer scope Sev1-4 (Technical Impact Area in the customer-scope list) + GreyOrange scope Sev4
# =====================================================================
import statistics
FRC = CFG.get("fr", {})
FRF = FRC.get("fields", {"severity": "Severity__c", "impact": "Impact_Percentage__c", "tia": "Technical_Impact_Area__c"})
FR_BUCKETS = [("crit", "CritSit · Sev1/Sev2 ≥ 50%", "Excl. Customer Scope"), ("sev2", "Sev2 · < 50%", "Excl. Customer Scope"),
              ("sev3", "Non-CritSit · Sev3", "Excl. Customer Scope"), ("cust", "Customer Sev1–4 · GreyOrange Sev4", "Customer scope Sev1–4 + GreyOrange scope Sev4")]


def _norm(s):
    return " ".join(str(s or "").lower().split())


def fr_scope(tia):
    cs = {_norm(x) for x in FRC.get("customer_scope", [])}
    return "customer" if _norm(tia) in cs else "go"


def _num(v):
    m = re.search(r"-?\d+(?:\.\d+)?", str(v if v is not None else ""))
    return float(m.group()) if m else None


def fr_bucket(sev, imp, scope):
    if sev not in (1, 2, 3, 4):
        return None
    if scope == "customer" or sev == 4:  # customer scope (any Sev1-4) + GreyOrange-scope Sev4
        return "cust"
    thr = FRC.get("crit_impact_pct", 50)
    if sev in (1, 2) and imp is not None and imp >= thr:
        return "crit"
    if sev == 2 and imp is not None and imp < thr:
        return "sev2"
    if sev == 3:
        return "sev3"
    return None  # Sev1 below threshold, Sev2 without impact: not part of any definition


def fr_where_cases():
    return " AND ".join(["Id != null"] + [c_in(r["field"], r["values"]) for r in FRC.get("include", [])])


_resf = {}


def res_field():
    """API name of the Case field 'Resolution Time (min)': fr.fields.resolution in config.json, otherwise found by label."""
    cfgv = (FRF.get("resolution") or "").strip()
    if cfgv or MOCK:
        return cfgv or "Resolution_Time_Min__c"
    if "n" not in _resf:
        d = sf_get(f"/services/data/{sf_env()['ver']}/sobjects/Case/describe")
        cands = [x for x in d["fields"] if "resolution" in x["label"].lower() and "time" in x["label"].lower()]
        pick = next((x for x in cands if "min" in x["label"].lower() or "min" in x["name"].lower()), cands[0] if cands else None)
        if not pick:
            raise HTTPException(500, "Could not find a Case field labelled 'Resolution Time (min)'. Put its API name in config.json -> fr.fields.resolution "
                                     "(see /api/fr-fields for candidates).")
        _resf["n"] = pick["name"]
    return _resf["n"]


def fr_collect_live(s, e, idmap, slices, mname=None, extra=None):
    """Population = exactly the Solved-Tickets population (same filters, End Time window, roster owners), then the First Response milestone for those cases."""
    flt, idl = filters_where(), id_list(list(idmap))
    sel = (f"SELECT Id, CaseNumber, CreatedDate, {F['type']}, {F['owner_id']}, Account.Name, {F['end_time']}, "
           f"{FRF['severity']}, {FRF['impact']}, {FRF['tia']}{', ' + extra if extra else ''} FROM {CFG['object']}")

    def pop(sl):
        x, y = utc_bounds(*sl)
        try:
            return sf_query(f"{sel} WHERE {flt} AND {time_cond(x, y)} AND {F['owner_id']} IN ({idl}) ORDER BY {F['end_time']}")
        except HTTPException as ex:
            raise HTTPException(500, f"{ex.detail}  -- Check the field names under 'fr' in config.json (open /api/fr-fields).")

    def ms(ids):
        return sf_query("SELECT CaseId, StartDate, CompletionDate, IsCompleted, IsViolated, TargetResponseInMins FROM CaseMilestone "
                        f"WHERE MilestoneType.Name = {sq(mname or FRC.get('milestone', 'First Response'))} AND CaseId IN ({id_list(ids)})")

    with ThreadPoolExecutor(max_workers=5) as ex:
        cases = _flat(ex.map(pop, slices)) if idmap else []
        ids = [c["Id"] for c in cases]
        mrows = _flat(ex.map(ms, _chunks(ids, 150))) if ids and not extra else []
    return cases, mrows


def fr_collect_mock(s, e, idmap, entries, slices, kind="fr"):
    rnd = random.Random(11)
    cases = [r for r in mock_rows(s, e, entries) if passes(r) and dget(r, F["owner_id"]) in idmap and s <= parse_ist(dget(r, F["end_time"])) <= e]
    tia_vals = ["Hardware Fault", "Software Bug", "Config Issue", None] + FRC.get("customer_scope", [])[:4]
    mrows, f = [], "%Y-%m-%dT%H:%M:%S.000+0000"
    for c in cases:
        sev = rnd.choices([1, 2, 3, 4], [10, 35, 40, 15])[0]
        c[FRF["severity"]], c[FRF["impact"]] = f"Severity {sev}", (rnd.choice([10, 30, 50, 60, 80, 100]) if sev <= 2 else None)
        c[FRF["tia"]] = rnd.choice(tia_vals)
        end = _pdt(dget(c, F["end_time"]))
        created = end - dt.timedelta(hours=rnd.randint(2, 70))
        c["CreatedDate"] = created.strftime(f)
        if kind == "res":
            tg = {1: 60, 2: 90, 3: 1440, 4: 1440}[sev]
            c[res_field()] = None if rnd.random() < .04 else max(5, round(rnd.gauss(tg * .85, tg * .45), 1))
            continue
        if rnd.random() < .05:
            continue  # no FR milestone on this case
        tgt = {1: 15, 2: 30, 3: 60, 4: 120}[sev]
        frm = max(1, int(rnd.gauss(tgt * .8, tgt * .5)))
        if kind == "res":
            tgt = {1: 60, 2: 90, 3: 1440, 4: 1440}[sev]
            frm = max(5, int(rnd.gauss(tgt * .85, tgt * .45)))
        mrows.append({"CaseId": c["Id"], "StartDate": created.strftime(f), "CompletionDate": (created + dt.timedelta(minutes=frm)).strftime(f),
                      "IsCompleted": True, "IsViolated": frm > tgt, "TargetResponseInMins": tgt})
    return cases, mrows


def _pdt(ts):
    return dt.datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z") if ts else None


def fr_summarize(start=None, end=None, kind="fr"):
    RES = kind == "res"
    label = "Resolution" if RES else "First Response"
    if start and end:
        s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    else:
        s, e = rolling_window()
    entries, idmap, report = resolve_roster()
    slices = week_slices(s, e)
    cases, mrows = fr_collect_mock(s, e, idmap, entries, slices, kind) if MOCK else fr_collect_live(s, e, idmap, slices, None, res_field() if RES else None)
    mmap, dup_ms = {}, 0
    for m in mrows:
        if m["CaseId"] in mmap:
            dup_ms += 1
        mmap[m["CaseId"]] = m
    tickets, seen, dupes, outside, unclass, nomile, miss_sev, miss_imp = [], set(), 0, 0, 0, 0, 0, 0
    for c in cases:
        cid = c["Id"]
        if cid in seen:
            dupes += 1
            continue
        seen.add(cid)
        d = parse_ist(dget(c, F["end_time"]))
        if not (s <= d <= e):
            outside += 1
            continue
        own = idmap.get(dget(c, F["owner_id"]))
        if not own:
            continue
        sv = _num(dget(c, FRF["severity"]))
        sev = int(sv) if sv is not None else None
        imp = _num(dget(c, FRF["impact"]))
        tia = dget(c, FRF["tia"])
        scope = fr_scope(tia)
        if sev is None:
            miss_sev += 1
        if sev in (1, 2) and imp is None and scope == "go":
            miss_imp += 1
        b = fr_bucket(sev, imp, scope)
        m = mmap.get(cid)
        created = _pdt(dget(c, "CreatedDate"))
        out, mins, tgt = "none", None, None
        if RES:
            rv = _num(dget(c, res_field()))
            ov = FRC.get("resolution_target_minutes", {}).get(b) if b else None
            if rv is None:
                nomile += 1
            else:
                mins, tgt = round(rv, 4), ov
                out = ("met" if mins < ov else "missed") if ov else "none"
        elif m:
            tgt = m.get("TargetResponseInMins")
            st = _pdt(m.get("StartDate")) or created
            cp = _pdt(m.get("CompletionDate"))
            if m.get("IsCompleted") and cp:
                out, mins = ("missed" if m.get("IsViolated") else "met"), round((cp - st).total_seconds() / 60, 4)
            elif m.get("IsViolated"):
                out = "missed"
            else:
                out = "open"
            ov = (FRC.get("resolution_target_minutes", {}) if RES else FRC.get("target_minutes", {})).get(b)
            if ov:  # fixed target for this bucket (e.g. 2 h): judge by FR time instead of the milestone's own flag
                tgt = ov
                if mins is not None:
                    out = "met" if mins <= ov else "missed"
                elif (dt.datetime.now(UTC) - st).total_seconds() / 60 > ov:
                    out = "missed"
                else:
                    out = "open"
        else:
            nomile += 1
        if b is None:
            unclass += 1
        tickets.append({"case": dget(c, "CaseNumber"), "account": dget(c, "Account.Name") or "", "owner": own["name"], "pod": own["pod"],
                        "sev": sev, "impact": imp, "tia": tia or "(blank)", "scope": scope, "bucket": b or "none",
                        "created": created.astimezone(TZ).strftime("%Y-%m-%d %H:%M") if created else "", "end": d.isoformat(), "date": d.isoformat(),
                        "type": dget(c, F["type"]) or "", "target": tgt, "fr_min": mins, "outcome": out})
    pods = sorted({en["pod"] for en in entries})
    weeks = [{"from": a.isoformat(), "to": b.isoformat(), "partial": a.weekday() != 6 or b.weekday() != 5} for a, b in slices]
    t1 = None
    try:
        if not MOCK:
            sm = get_summary(start, end)
            t1 = sum(c[0] + c[1] for x in sm["engineers"] for c in x["days"].values())
    except Exception:
        t1 = None
    out = {"window": {"start": s.isoformat(), "end": e.isoformat()}, "generated_at": dt.datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S"), "mock": MOCK,
           "buckets": [{"key": k, "label": l, "sub": sub} for k, l, sub in FR_BUCKETS], "weeks": weeks, "pods": pods,
           "total": len(tickets), "solved_tab_total": t1, "no_milestone": nomile, "unclassified": unclass,
           "tickets": sorted(tickets, key=lambda t: t["created"], reverse=True)}
    out["checks"] = [
        {"name": "Ticket count = Solved Tickets tab (roster-owned)", "ok": t1 is None or t1 == len(tickets),
         "detail": f"{len(tickets)} here vs {t1 if t1 is not None else 'n/a (mock)'} there"},
        {"name": "Each case counted once", "ok": dupes == 0 and dup_ms == 0, "detail": f"{dupes} duplicate cases, {dup_ms} duplicate milestone rows ignored"},
        {"name": "Every case dated inside the window", "ok": outside == 0, "detail": f"{outside} outside"},
        {"name": ("Cases without a Resolution Time (min) value" if RES else "Cases without a First Response milestone"), "ok": nomile == 0, "detail": f"{nomile} (counted in totals, no {label.lower()} time)"},
        {"name": "Tickets matching none of the 4 definitions", "ok": unclass == 0, "detail": f"{unclass} (Sev1 under 50% impact, Sev2 without impact %, or no severity)"},
        {"name": "Severity missing", "ok": miss_sev == 0, "detail": f"{miss_sev} tickets"},
        {"name": "Sev1/2 GreyOrange-scope tickets without Impact %", "ok": miss_imp == 0, "detail": f"{miss_imp} tickets"}]
    if RES:
        out["notes"] = ["Population is the same as the Solved Tickets tab: Status = Solved, Category = Software, Record Type = Ticket Form, Type != Alert, roster engineers only (current owner), dated by End Time (IST).",
                        "Resolution source: Case field 'Resolution Time (min)' (%s), value in minutes. Met = resolution time below the target (strictly less)." % res_field(),
                        "Resolution targets (met = resolved within target): " + ", ".join(f"{k} = {v} min" for k, v in FRC.get("resolution_target_minutes", {}).items()),
                        "Customer scope = Technical Impacted Area in: " + "; ".join(FRC.get("customer_scope", [])) + ". Blank or any other value = GreyOrange scope.",
                        "CritSit Impact threshold = %s%%." % FRC.get("crit_impact_pct", 50)]
    else:
        out["notes"] = ["Population is the same as the Solved Tickets tab: Status = Solved, Category = Software, Record Type = Ticket Form (not IM Ticket), Type != Alert, roster engineers only (current owner), dated by End Time (IST).",
                        "First Response source: Salesforce CaseMilestone '%s'. FR minutes = CompletionDate minus milestone StartDate. Met = completed and not violated; Missed = violated; Open = still running." % FRC.get("milestone", "First Response"),
                        "Customer scope = Technical Impacted Area in: " + "; ".join(FRC.get("customer_scope", [])) + ". Blank or any other value = GreyOrange scope.",
                        "CritSit Impact threshold = %s%%." % FRC.get("crit_impact_pct", 50),
                        "Fixed FR targets: " + ", ".join(f"{k} = {v} min" for k, v in FRC.get("target_minutes", {}).items()) + " (met/missed judged on FR time vs this target); other buckets use the Salesforce milestone result."]
    return out


def get_fr(start=None, end=None, refresh=False, kind="fr"):
    key = ("FR" if kind == "fr" else "RES", start, end, str(ist_today()))
    hit = _cache.get(key)
    if hit and not refresh and time.time() - hit[0] < CFG.get("cache_seconds", 300):
        return hit[1]
    data = fr_summarize(start, end, kind)
    _cache[key] = (time.time(), data)
    return data


@app.get("/api/resolution")
def api_res(start: str = None, end: str = None, refresh: bool = False):
    return get_fr(start, end, refresh, "res")


@app.get("/api/firstresponse")
def api_fr(start: str = None, end: str = None, refresh: bool = False):
    return get_fr(start, end, refresh)


# =====================================================================
# Tab 5: FR Live Tracker (independent of the other tabs)
# Open tickets (all owners) whose First Response is breached within the last 7 days or will breach within the next 2 hours.
# due = milestone TargetDate (or StartDate + fixed target for buckets listed in fr.target_minutes). Times are IST in the UI; the browser ticks the countdowns.
# =====================================================================
_live = {}


def lightning_base():
    u = (sf_env().get("url") or "").rstrip("/") if not MOCK else "https://greyorangeorg.my.salesforce.com"
    return u.replace(".my.salesforce.com", ".lightning.force.com")


def fr_live_collect(now):
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    since = (now - dt.timedelta(days=9)).strftime(fmt)
    where = " AND ".join([f"MilestoneType.Name = {sq(FRC.get('milestone', 'First Response'))}", "Case.IsClosed = false", c_not("Case.Status", ["Solved"]), f"StartDate >= {since}"]
                         + [c_in("Case." + r["field"], r["values"]) for r in CFG.get("include", [])]
                         + [c_not("Case." + r["field"], r["values"]) for r in CFG.get("exclude", [])] + combo_not("Case."))
    q = (f"SELECT Id, CaseId, StartDate, TargetDate, CompletionDate, IsCompleted, IsViolated, TargetResponseInMins, Case.CaseNumber, Case.CreatedDate, Case.Status, "
         f"Case.OwnerId, Case.Owner.Name, Case.Account.Name, Case.{FRF['severity']}, Case.{FRF['impact']}, Case.{FRF['tia']} FROM CaseMilestone WHERE {where} ORDER BY StartDate")
    return sf_query(q)


def fr_live_mock(now, entries):
    rnd = random.Random(int(now.timestamp() // 60))
    live = [en for en in entries if en["ids"]]
    f, rows = "%Y-%m-%dT%H:%M:%S.000+0000", []
    for i in range(46):
        sev = rnd.choices([1, 2, 3, 4], [10, 30, 45, 15])[0]
        tgt = {1: 15, 2: 30, 3: 120, 4: 120}[sev]
        start = now - dt.timedelta(minutes=rnd.choice([rnd.randint(1, tgt * 2), rnd.randint(tgt, 60 * 24 * 6)]))
        due = start + dt.timedelta(minutes=tgt)
        done = rnd.random() < .35
        comp = start + dt.timedelta(minutes=int(tgt * rnd.uniform(.4, 1.8))) if done else None
        own = rnd.choice(live + [None, None])
        rows.append({"Id": f"m{i}", "CaseId": f"500LIVE{i:04d}", "StartDate": start.strftime(f), "TargetDate": due.strftime(f), "TargetResponseInMins": tgt,
                     "CompletionDate": comp.strftime(f) if comp else None, "IsCompleted": done, "IsViolated": bool(comp and comp > due),
                     "Case": {"CaseNumber": str(310000 + i), "CreatedDate": start.strftime(f), "Status": rnd.choice(["Assigned", "In Progress", "Pending Customer"]),
                              "OwnerId": own["ids"][0] if own else "005OTHER", "Owner": {"Name": own["name"] if own else "Support Queue"},
                              "Account": {"Name": rnd.choice(["Acme Retail", "Globex", "Initech", "Umbrella"])},
                              FRF["severity"]: f"Severity {sev}", FRF["impact"]: rnd.choice([20, 50, 80]) if sev <= 2 else None,
                              FRF["tia"]: rnd.choice(["Software Bug", "Config Issue", None, "Customer WMS Issue"])}})
    return rows


def fr_live_build():
    now = dt.datetime.now(UTC)
    entries, idmap, _ = resolve_roster()
    rows = fr_live_mock(now, entries) if MOCK else fr_live_collect(now)
    tmap, out, seen = FRC.get("target_minutes", {}), [], set()
    horizon, win = now + dt.timedelta(hours=2), now - dt.timedelta(days=7)
    for r in rows:
        if r["CaseId"] in seen:
            continue
        seen.add(r["CaseId"])
        c = r.get("Case") or {}
        sv = _num(c.get(FRF["severity"]))
        sev = int(sv) if sv is not None else None
        imp, tia = _num(c.get(FRF["impact"])), c.get(FRF["tia"])
        scope = fr_scope(tia)
        b = fr_bucket(sev, imp, scope)
        st = _pdt(r.get("StartDate"))
        ov = FRC.get("live_target_minutes", {}).get(str(sev))  # live tracker rule: Sev1/2 = 15 min, Sev3/4 = 2 h, counted from the FR start date
        tgt = ov or r.get("TargetResponseInMins")
        due = (st + dt.timedelta(minutes=ov)) if (ov and st) else _pdt(r.get("TargetDate")) or ((st + dt.timedelta(minutes=tgt)) if st and tgt else None)
        if not due:
            continue
        cp = _pdt(r.get("CompletionDate")) if r.get("IsCompleted") else None
        if cp:
            if cp <= due or due < win:
                continue
            state, late = "late", round((cp - due).total_seconds())
        elif due <= now:
            if due < win:
                continue
            state, late = "nores", round((now - due).total_seconds())
        elif due <= horizon:
            state, late = "soon", None
        else:
            continue
        own = idmap.get(c.get("OwnerId"))
        if not own:
            continue  # roster engineers only
        out.append({"id": r["CaseId"], "case": c.get("CaseNumber"), "account": dget(c, "Account.Name") or "", "owner": own["name"] if own else (dget(c, "Owner.Name") or "(unknown)"),
                    "pod": own["pod"] if own else "Non-roster", "status": c.get("Status") or "", "bucket": b or "none", "sev": sev, "impact": imp, "tia": tia or "(blank)",
                    "start": st.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S") if st else "", "due": due.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S"), "due_ms": int(due.timestamp() * 1000),
                    "target": tgt, "state": state, "late_s": late, "done_ms": int(cp.timestamp() * 1000) if cp else None})
    out.sort(key=lambda t: t["due_ms"])
    return {"generated_at": dt.datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S"), "now_ms": int(now.timestamp() * 1000), "mock": MOCK,
            "link_base": lightning_base(), "buckets": [{"key": k, "label": l} for k, l, _ in FR_BUCKETS] + [{"key": "none", "label": "Other (no FR bucket)"}], "tickets": out,
            "notes": ["Open tickets (not Solved/Closed) owned by roster engineers (POD-1 to POD-4), same Software / Ticket Form / not-Alert filters as the other tabs.",
                      "Breached = First Response not met by its due time in the last 7 days: 'No response yet' (still waiting) or 'Responded late'. Going to breach = no response yet and due within the next 2 hours.",
                      "Due time = First Response start date + target by severity: Sev1/Sev2 = 15 minutes, Sev3/Sev4 = 2 hours (config: fr.live_target_minutes). Countdown ticks in the browser; data refreshes every minute."]}


@app.get("/api/fr-live")
def api_fr_live(refresh: bool = False):
    hit = _live.get("d")
    if hit and not refresh and time.time() - hit[0] < 30:
        return hit[1]
    d = fr_live_build()
    _live["d"] = (time.time(), d)
    return d


@app.get("/api/fr-fields")
def api_fr_fields():
    """Helps find the right Case API field names for severity / impact % / technical impact area, and lists milestone types."""
    d = sf_get(f"/services/data/{sf_env()['ver']}/sobjects/Case/describe")
    pat = re.compile(r"severity|impact|technical|sla|priority|resolution", re.I)
    f = [{"name": x["name"], "label": x["label"], "type": x["type"], "values": [v["value"] for v in x["picklistValues"] if v["active"]][:20]}
         for x in d["fields"] if pat.search(x["name"] + " " + x["label"])]
    mt = [r["Name"] for r in sf_query("SELECT Name FROM MilestoneType")]
    tia_all = next((v for x in d["fields"] if x["name"] == FRF["tia"] for v in [[p["value"] for p in x["picklistValues"] if p["active"]]]), [])
    import difflib
    match = [{"configured": cs, "exact_match": any(_norm(cs) == _norm(v) for v in tia_all),
              "closest_in_salesforce": [v for v in tia_all if _norm(v) != _norm(cs)][:0] or difflib.get_close_matches(cs, tia_all, n=2, cutoff=.4)}
             for cs in FRC.get("customer_scope", [])]
    return {"configured": FRF, "technical_impact_area_all_values": tia_all, "customer_scope_check": match,
            "candidate_fields": f, "milestone_types": mt}



@app.get("/api/fields")
def api_fields():
    d = sf_get(f"/services/data/{sf_env()['ver']}/sobjects/{CFG['object']}/describe")
    return [{"name": f["name"], "label": f["label"], "type": f["type"],
             "picklist": [v["value"] for v in f["picklistValues"] if v["active"]][:25]} for f in d["fields"]]


@app.get("/api/auth-check")
def api_auth_check():
    """Quick test of the OAuth credentials: gets a token and runs a trivial query."""
    sf_query("SELECT Id FROM User LIMIT 1")
    return {"ok": True, "instance": _tok["inst"], "api_version": sf_env()["ver"]}


@app.get("/api/roster-check")
def api_roster_check():
    return resolve_roster()[2]


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
