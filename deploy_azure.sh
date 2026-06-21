#!/bin/bash
# =============================================================================
# Deploy Greek Corpus Workbench to Azure Container Apps
# Multi-database version: corpus.db + literature.db + dialectal.db
# =============================================================================
set -e

RESOURCE_GROUP="greek-app-heaven-rg"
STORAGE_ACCOUNT="greekcoporastorage"
FILE_SHARE_NAME="corpus-db"
ACR_NAME="greekappheaven"
APP_NAME="greek-corpus-workbench"
ENV_NAME="greek-apps-env"
IMAGE_NAME="greek-corpus-workbench"
IMAGE_TAG="v$(date +%Y%m%d%H%M%S)"
FULL_IMAGE="$ACR_NAME.azurecr.io/$IMAGE_NAME:$IMAGE_TAG"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Deploy Greek Corpus Workbench (Multi-DB) ==="
echo "Image: $FULL_IMAGE"
echo ""

# ---- Step 1: Build and push Docker image to ACR ----
echo "[1/4] Building Docker image in ACR..."
cd "$SCRIPT_DIR"

az acr build \
  --registry "$ACR_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --image "$IMAGE_NAME:$IMAGE_TAG" \
  --file Dockerfile \
  --build-arg "CACHEBUST=$(date +%s)" \
  .

echo "  Build complete."

# ---- Step 2: Storage config ----
echo "[2/4] Configuring File Share mount..."
STORAGE_KEY=$(az storage account keys list \
  --account-name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --query "[0].value" -o tsv)

az containerapp env storage set \
  --name "$ENV_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --storage-name corpusdb \
  --azure-file-account-name "$STORAGE_ACCOUNT" \
  --azure-file-account-key "$STORAGE_KEY" \
  --azure-file-share-name "$FILE_SHARE_NAME" \
  --access-mode ReadWrite \
  --output none

# ---- Step 3: Update container with image + env vars ----
echo "[3/4] Updating container image + env vars..."
az containerapp update \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --image "$FULL_IMAGE" \
  --set-env-vars \
    "DB_PATH=/data/corpus.db" \
    "LITERATURE_DB_PATH=/data/literature.db" \
    "DIALECTAL_DB_PATH=/data/dialectal.db" \
  --output none

echo "  Image updated. Waiting 30s for new revision to start..."
sleep 30

# ---- Step 4: Verify ----
echo "[4/4] Verifying deployment..."
FQDN=$(az containerapp show \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.configuration.ingress.fqdn -o tsv)

echo ""
echo "=== Deployment Complete ==="
echo "App URL: https://$FQDN"
echo ""

# Check databases endpoint
echo "Checking /api/databases..."
curl -s "https://$FQDN/api/databases" | python3 -c "
import sys, json
d = json.load(sys.stdin)
dbs = d.get('databases', [])
for db in dbs:
    status = 'OK' if db.get('available') else 'MISSING'
    sents = db.get('sentences', '?')
    print(f\"  {db['name']}: {status} ({sents:,} sentences)\" if isinstance(sents, int) else f\"  {db['name']}: {status}\")
" || echo "WARNING: App may still be starting. Wait 1-2 min and refresh."
