# Course Watch

A UMass Amherst course-opening notifier with verified `@umass.edu` accounts, private watchlists, and email alerts. The MVP monitors **Spring 2027 COMPSCI** courses. Python/FastAPI handles accounts and collection, PostgreSQL stores state and the email outbox, and Next.js provides the dashboard.

## Run locally

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22+, Docker with Compose.

```sh
cp .env.example .env
# Replace AUTH_SECRET in .env with a random value.
uv run python -c 'import secrets; print(secrets.token_hex(32))'
docker compose up -d
uv sync --frozen
uv run python -m coursewatch.db
npm --prefix web ci
```

Run these in three terminals from the project root:

```sh
uv run uvicorn coursewatch.api:app --host 127.0.0.1 --port 8000
```

```sh
uv run python -m coursewatch.worker
```

```sh
npm --prefix web run dev
```

Open http://localhost:3000. Development email goes to **Mailpit at http://localhost:8025**, not a real recipient. Enter an `@umass.edu` address, retrieve the six-digit code from Mailpit, and verify it. Real inbox verification requires configuring a sending provider.

Add a course number such as `220`, optionally with a five-digit class number. A blank class number watches any supported combination. The class number may identify a lecture, lab, or discussion: the worker still checks the complete required combination. Each account can have 20 watches.

On the initial development machine, Docker was unavailable, so the local environment uses an isolated native PostgreSQL cluster on `127.0.0.1:55432` and a Mailpit binary under ignored `.local/`. Its `.env` points to that database. To restart those existing services:

```sh
pg_ctl -D .local/pg -l .local/postgres.log -o '-p 55432 -h 127.0.0.1' start
.local/mailpit --listen 127.0.0.1:8025 --smtp 127.0.0.1:1025 --database .local/mailpit.db --disable-version-check
```

## Availability and alerts

- Collection uses SPIRE's public guest form with cookies and hidden PeopleSoft state. It does not use a student password or enroll anyone.
- A watched course is fetched once for all subscribers. Published courses are checked about every five minutes, with at least three seconds between HTTP requests. A complete check may take longer when many sections are present.
- As of the October 10, 2026 live check, `2027 Spring` was absent from the guest term selector. Such watches remain queued with **Term not published**, and the worker checks again hourly. Missing courses also retry hourly.
- The first successful observation establishes a baseline without alerting for already-open seats. A later newly open combination produces an alert. A combination can alert again after it closes and reopens; opening a different compatible lab also counts as a new combination.
- Each required section must report `Open` and a positive available-seat count. Waitlists and closed sections do not trigger alerts.
- **Pairings are inferred from unambiguous section-label prefixes within the same session**, for example `01-LEC` with `01LL-LAB`. The public guest interface does not expose authoritative enrollment associations. Emails and the dashboard explicitly instruct you to verify compatibility in SPIRE. Ambiguous or unsupported pairings are omitted and reported; unrelated sessions and known time conflicts are excluded. Unknown meeting times are labeled.
- Prerequisites, major restrictions, reserved seats, appointments, and holds can prevent enrollment. Alerts include observed seats, class numbers, restrictions, timestamps, and a SPIRE link.
- Failed requests, expired sessions, unexpected HTML, and incomplete seat counts never become “closed” observations. The last successful snapshot and alert baseline remain intact, with exponential retry backoff up to one hour. Each new course check opens a fresh guest session.

Email uses a durable PostgreSQL outbox. Delivery runs independently of slow collection, retries failures, and marks successful sends. PostgreSQL advisory locks prevent duplicate polling and concurrent email dispatch across worker instances. The Resend integration supplies an idempotency key; SMTP remains at-least-once if the process crashes after a server accepts a message but before the database records success.

Verification codes expire in ten minutes, allow five attempts, and can be used once. Code requests are throttled per email and by the API-visible client address; behind the local frontend or production proxy that address may be shared. Code and session hashes are stored in the database, queued code bodies are cleared after successful delivery, and sessions expire in 30 days. Mutation requests require the configured `APP_ORIGIN`; session cookies are HttpOnly, SameSite=Lax, and Secure in production.

## Checks

```sh
uv run ruff check backend
uv run ruff format --check backend
# Integration tests create and remove isolated schemas in the specified database.
TEST_DATABASE_URL=postgresql+psycopg://coursewatch:coursewatch@localhost:5432/coursewatch uv run pytest -q
npm --prefix web run build
npm --prefix web run typecheck
npm --prefix web audit --omit=dev
```

