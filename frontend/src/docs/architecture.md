# Architecture

QoderGate bridges OpenAI-compatible clients to Qoder sessions.

## Request Flow

```text
Client
  -> FastAPI /v1/chat/completions
  -> API key validation
  -> SQLite account router
  -> Outbound request (optional QODER_PROXY)
  -> Qoder upstream API
  -> OpenAI-compatible response
```

## Backend Components

| Module | Responsibility |
| --- | --- |
| `app.py` | FastAPI routes, UI auth, request routing and account rotation. |
| `accounts.py` | SQLite account CRUD and active session selection. |
| `auth.py` | PAT exchange, local auth import, user status query. |
| `bridge.py` | Upstream `/model/v1/chat/completions` calls plus OpenAI-compatible stream/non-stream conversion. |
| `mailbox.py` | Temp-mail abstraction: cloudflare_temp_email / yyds. |
| `registrar.py` | Registrar service: browser automation, CAPTCHA scheduling, account persistence. |
| `remotebrowser.py` | CDP frame bridge: streams the registrar browser to the console and replays input. |
| `tokens.py` | Token refresh, quota queries, and the 6-hour refresh thread. |
| `signature.py` | COSY signing constants (legacy protocol path). |
| `database.py` | SQLite schema and connection helpers. |
| `config.py` | Gateway configuration and API key storage. |
| `env.py` | Environment loading plus proxy/temp-mail configuration. |
| `encoding.py` | Upstream custom base64 encoding (unused by the current protocol). |

## Outbound Networking

Every outbound request is built through `env.httpx_client_kwargs()`. Proxying is decided **only** by `QODER_PROXY`; `HTTP_PROXY` and friends are ignored.

## Frontend Components

The WebUI is built with Vite, React, Tailwind CSS, GSAP, and Markdown rendering.

It is compiled into:

```text
src/qoder2api/static
```

FastAPI serves the compiled `index.html`, `console.html`, `docs.html` and static assets directly.

## Container

The `Dockerfile` is a multi-stage build: a Node stage compiles the frontend, and a Python stage installs the gateway and copies the assets in. A build-time smoke test asserts the `StaticFiles` mount resolves.

The image bundles Chromium and Xvfb for the registrar. The CAPTCHA is solved by hand in a headed browser, by default through the CDP frame bridge in `remotebrowser.py` directly inside the Web console (no VNC); VNC is neither installed nor exposed.

The GitHub Actions workflow builds on native amd64 and arm64 runners, then merges both into a multi-arch manifest pushed to GHCR.