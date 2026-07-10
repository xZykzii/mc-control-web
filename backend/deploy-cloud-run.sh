#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-project-e8d49f4b-11ae-4521-b23}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-mc-discord-control}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-mc-discord-bot}"
ZONE="${ZONE:-us-central1-c}"
INSTANCE="${INSTANCE:-minecraft-modpack-server}"

if [[ -z "${DISCORD_PUBLIC_KEY:-}" ]]; then
  echo "Set DISCORD_PUBLIC_KEY first." >&2
  exit 1
fi
if [[ -z "${NOTIFY_SECRET:-}" ]]; then
  echo "Set NOTIFY_SECRET first." >&2
  exit 1
fi
if [[ -z "${SESSION_SECRET:-}" ]]; then
  echo "Set SESSION_SECRET first (used to sign web login sessions, e.g. openssl rand -hex 32)." >&2
  exit 1
fi

gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com compute.googleapis.com

if ! gcloud iam service-accounts describe "${SERVICE_ACCOUNT}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SERVICE_ACCOUNT" \
    --display-name "Minecraft Discord controller"
fi

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${SERVICE_ACCOUNT}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role "roles/compute.instanceAdmin.v1" >/dev/null

gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --service-account "${SERVICE_ACCOUNT}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars "PROJECT_ID=${PROJECT_ID},ZONE=${ZONE},INSTANCE=${INSTANCE},DISCORD_PUBLIC_KEY=${DISCORD_PUBLIC_KEY},DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN:-},DISCORD_NOTIFY_CHANNEL_ID=${DISCORD_NOTIFY_CHANNEL_ID:-},NOTIFY_SECRET=${NOTIFY_SECRET},ALLOWED_ROLE_IDS=${ALLOWED_ROLE_IDS:-},ALLOWED_USER_IDS=${ALLOWED_USER_IDS:-},CUSTOM_DOMAIN=${CUSTOM_DOMAIN:-},MINECRAFT_PORT=25565,DISCORD_CLIENT_ID=${DISCORD_CLIENT_ID:-},DISCORD_CLIENT_SECRET=${DISCORD_CLIENT_SECRET:-},DISCORD_GUILD_ID=${DISCORD_GUILD_ID:-},DISCORD_REDIRECT_URI=${DISCORD_REDIRECT_URI:-},SESSION_SECRET=${SESSION_SECRET},WEB_ORIGIN=${WEB_ORIGIN:-}"

gcloud run services describe "$SERVICE" --region "$REGION" --format "value(status.url)"
