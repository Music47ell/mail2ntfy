# mail2ntfy

Watches one Gmail mailbox and one IMAP mailbox over IMAP and pushes a
notification to your existing self-hosted [ntfy](https://ntfy.sh) server when
new email arrives. Notifications are near-instant thanks to IMAP IDLE.

```
Gmail / IMAP → IMAP (993, outbound only) → mail2ntfy container
              → ntfy container on the shared "tunnel-net" Docker network
              → your Android ntfy app
```

No inbound ports are opened. The watcher makes outbound connections to the
mail providers and to the internal ntfy service only.

## Files

```
watcher/watcher.py      IDLE + polling + notification logic (stdlib only)
watcher/Dockerfile      python:3.14-slim, runs as non-root (UID 1000)
compose.yaml            the mail2ntfy service (pulls the GHCR image, no ports)
.github/workflows/build.yml  builds and pushes the image to GHCR
.env.example            every variable, with comments
.gitignore              excludes .env, *.db, __pycache__/, *.pyc
```

Key behaviour: holds an IMAP IDLE session per mailbox (via Python 3.14's
`imaplib` Idler) so new mail is notified within ~1s of arrival. Each IDLE
session is renewed every `IDLE_TIMEOUT` seconds (default 300), and a poll runs
after every renewal as a safety net — so mail is never missed even if an IDLE
notification is dropped. Servers that don't advertise IDLE automatically fall
back to polling every `POLL_INTERVAL` seconds (default 30). The watcher never
downloads bodies or marks mail read, decodes Unicode subjects/senders, and
records every notified `(account, UID)` in `/data/notified.db` so nothing is
ever notified twice. First run only starts watching mail that arrives after
deployment (no flood of old mail). Reconnects use exponential backoff.

## Setup

**1. Push to GitHub.** The image is built in CI, not on the VPS. On every push
to `main` (or a `v*` tag), `.github/workflows/build.yml` builds
`watcher/Dockerfile` and pushes `ghcr.io/music47ell/mail2ntfy:latest` to GHCR.

```powershell
git add .
git status                 # confirm no .env / *.db before committing
git commit -m "..."
git push
```

**2. Make the image pullable.** The GHCR package is private by default, so
either:

- set it public: GitHub → your profile → **Packages** → `mail2ntfy` →
  **Package settings** → **Change visibility** → Public; or
- add GHCR credentials (a PAT with `read:packages`) in Dockhand under
  **Settings → Registries**.

Wait for the **Actions** tab to show a green build before deploying.

**3. Prepare the state directory once on the VPS** (watcher runs as UID 1000):

```sh
sudo mkdir -p /opt/docker/data/mail2ntfy && sudo chown -R 1000:1000 /opt/docker/data/mail2ntfy
```

**4. Create an ntfy token** (only if access control is on — check
`grep -E '^auth' /opt/docker/data/ntfy/server.yml`; if it prints nothing,
leave `NTFY_TOKEN` empty):

```sh
docker exec -it ntfy ntfy token add <your-username>
docker exec -it ntfy ntfy access <your-username> allow email rw
```

**5. Deploy in Dockhand:** import this git repo → branch `main` → compose file
`compose.yaml`. Dockhand pulls the prebuilt image from GHCR; nothing is built
on the VPS. Set these stack environment variables, then Deploy:

```
NTFY_URL=http://ntfy:80
NTFY_TOPIC=email
NTFY_TOKEN=<from step 4>
GMAIL_USER=            GMAIL_PASSWORD=<Gmail App Password, not your password>
IMAP_NAME=           IMAP_USER=         IMAP_PASSWORD=
```

Only accounts with both `USER` and `PASSWORD` set are monitored; leave the
other blank. `NAME` is the label shown in the notification. See `.env.example`
for all options (poll/IDLE timing, log level, reconnect bounds).

## Check it works

```sh
docker ps --filter name=mail2ntfy
docker logs -f mail2ntfy        # expect: configured mailboxes / [Gmail] connected / ...
```

Send yourself a test email with a Unicode subject → push arrives within ~1s and
logs show `[Gmail] server announced new mail (IDLE)` + `[Gmail] new email from
...` + `notification sent`. Resend an email you already received → no duplicate.

Notifications use the title `You've Got Mail` (prefixed with the 📧 tag) and a
plain-text body with `From:` and `To:` lines. To change this, edit the payload
in `send_ntfy()` and the `send_ntfy(...)` call in `_poll_once()`.

To update the watcher: push to `main`, wait for the Actions build, then
redeploy (or "Re-pull images") in Dockhand. `compose.yaml` sets
`pull_policy: always`, so a redeploy always picks up the freshly built image.

## Troubleshooting

- **`cannot open database /data/notified.db`** → run the `chown` from Setup step 3.
- **Gmail `LOGIN failed`** → enable IMAP in Gmail settings and use an App
  Password (https://myaccount.google.com/apppasswords).
- **IMAP `LOGIN failed`** → wrong mailbox address/password.
- **ntfy `401/403`** → wrong `NTFY_TOKEN`, or token's user can't publish:
  `docker exec -it ntfy ntfy access <username> allow <topic> rw`.
- **`cannot reach ntfy at http://ntfy:80`** → both containers must be on the
  same network: `docker network inspect tunnel-net`.
- **Container restarts / no logs** → no mailboxes configured (`GMAIL_USER`/
  `GMAIL_PASSWORD` and `IMAP_USER`/`IMAP_PASSWORD` all empty); check the
  Dockhand env vars were applied.
- **`connected` but notifications are slow** → the server may not advertise
  IDLE; the log will say `server does not advertise IDLE; polling every ...`.
  Gmail and IMAP both support IDLE, so this usually means a proxy/firewall is
  stripping capabilities.
- **Notifications stop after a while but `connected` is logged** → the IDLE
  session may have been silently dropped; the watcher renews IDLE and polls
  every `IDLE_TIMEOUT` seconds, so mail still arrives within that window. Lower
  `IDLE_TIMEOUT` (e.g. 60) if you want faster recovery.
- **`pull access denied` / `manifest unknown` on deploy** → the GHCR package is
  private or the CI build hasn't finished. Make the package public (Setup step
  2) or add GHCR credentials in Dockhand, and confirm the Actions build is green.
- **`mkdir /home/dockhand/.docker: read-only file system`** → Dockhand's own
  container has a read-only `$HOME`. This stack only pulls, so it shouldn't
  build; if it does, set `DOCKER_CONFIG=/app/data/.docker` on the Dockhand
  container and restart it.
- **Reset state** (stop notifications permanently for old mail or fix a
  mistake): `sudo rm -f /opt/docker/data/mail2ntfy/notified.db*`, then
  restart — it re-baselines without spamming old mail.

## Security

No `ports:`, no host networking, no public endpoints. Credentials/token come
only from environment variables and are never logged or committed. Email
bodies are never fetched; only the From/Subject headers needed for the
notification (via `BODY.PEEK`, so mail stays unread).