Without `TEST_DATABASE_URL`, PostgreSQL integration tests are skipped. CI supplies a PostgreSQL service and runs the complete suite. Tests cover email verification and replay, account isolation, watch limits, parser failure handling, multi-component pairing, baseline/change detection, shared collection, worker locking, unpublished terms, and delivery retries. No browser automation or visual UI verification is used.

One-off source diagnostics:

```sh
uv run python -m coursewatch.spire 220
# A currently published term can validate the source independently of the Spring-only UI.
uv run python -m coursewatch.spire 220 --term '2026 Fall'
uv run python -m coursewatch.worker --once
```

A local untracked `DEMO.md`, generated with Showboat, records reproducible test commands.

## Deploy to your DigitalOcean VM

The existing 8 GB VM is the intended deployment target. The production stack keeps the Next.js web server, Python API, polling/email worker, and PostgreSQL running continuously. Caddy handles HTTPS and sends `/api/*` to the internal API. Only ports 80 and 443 are published. Cloudflare DNS can point to the VM; neither Cloudflare Pages nor GitHub Pages is needed to run this stack.

1. Install Docker and Compose on the VM and clone this private repository using your own GitHub credentials.
2. Point a domain's DNS to the VM and permit inbound ports 80/443. Create `.env.production` from `.env.production.example`. Set matching `APP_HOST` and HTTPS `APP_ORIGIN`, random `AUTH_SECRET` and hex `POSTGRES_PASSWORD`, and the email settings.
3. For real mail, use Resend with a verified domain and API key, or set `EMAIL_BACKEND=smtp` and configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_STARTTLS`, and credentials. No credentials are included in the repository.
4. Build and start:

```sh
docker compose --env-file .env.production -f compose.production.yaml up -d --build
docker compose --env-file .env.production -f compose.production.yaml logs -f worker
```

The one-shot `migrate` service initializes the schema before the API and worker start. It creates missing tables; it is not a schema migration framework. Introduce versioned migrations before altering deployed table structures.

Updates use the same `up -d --build` command. Database and Caddy data persist in named volumes. Back up PostgreSQL regularly and keep the backups off the VM:

```sh
mkdir -p .local/backups
docker compose --env-file .env.production -f compose.production.yaml exec -T db pg_dump -U coursewatch -Fc coursewatch > .local/backups/coursewatch.dump
```

The production Compose configuration has been validated. Container image builds and deployment on the VM have not been performed. The collector has passed a live Fall 2026 check; sustained operation and Spring 2027 behavior after publication still need observation. Telegram delivery is deferred.

### Existing Nginx droplet

The inspected droplet at `165.227.126.242` has **1 GB RAM**, runs existing applications behind Nginx, and now has 2 GB swap. Use `compose.vm.yaml` with the production file to disable the Caddy service, publish only localhost ports `3010` (web) and `8010` (API), limit memory, and rotate container logs. Existing Nginx sites remain separate.

Build images on GitHub's runner rather than on this small server:

```sh
gh workflow run images.yml --ref main
# Once that run completes, download its private coursewatch-images artifact.
gh run download RUN_ID --name coursewatch-images --dir .local/deployment-images
cd .local/deployment-images
shasum -a 256 -c images.sha256
```

The artifact contains the Linux images and `revision.txt`. Transfer the files to the VM, verify `images.sha256` there, and load `images.tar.gz` with `docker load`. Set `COURSEWATCH_VERSION` in the server's `.env.production` to the revision from the artifact, then:

```sh
cd /opt/coursewatch/app
docker compose --env-file .env.production -p coursewatch -f compose.production.yaml -f compose.vm.yaml up -d --no-build db migrate api worker web
```

The app/database secrets are generated locally and kept in ignored `.env.production` with mode 600; the server copy uses the same permissions. Set the real domain, email sender, and provider key before enabling public access. Render `deploy/nginx.conf.template` for the chosen host into a new Nginx site, add a publicly trusted TLS certificate, validate with `nginx -t`, and reload Nginx. The configuration template starts with HTTP for certificate bootstrap; production needs HTTPS because sessions use Secure cookies.

The Linux images have been successfully built in GitHub Actions. Public DNS, HTTPS, and real email delivery require the user's chosen domain and email-provider credentials.
