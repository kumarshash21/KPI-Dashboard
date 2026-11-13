# Deploying the Salesforce Support Dashboard to an always-on server

This guide moves the dashboard from a laptop to a server that is always on, so anyone on your network can open it
at a fixed address such as `http://172.35.0.13:4173`.

The server version signs in to Salesforce with **API credentials** (a Connected App using the OAuth 2.0 *client
credentials* flow). It needs no Salesforce CLI, no browser login and has no session that expires.

---

## 0. What you need

| Item | Details |
|---|---|
| Server | Linux with systemd (Ubuntu 20.04+, Debian 11+, RHEL/Rocky/Amazon Linux 2+). 1 vCPU / 1 GB RAM is enough. |
| Software | Python 3.9 or newer, `unzip`. (Docker is an optional alternative, see step 9.) |
| Network out | HTTPS (443) from the server to `greyorangeorg.my.salesforce.com`. |
| Network in | TCP 4173 open from the people who will use the dashboard (office network or VPN). |
| Salesforce | The Connected App's **Client ID** and **Client Secret** (step 1). |
| Access | SSH login with `sudo` rights on the server. |

Check the basics on the server:

```bash
python3 --version          # must be 3.9 or higher
curl -sI https://greyorangeorg.my.salesforce.com | head -1     # should print an HTTP status line
```

The server's clock must be correct (the dashboard uses today's date for its rolling window). On most servers this is
automatic; check with `timedatectl`.

---

## 1. Salesforce Connected App (one-time, ask your Salesforce admin)

If you already have the Client ID and Secret, check these settings are in place, otherwise sign-in will fail:

1. Setup → App Manager → the Connected App → **Manage → Edit Policies** (or Edit).
2. OAuth is enabled, with the scope **Manage user data via APIs (api)**.
3. **Enable Client Credentials Flow** is ticked.
4. **Run As** (Client Credentials Flow section of Edit Policies) is set to a user. *Every query runs as this user.*
   That user needs read access to **Case** (including `End_Time_of_Incident__c`, `Category__c`, `Type`, `Status`,
   `OwnerId`) and to **User**, and must be able to see cases owned by all 52 engineers. If it cannot, totals will be lower
   than your Salesforce reports.
5. Note the **Consumer Key** (= Client ID) and **Consumer Secret** (= Client Secret).

---

## 2. Copy the project to the server

On your laptop, send the zip (it contains no secrets) to the server:

```bash
scp sf-dashboard-server.zip youruser@172.35.0.13:/tmp/
```

On the server:

```bash
sudo mkdir -p /opt/sf-dashboard
sudo chown $USER /opt/sf-dashboard
unzip /tmp/sf-dashboard-server.zip -d /opt
# the zip unpacks to /opt/sf-dashboard-server  ->  move its contents into /opt/sf-dashboard
cp -r /opt/sf-dashboard-server/. /opt/sf-dashboard/ && rm -rf /opt/sf-dashboard-server
cd /opt/sf-dashboard
ls
```

You should see: `app.py  config.json  roster.csv  requirements.txt  static/  deploy/  .env.example  Dockerfile ...`

> You can use a different folder than `/opt/sf-dashboard`; just use it consistently below.

---

## 3. Create the `.env` file with the credentials

```bash
cd /opt/sf-dashboard
cp .env.example .env
nano .env
```

Fill in:

```
SF_CLIENT_ID=<your consumer key>
SF_CLIENT_SECRET=<your consumer secret>
SF_INSTANCE_URL=https://greyorangeorg.my.salesforce.com
SF_API_VERSION=v60.0

# optional but recommended on a shared server (see step 8)
DASH_USER=team
DASH_PASSWORD=<a strong password>
```

Then lock the file down:

```bash
chmod 600 .env
```

**Never email, chat, commit or upload `.env`.** It holds the Salesforce secret.

---

## 4. Install (automatic)

```bash
cd /opt/sf-dashboard
sudo bash deploy/install.sh
```

The script creates a virtualenv, installs the packages, installs a systemd service called `sf-dashboard` that starts at
boot and restarts if it crashes, and starts it (if the credentials are filled in).

<details><summary>Manual equivalent (if you prefer not to use the script)</summary>

```bash
cd /opt/sf-dashboard
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
sed -e "s|__APP_DIR__|/opt/sf-dashboard|g" -e "s|__USER__|$USER|g" deploy/sf-dashboard.service | sudo tee /etc/systemd/system/sf-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now sf-dashboard
```
</details>

---

## 5. Test it on the server

```bash
curl http://localhost:4173/api/auth-check
```

Expected: `{"ok":true,"instance":"https://...","api_version":"v60.0"}`
(If you set `DASH_USER`/`DASH_PASSWORD`, add `-u team:yourpassword`.)

If it fails, see Troubleshooting (section 11). Then check the roster:

```bash
curl http://localhost:4173/api/roster-check
```

It should list 52 engineers. Anyone not found in Salesforce (for example a new joiner) shows as "not in Salesforce yet".

---

## 6. Open the firewall port

Pick the command for your system (skip if no firewall is active):

```bash
# Ubuntu / Debian (ufw)
sudo ufw allow 4173/tcp

# RHEL / Rocky / Amazon Linux (firewalld)
sudo firewall-cmd --permanent --add-port=4173/tcp && sudo firewall-cmd --reload
```

