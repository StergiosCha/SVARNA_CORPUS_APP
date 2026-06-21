#!/bin/bash
# =============================================================================
# Upload literature.db and dialectal.db to Azure File Share
# Uses azcopy for large files (progress bar, resume on failure, no timeouts)
# corpus.db is already there from the original deployment
# =============================================================================
set -e

RESOURCE_GROUP="greek-app-heaven-rg"
STORAGE_ACCOUNT="greekcoporastorage"
FILE_SHARE_NAME="corpus-db"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIT_DIR="$SCRIPT_DIR/../greek_literature"

echo "=== Upload New Databases to Azure File Share ==="
echo ""

# ---- Check azcopy ----
if ! command -v azcopy &>/dev/null; then
  echo "azcopy not found. Installing via Homebrew..."
  brew install azcopy
fi

# ---- Generate SAS token (valid 2 hours) ----
echo "[1/4] Generating SAS token..."
STORAGE_KEY=$(az storage account keys list \
  --account-name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --query "[0].value" -o tsv)

EXPIRY=$(date -u -v+2H +"%Y-%m-%dT%H:%MZ" 2>/dev/null || date -u -d "+2 hours" +"%Y-%m-%dT%H:%MZ")

SAS=$(az storage account generate-sas \
  --account-name "$STORAGE_ACCOUNT" \
  --account-key "$STORAGE_KEY" \
  --services f \
  --resource-types sco \
  --permissions rwdlacup \
  --expiry "$EXPIRY" \
  --output tsv)

BASE_URL="https://${STORAGE_ACCOUNT}.file.core.windows.net/${FILE_SHARE_NAME}"

# ---- Upload literature.db ----
echo "[2/4] Uploading literature.db (723 MB)..."
if [ -f "$LIT_DIR/literature.db" ]; then
  azcopy copy \
    "$LIT_DIR/literature.db" \
    "${BASE_URL}/literature.db?${SAS}" \
    --overwrite=true \
    --cap-mbps 0
  echo "  literature.db done."
else
  echo "  ERROR: $LIT_DIR/literature.db not found!"
  exit 1
fi

# ---- Upload dialectal.db ----
echo "[3/4] Uploading dialectal.db (161 MB)..."
if [ -f "$LIT_DIR/dialectal.db" ]; then
  azcopy copy \
    "$LIT_DIR/dialectal.db" \
    "${BASE_URL}/dialectal.db?${SAS}" \
    --overwrite=true \
    --cap-mbps 0
  echo "  dialectal.db done."
else
  echo "  ERROR: $LIT_DIR/dialectal.db not found!"
  exit 1
fi

# ---- Verify ----
echo "[4/4] Verifying file share contents..."
az storage file list \
  --share-name "$FILE_SHARE_NAME" \
  --account-name "$STORAGE_ACCOUNT" \
  --account-key "$STORAGE_KEY" \
  --output table

echo ""
echo "=== Upload Complete ==="
