#!/bin/bash
# =============================================================================
# Build Greek Corpus FTS5 Index on Azure
# =============================================================================
# Nothing runs on your Mac except this script (which just talks to Azure API).
#
# What happens:
#   1. Uploads build_index.py to Azure Blob Storage
#   2. Creates an Azure File Share for the output corpus.db
#   3. Spins up a temporary Azure Container Instance (4 CPU, 8 GB RAM)
#   4. The container downloads corpora + build script from Blob,
#      builds the FTS5 index, writes corpus.db to File Share
#   5. You delete the container when done
#
# Prerequisites:
#   - az cli logged in: az login
#   - Corpora uploaded to Blob via upload_to_azure.sh
#
# Cost: ~$0.05/hour. Build ~30-60 min. Total under $0.10.
# =============================================================================

set -e

RESOURCE_GROUP="greek-app-heaven-rg"
STORAGE_ACCOUNT="greekcoporastorage"
BLOB_CONTAINER="greek-corpora"
FILE_SHARE_NAME="corpus-db"
ACI_NAME="corpus-index-builder"
LOCATION="westeurope"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Greek Corpus Index Builder (Azure) ==="
echo ""

# ---- Step 1: Upload build_index.py to Blob ----
echo "[1/5] Uploading build_index.py to Blob Storage..."
CONN_STR=$(az storage account show-connection-string \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --output tsv)

az storage blob upload \
  --container-name "$BLOB_CONTAINER" \
  --file "$SCRIPT_DIR/build_index.py" \
  --name "scripts/build_index.py" \
  --connection-string "$CONN_STR" \
  --overwrite true \
  --output none

# ---- Step 2: Create File Share for corpus.db ----
echo "[2/5] Creating Azure File Share '$FILE_SHARE_NAME'..."
az storage share-rm create \
  --storage-account "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --name "$FILE_SHARE_NAME" \
  --quota 20 \
  --output none \
  2>/dev/null || echo "  (already exists)"

STORAGE_KEY=$(az storage account keys list \
  --account-name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --query "[0].value" -o tsv)

# ---- Step 3: Generate the build script ----
# This script runs INSIDE the container
echo "[3/5] Preparing container build script..."

BUILD_SCRIPT='#!/bin/bash
set -e
echo "=== Index Build Started ==="
echo "Time: $(date -u)"
echo ""

echo "[1/4] Installing Python..."
apt-get update -qq && apt-get install -y -qq python3 > /dev/null 2>&1
echo "  Python ready: $(python3 --version)"

echo "[2/4] Downloading corpora from Blob..."
mkdir -p /data/greek_corpora

for corpus in parliament/cleaned/dataset_versions wikipedia opensubtitles leipzig europarl universal_dependencies cc100 opus/CCAligned opus/EUbookshop opus/ParaCrawl opus/WikiMatrix; do
  target_dir="/data/greek_corpora/$corpus"
  mkdir -p "$target_dir"
  echo "  Downloading $corpus..."
  az storage blob download-batch \
    --destination "$target_dir" \
    --source '"$BLOB_CONTAINER"' \
    --pattern "$corpus/*" \
    --connection-string "$AZURE_STORAGE_CONNECTION_STRING" \
    --output none 2>/dev/null || echo "  [WARN] $corpus: partial download"
done

echo ""
echo "  Downloaded:"
du -sh /data/greek_corpora/*/ 2>/dev/null || true

echo ""
echo "[3/4] Downloading build script..."
az storage blob download \
  --container-name '"$BLOB_CONTAINER"' \
  --name "scripts/build_index.py" \
  --file /data/build_index.py \
  --connection-string "$AZURE_STORAGE_CONNECTION_STRING" \
  --output none

echo "[4/4] Building FTS5 index (this takes 30-60 minutes)..."
python3 /data/build_index.py \
  --data-dir /data/greek_corpora \
  --output /output/corpus.db

echo ""
echo "=== BUILD COMPLETE ==="
ls -lh /output/corpus.db
echo "Time: $(date -u)"
echo ""
echo "You can now delete this container with:"
echo "  az container delete -g '"$RESOURCE_GROUP"' -n '"$ACI_NAME"' --yes"
'

# ---- Step 4: Create the ACI ----
echo "[4/5] Creating Azure Container Instance..."
echo "  Image:  mcr.microsoft.com/azure-cli:latest"
echo "  CPU:    4 cores"
echo "  RAM:    8 GB"
echo "  Mount:  File Share '$FILE_SHARE_NAME' at /output"
echo ""

az container create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ACI_NAME" \
  --location "$LOCATION" \
  --image mcr.microsoft.com/azure-cli:latest \
  --cpu 4 \
  --memory 16 \
  --restart-policy Never \
  --azure-file-volume-share-name "$FILE_SHARE_NAME" \
  --azure-file-volume-account-name "$STORAGE_ACCOUNT" \
  --azure-file-volume-account-key "$STORAGE_KEY" \
  --azure-file-volume-mount-path /output \
  --environment-variables \
    AZURE_STORAGE_CONNECTION_STRING="$CONN_STR" \
  --command-line "/bin/bash -c '$BUILD_SCRIPT'" \
  --output none

echo "[5/5] Container is running!"
echo ""
echo "=========================================="
echo " MONITOR PROGRESS"
echo "=========================================="
echo ""
echo "  Follow logs:"
echo "    az container logs -g $RESOURCE_GROUP -n $ACI_NAME --follow"
echo ""
echo "  Check status:"
echo "    az container show -g $RESOURCE_GROUP -n $ACI_NAME --query instanceView.state -o tsv"
echo ""
echo "  When status is 'Terminated' and logs show 'BUILD COMPLETE':"
echo ""
echo "  Verify the file:"
echo "    az storage file list --share-name $FILE_SHARE_NAME --account-name $STORAGE_ACCOUNT --account-key \"$STORAGE_KEY\" -o table"
echo ""
echo "  Clean up (stop billing):"
echo "    az container delete -g $RESOURCE_GROUP -n $ACI_NAME --yes"
echo ""
echo "  The corpus.db is now on Azure File Share '$FILE_SHARE_NAME',"
echo "  ready to mount into the Greek Corpus Workbench Container App."