Cloud servers (AWS/Azure/GCP) also need an inbound rule for TCP 4173 in the security group, restricted to your office /
VPN address range.

---

## 7. Open the dashboard

From any computer on the network: **`http://<server-ip>:4173`** (for example `http://172.35.0.13:4173`).

The first load takes 20-40 seconds (about 20 Salesforce queries). After that data is cached and the page refreshes
itself every 5 minutes; "Last refreshed" shows at the top. Share that URL with your team.

---

## 8. Security (read before sharing the URL)

- **Login:** set `DASH_USER` / `DASH_PASSWORD` in `.env` and restart (`sudo systemctl restart sf-dashboard`). The browser then
  asks for a username and password. Without these, anyone who can reach the port sees the data.
- **Restrict access:** allow port 4173 only from your office/VPN range (firewall or cloud security group).
- **HTTPS:** HTTP Basic login sends the password unencrypted over plain HTTP. On an internal network or VPN this is often
  acceptable; otherwise put nginx in front for HTTPS using `deploy/nginx.conf.example` (and then you can close port 4173
  to everyone except localhost by editing `--host 0.0.0.0` to `--host 127.0.0.1` in the service file).
- **Secrets:** keep `.env` at `chmod 600`. Rotate the Client Secret in Salesforce if it is ever exposed.
- All users see the same data: whatever the Run As user can see.

---

## 9. Alternative: run with Docker

If the server has Docker instead of a Python setup:

```bash
cd /opt/sf-dashboard
cp .env.example .env && nano .env        # fill in the credentials
docker compose up -d --build
docker compose logs -f                   # Ctrl+C to leave the logs
```

`restart: unless-stopped` keeps it running across reboots. Skip steps 4 (install script) if you use Docker.

---

## 10. Day-to-day operations

| Task | Command |
|---|---|
| Status | `sudo systemctl status sf-dashboard` |
| Live logs | `sudo journalctl -u sf-dashboard -f` |
| Restart | `sudo systemctl restart sf-dashboard` |
| Stop / start | `sudo systemctl stop sf-dashboard` / `sudo systemctl start sf-dashboard` |
| Change roster | Edit `/opt/sf-dashboard/roster.csv` (columns `name,email,pod`), then restart |
| Change filters, window, refresh time | Edit `/opt/sf-dashboard/config.json` (`include`, `exclude`, `rolling`, `cache_seconds`), then restart |
| Update the code | Replace `app.py` / `static/index.html` / `config.json`, then restart. Browsers: hard refresh (Ctrl+Shift+R). |
| Back up | Keep a copy of the folder (except `venv/`). Store `.env` separately and securely. |

Reboot test (recommended once): `sudo reboot`, then check the URL opens again by itself after a minute.

---

## 11. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `auth-check` says "Missing SF_CLIENT_ID / SF_CLIENT_SECRET" | `.env` is empty or in the wrong folder. It must sit next to `app.py`. Restart after editing. |
| "token request failed (400) invalid_client" | Wrong Client ID/Secret, or **Enable Client Credentials Flow** is off. |
| "token request failed ... invalid_grant / no client credentials user" | **Run As** user not set on the Connected App. |
| "Cannot reach Salesforce" | Server has no outbound HTTPS to `greyorangeorg.my.salesforce.com` (proxy/firewall). Test with `curl -I`. |
| "Salesforce API error (403) ..." or totals lower than reports | Run As user lacks access to Case/User or to those cases. Ask the admin to widen its permissions. |
| "INVALID_FIELD ..." | A field name in `config.json` does not exist or is not visible to the Run As user. Open `/api/fields` to list available fields. |
| Page will not open from another computer | Firewall / security group not open on TCP 4173, or you are not on the office network/VPN. |
| 401 "Login required" | `DASH_USER`/`DASH_PASSWORD` is set: enter those, or clear them in `.env` and restart. |
| Service keeps restarting | `sudo journalctl -u sf-dashboard -n 50` shows the Python error (often a typo in `.env` or `config.json`). |
| Stale data in the browser | Hard refresh (Ctrl+Shift+R). The server sends no-cache headers; data itself refreshes every 5 minutes. |
| First load very slow | Normal (about 20 Salesforce queries). If it exceeds 3 minutes behind nginx, raise `proxy_read_timeout`. |

---

## 12. What is in the package

```
sf-dashboard-server/
  app.py                 backend (FastAPI + Salesforce REST client)
  config.json            field mapping, filters, rolling window, refresh time
  roster.csv             52 engineers: name,email,pod
  requirements.txt       Python packages (fastapi, uvicorn)
  static/index.html      the dashboard page
  .env.example           template for the secrets file (copy to .env)
  deploy/install.sh      automatic installer (venv + systemd service)
  deploy/sf-dashboard.service   systemd unit template
  deploy/nginx.conf.example     optional HTTPS reverse proxy
  Dockerfile, docker-compose.yml, .dockerignore   optional Docker run
  DEPLOY-GUIDE.md        this guide
```

Rules the dashboard applies (unchanged from the laptop version): Status = Solved, Category = Software, Type is not Alert;
Incident = Type Incident, everything else SR/Query; tickets credited to the current owner; dated by End Time of Incident
converted to IST; weeks run Sunday-Saturday; averages divide by Monday-Friday days; window is today-37 to today-7.
