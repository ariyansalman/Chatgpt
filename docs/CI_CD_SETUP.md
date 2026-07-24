# CI/CD Setup (GitHub Actions)

Workflow file: `.github/workflows/ci-cd.yml`

## What runs, and when

| Job      | Trigger                                   | What it does                                                   |
|----------|--------------------------------------------|------------------------------------------------------------------|
| `test`   | Every push (any branch) + PRs into `main`  | Installs deps, runs `pytest tests/` on an in-memory SQLite DB    |
| `lint`   | Every push (any branch) + PRs into `main`  | `flake8` — hard-fails on syntax errors / undefined names, full style report is non-blocking |
| `deploy` | Push directly to `main` (i.e. a merge), only if `test` + `lint` both passed | SSHes into the VPS and re-deploys via `docker compose` |

The `deploy` job never runs on pull requests or feature branches — only on
`main`, so nothing reaches production until it's merged.

## One-time setup

### 1. Repo secrets

Go to **Settings → Secrets and variables → Actions** on GitHub and add:

| Secret              | Example                          | Notes                                              |
|---------------------|-----------------------------------|-----------------------------------------------------|
| `VPS_HOST`           | `203.0.113.10`                   | VPS IP or hostname                                 |
| `VPS_USER`           | `deploy`                         | A **non-root** user with docker permissions is recommended |
| `VPS_SSH_KEY`        | *(paste the private key)*        | Private half of a dedicated deploy keypair (see below) |
| `VPS_PORT`           | `22`                              | Optional — defaults to `22` if unset               |
| `VPS_DEPLOY_PATH`    | `/home/deploy/telegram-store-bot`| Absolute path to the git checkout on the VPS       |

### 2. Generate a dedicated deploy keypair (don't reuse your personal SSH key)

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f deploy_key -N ""
```

- Copy `deploy_key.pub` into `~/.ssh/authorized_keys` for `VPS_USER` on the VPS.
- Paste the *contents* of `deploy_key` (the private key) into the `VPS_SSH_KEY` secret.
- Delete the local key files once both are stored.

### 3. VPS prerequisites

- Docker + Docker Compose v2 installed.
- A clean `git clone` of this repo already sitting at `VPS_DEPLOY_PATH`,
  with its own `.env` file already configured there (the workflow never
  copies secrets/`.env` — it only does `git reset --hard` + `docker compose
  up -d --build` against what's already on disk).
- `VPS_USER` must be able to run `docker` / `docker compose` without `sudo`
  (add them to the `docker` group: `sudo usermod -aG docker $VPS_USER`).

### 4. (Optional) Require manual approval before deploying

The `deploy` job targets a GitHub **Environment** named `production`. If you
add that environment under **Settings → Environments** and enable
"Required reviewers", every deploy will pause for a manual approval click
before it touches the VPS — useful once the bot is handling real traffic.

## Running it locally before pushing

```bash
cd bot_src
pip install -r requirements.txt -r requirements-dev.txt
BOT_TOKEN=test:dummy ADMIN_TELEGRAM_ID=1 DATABASE_URL=sqlite:///:memory: pytest tests/ -v
flake8 . --select=E9,F63,F7,F82
```
