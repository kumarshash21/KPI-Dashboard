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
import base64, csv, hmac, json, os, random, threading, time, urllib.error, urllib.parse, urllib.request
import datetime as dt
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
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
    if user and pw:
        ok, h = False, request.headers.get("authorization", "")
        if h.lower().startswith("basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                ok = hmac.compare_digest(u, user) and hmac.compare_digest(p, pw)
            except Exception:
                ok = False
        if not ok:
            from starlette.responses import Response
            return Response("Login required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Support Dashboard"'})
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp

_cache = {}
_roster_cache = {}


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


def _http(method, url, data=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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


def filters_where():
    parts = [base_where()]
    parts += [c_in(r["field"], r["values"]) for r in CFG.get("include", [])]
    parts += [c_not(r["field"], r["values"]) for r in CFG.get("exclude", [])]
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
                    F["owner_id"]: oid, "Owner": {"Name": oname},
                    F["end_time"]: t.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
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
@app.get("/api/summary")
def api_summary(start: str = None, end: str = None, refresh: bool = False):
    key = (start, end, str(ist_today()))
    hit = _cache.get(key)
    if hit and not refresh and time.time() - hit[0] < CFG.get("cache_seconds", 300):
        return hit[1]
    if refresh:
        _roster_cache.clear()
    data = summarize(start, end)
    _cache[key] = (time.time(), data)
    return data


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
