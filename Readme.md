# Otterball v2 🦦🏆
Thank you clanker for the Readme <3

Otterball v2 is a highly performant, dockerized sports prediction platform (e.g., for the FIFA World Cup) that seamlessly connects an interactive Django web dashboard with an advanced Discord bot. 

The system utilizes native Discord polls for prediction submissions, manages automated match day threads, calculates dynamic leaderboards using the official Standard Competition Ranking (1-2-2-4 ranking method), and processes data-intensive background tasks asynchronously within a cluster.

## ⚡ Core Features

- **Automated Match Day Polls:** Automatically generates weekly or match-day-specific Discord threads, including native polls for upcoming fixtures. Poll durations are precisely capped to close exactly at kickoff time.
- **True Sports Leaderboard (Standard Competition Ranking):** Computes rankings mathematically. If two players share 1st place, 2nd place is skipped, and the next player lands directly on 3rd place. The formatting prevents breaking Discord's character limits via dynamic *Pleb-Splitting*.
- **Real-Time Reconciliation:** Asynchronous workers synchronize submitted poll votes directly with the PostgreSQL database immediately after a poll closes, ensuring a tamper-proof pipeline.
- **Garbage Removal System:** Automatically cleans up ticker channels and threads by removing annoying, Discord-generated system messages (*"The poll results are in!"*) both in real-time and via a historical boot sweep.
- **Modern Deployment:** Ultra-fast multi-stage Docker builds leveraging the modern `uv` package manager and BuildKit caching.

## 🛠️ Tech Stack

- **Backend Runtime:** Python 3.14-slim
- **Web Framework:** Django 6.0+ (including WhiteNoise for optimized static asset delivery in production mode)
- **ASGI/WSGI Server:** Gunicorn
- **Asynchronous Tasks & Scheduler:** Celery & Celery Beat
- **Database:** PostgreSQL 18-alpine (optimized for the new cluster layout to allow seamless major upgrades)
- **In-Memory Cache & Broker:** Valkey 8-alpine (a fully compatible, modern open-source Redis successor)
- **Bot Library:** `discord.py` (fully asynchronous operation via Django's Async-ORM)
- **Package Management:** `uv` by Astral

---

## 🚀 Production Deployment (Quick Start)

Since the package is public, your production server can pull the pre-built image directly from the GitHub Container Registry (GHCR) without needing any authentication.

### 1. Prerequisites
Ensure Docker and the Docker Compose plugin are installed on your host system:
```bash
sudo apt-get update && sudo apt-get install docker-compose-plugin
```

### 2. Setup Environment Variables (`.env`)
Create a `.env` file in your deployment directory.

*⚠️ IMPORTANT: If your `DJANGO_SECRET_KEY` or your Discord token contains special characters like `#` (comment parser) or `$` (variable parser), you must wrap the key in single quotes (`'...'`)!*

```env
# Django Settings
DEBUG=False
ENV=production
DJANGO_SECRET_KEY='your-secret-key-containing-#-or-$'
ALLOWED_HOSTS=your-domain.com,127.0.0.1

# PostgreSQL 18 Configuration
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_secure_database_password
POSTGRES_DB=otterball_db
TZ=Europe/Berlin

# Discord API
DISCORD_BOT_TOKEN=your_discord_bot_token

# Celery & Cache
CELERY_BROKER_URL=redis://valkey:6379/0
CELERY_RESULT_BACKEND=redis://valkey:6379/0
```

### 3. Create the Directories & `compose.yml`
Before launching the containers, create the host directory for persistent assets like team logos:
```bash
mkdir -p media
```

Create a `compose.yml` file next to your `.env` pointing to the official GHCR image:

```yaml
services:
  web:
    image: ghcr.io/blackilli/otterball_v2:latest
    restart: unless-stopped
    env_file: .env
    ports:
      - "127.0.0.1:8000:8000"
    volumes:
      - ./media:/app/media
    depends_on:
      - db
      - valkey

  bot:
    image: ghcr.io/blackilli/otterball_v2:latest
    restart: unless-stopped
    env_file: .env
    command: uv run python manage.py runbot
    volumes:
      - ./media:/app/media
    depends_on:
      - db
      - valkey

  worker:
    image: ghcr.io/blackilli/otterball_v2:latest
    restart: unless-stopped
    env_file: .env
    command: uv run celery -A otterball_v2 worker --loglevel=info
    volumes:
      - ./media:/app/media
    depends_on:
      - db
      - valkey

  beat:
    image: ghcr.io/blackilli/otterball_v2:latest
    restart: unless-stopped
    env_file: .env
    command: uv run celery -A otterball_v2 beat --loglevel=info
    volumes:
      - ./media:/app/media
    depends_on:
      - db
      - valkey

  db:
    image: postgres:18-alpine
    restart: unless-stopped
    env_file: .env
    volumes:
      - postgres_data:/var/lib/postgresql/data

  valkey:
    image: valkey/valkey:8-alpine
    restart: unless-stopped

volumes:
  postgres_data:
```

### 4. Configure Production Nginx
For maximum performance, configure the host's Nginx proxy to bypass Django and serve the persistent `media/` folder (team logos) directly:

```nginx
location /media/ {
    alias /path/to/your/Otterball_v2/media/;
    expires 30d;
}
```

### 5. Pull and Start the Application
Execute the following commands to pull the latest image and boot up the cluster:
```bash
sudo docker compose pull
sudo docker compose up -d
```

### 6. Updates & Continuous Deployment
Whenever the GitHub Actions pipeline finishes baking a new version, update your live container stack by running:
```bash
sudo docker compose pull && sudo docker compose up -d
```

---

## 🛠️ Local Development (Alternative Build)

If you want to modify the source code locally and build the image yourself instead of using GHCR:

1. Clone the repository: `git clone https://github.com/blackilli/Otterball_v2.git`
2. Change the `image: ghcr.io/blackilli/otterball_v2:latest` lines in `compose.yml` to `build: .`
3. Run the BuildKit compiler locally:
```bash
sudo docker compose up -d --build
```

---

## 📦 CI/CD Pipeline (GitHub Actions)

The project includes an optimized GitHub Actions pipeline (`.github/workflows/build-image.yml`) that automatically triggers on every push or pull request to the `main` branch.

- **Registry:** GitHub Container Registry (`ghcr.io/blackilli/otterball_v2`)
- **Caching:** Utilizes the native GitHub Actions cache backend (`type=gha`), ensuring that unchanged layers and the `uv` cache are reused. Subsequent builds typically complete in under 10 seconds.
- **Tags:** Every image is tagged with the short Git commit SHA, and builds on the default branch automatically receive the `latest` tag.
- **Manual Trigger:** Enabled via `workflow_dispatch`, allowing you to manually force a build via the GitHub Web UI or GitHub CLI (`gh workflow run`) at any time.

