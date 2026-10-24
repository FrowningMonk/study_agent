#!/bin/sh
# Deploys the latest main on the VPS. Run by CI through a forced-command SSH key,
# or manually: sh /opt/study_agent/scripts/deploy.sh
set -eu

APP_DIR=/opt/study_agent
cd "$APP_DIR"

git fetch --quiet origin main
git reset --hard --quiet origin/main
echo "Deploying $(git log -1 --format='%h %s')"

# Container runs as uid 1000 and writes into these bind mounts
mkdir -p data/parsed_articles ideas_md conspect
chown -R 1000:1000 data ideas_md conspect

docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null

sleep 5
docker compose ps
docker compose logs --tail 20 bot
