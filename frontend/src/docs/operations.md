# Operations

Operational notes for running QoderGate.

## SQLite Storage

QoderGate stores runtime data in:

```text
~/.qoder/qoder2api.db
```

Set `QODER_DATA_DIR` to relocate it to `$QODER_DATA_DIR/qoder2api.db` (`/data/qoder2api.db` in the container).

The database contains accounts, allowed API keys, and settings.

## Running with Docker

The image runs as a non-root user (uid 10001) and keeps its data volume at `/data`:

```bash
docker run -d --name qodergate \
  -p 5050:5050 \
  -v qodergate-data:/data \
  -e QODER_ADMIN_PASSWORD=your-strong-password \
  ghcr.io/wu-hins/qodergateway:latest
```

```bash
docker logs -f qodergate
docker inspect --format '{{.State.Health.Status}}' qodergate
```

## Proxy

Outbound proxying is controlled **only** by `QODER_PROXY`; `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` are ignored so behaviour stays predictable.

It is required to reach `qoder.sh` from mainland China, and applies to chat, auth, token refresh, quota checks, the registrar browser, and the temp-mail API.

```bash
-e QODER_PROXY=http://host.docker.internal:7890
```

On Linux add `--add-host=host.docker.internal:host-gateway`.

## Temp Mail

The registrar needs a mailbox backend for verification codes. **Configure it in the console**:
Register tab → "Temp mail configuration". Saving takes effect immediately, no restart needed.
Settings are stored in the local SQLite database (under `/data`, persisted with the volume).

- Pick the backend from the dropdown: `auto` / `cloudflare` / `yyds`
- Secret fields (admin password, site password, token) are shown masked and never returned in
  plaintext; leaving them blank keeps the current value
- After saving, "Test" creates a real mailbox to confirm the configuration works
- The `CF_TEMP_EMAIL_*` / `YYDS_API_KEY` environment variables still work as first-deploy
  defaults; values saved in the console take precedence

Self-hosted [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email)
is recommended: with `ADMIN_PASSWORDS` set in its `wrangler.toml`, this project uses
`/admin/new_address`, bypassing Turnstile and the anonymous-creation restriction. If the site
enables `SITE_PASSWORD`, fill it into "Site password".

## Running the Registrar in a Container

Qoder's CAPTCHA is an Alibaba Cloud slider that **must be dragged by a human** and cannot be solved headless. This project embeds the registrar browser's live view directly into the Web console:

```bash
docker run -d --name qodergate \
  -p 5050:5050 \
  --shm-size=1g \
  -v qodergate-data:/data \
  -e QODER_ADMIN_PASSWORD=your-strong-password \
  ghcr.io/wu-hins/qodergateway:latest
```

Start the registrar from the **Register** tab. A "Remote Browser" panel appears — drag the slider right there. **No VNC, no extra port.**

Implementation: it reuses DrissionPage's existing CDP debug channel, streaming `Page.captureScreenshot` frames over a WebSocket to a frontend canvas and replaying input via `Input.dispatch*`. Xvfb still runs (headed Chromium needs an X display), but **VNC is neither installed nor exposed**.

- Tune with `QODER_REMOTE_BROWSER_FPS` (default 8) and `QODER_REMOTE_BROWSER_QUALITY` (default 60)
- Disable via `QODER_REMOTE_BROWSER=0`
- **`--shm-size=1g` is required**: the default 64MB `/dev/shm` crashes Chromium renderers
- Chromium runs with `--no-sandbox` (containers usually lack `CAP_SYS_ADMIN`)

> **Security**: the remote browser hands full control of that browser to the console user. Change the default password and never expose the console publicly.

## Data Volume Ownership

The image declares `VOLUME /data`. Docker creates a new volume owned by `root`,
which overrides the build-time `chown` and leaves a non-root user unable to write
the database (`sqlite3.OperationalError: unable to open database file`).

The container therefore **starts as root**, and the entrypoint first assigns
`$QODER_DATA_DIR` to the runtime user (uid/gid 10001 by default) before dropping
privileges via `setpriv` and re-executing itself. Xvfb, the window manager and the
gateway all run as non-root afterwards. Named volumes need no extra steps.

With a bind mount (`-v /host/path:/data`) the host directory keeps its owner; set it first:

```bash
sudo chown -R 10001:10001 /host/path
```

Override the runtime identity with `QODER_UID` / `QODER_GID`.

## Backup

Stop the server and copy the database file:

```bash
cp ~/.qoder/qoder2api.db ~/qoder2api.db.backup

# from the qodergate-data volume
docker run --rm -v qodergate-data:/data -v "$PWD:/backup" alpine \
  cp /data/qoder2api.db /backup/qoder2api.db.backup
```

## Reset the Gateway Token

The gateway token lives in the `settings` table under `gateway_token`.

Note: when `QODER_ADMIN_PASSWORD` is set it **overrides the database value**, so changing the password in the console has no effect. Remove the variable if you want to manage it from the WebUI.

## Token Refresh

All enabled accounts are refreshed every 6 hours. Failures are printed to the service log (`[tokens] scheduled refresh: ...`). If a `dt-` token expires and refresh keeps failing, that account will keep returning 401/403 and trigger rotation — check `QODER_PROXY`.

## Troubleshooting

### 401 Unauthorized

- WebUI route: check `X-Gateway-Token`.
- API route: check `Authorization: Bearer <key>`.

### No Active Session

Import an account or add a PAT from the Dashboard.

### Account Quota Exceeded

Disable the exhausted account or import another account and let rotation continue.

### Container exits right after start

Check that frontend assets were built — the image build already runs a smoke test. If the build passed but the container still fails, a bind mount is probably shadowing `/app/src/qoder2api/static`.

### Local Auth Import Failed

Make sure Qoder CLI has been logged in on this machine and the local auth files exist. Containers have no access to the host `~/.qoder/.auth` — use a PAT or batch import instead.