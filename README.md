# Salesforce Live Dashboard - Owner by End Time (API credentials version)

Same dashboard as the SSO version, but authenticates with a Salesforce Connected App using the
OAuth 2.0 **Client Credentials** flow and queries the REST API directly (no Salesforce CLI, no browser login,
no session expiry). Good for running on a server.

## Setup
1. Fill the two secrets in `.env` (SF_CLIENT_ID, SF_CLIENT_SECRET). Keep this file private; it is git-ignored and
   excluded from the zip. SF_INSTANCE_URL and SF_API_VERSION are already set.
2. `pip3 install -r requirements.txt`
3. Double-click `start-dashboard.command` (or run `python3 -m uvicorn app:app --host 0.0.0.0 --port 4173`)
4. Open http://localhost:4173 . Test the credentials at http://localhost:4173/api/auth-check

## Connected App requirements (ask your Salesforce admin if auth-check fails)
- Connected App with OAuth enabled and **Enable Client Credentials Flow** ticked, scope `api` (Manage user data via APIs)
- A **Run As** user set on the app (Manage > Edit Policies > Client Credentials Flow). All queries run as that user, so it
  needs read access to Case (incl. End_Time_of_Incident__c, Category__c) and User, and must see cases owned by all 52 engineers.
- Token endpoint used: `{SF_INSTANCE_URL}/services/oauth2/token` (My Domain URL).

## Files
app.py (backend), config.json (mapping/filters/window), roster.csv, static/index.html (UI), .env (secrets).
Everything else (widgets, rules, rolling window, validation) is identical to the SSO version - see config.json.

## Tab 2 - Owner vs Assisted (Weekly)
Second tab (`/api/assisted`): Owner total, Assisted total, assisted share, weekly Owner vs Assisted, per-engineer table by POD.
Assisted = Rule 1 (qualifying external email from a same-POD roster engineer, approximation) OR Rule 2 (case team member), once per
engineer per ticket, only on roster-owned tickets, never the owner. Validation checks + the full Assisted ticket list are behind the
"Show validation list & notes" button. Needs read access to EmailMessage and CaseTeamMember for the Run As user.

## Look & feel
Light/dark theme button (top right; remembered in the browser), colour-coded tables (each POD has its own colour), initials avatars,
zebra rows, hover highlight, click a row to pin it, click a heatmap cell or name for details, Expand/Collapse all PODs, "/" focuses search.

## Tab 3 - Reopened Tickets (Weekly)
`/api/reopened` (+ Excel download `/api/reopened.xlsx`). Reopened = Status history Pending Confirmation -> Assigned before the ticket was solved
(several = ONE ticket, dated by the LAST one, IST). Closure exclusion = rule-based text test of the last customer CaseComment before the reopen.
Manual overrides (always counted): `reopen_overrides` in config.json. Needs read access to CaseHistory and CaseComment for the Run As user,
and `pip3 install -r requirements.txt` (adds openpyxl for the Excel file).
