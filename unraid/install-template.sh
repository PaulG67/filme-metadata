#!/bin/bash
# Install / update filme-metadata Unraid user template + pull image
# Run on Unraid Terminal:
# bash <(curl -fsSL https://raw.githubusercontent.com/PaulG67/filme-metadata/main/unraid/install-template.sh)

set -euo pipefail

TEMPLATE_DIR="/boot/config/plugins/dockerMan/templates-user"
USER_XML="${TEMPLATE_DIR}/my-filme-metadata.xml"
IMAGE="ghcr.io/paulg67/filme-metadata:latest"
RAW_BASE="https://raw.githubusercontent.com/PaulG67/filme-metadata/main"
SRC_DIR="/mnt/user/appdata/filme-metadata-src"
REPO="https://github.com/PaulG67/filme-metadata.git"

echo "==> 1/3 Unraid-Vorlage"
mkdir -p "${TEMPLATE_DIR}"
curl -fsSL -H "Cache-Control: no-cache" "${RAW_BASE}/unraid/my-filme-metadata.xml?$(date +%s)" -o "${USER_XML}"

if ! grep -q '<Repository>ghcr.io/paulg67/filme-metadata:latest</Repository>' "${USER_XML}"; then
  echo "FEHLER: Vorlage enthaelt kein Repository — Abbruch."
  exit 1
fi
if ! grep -q 'Name="Jellyfin URL"' "${USER_XML}"; then
  echo "FEHLER: Vorlage enthaelt keine Jellyfin-URL — Abbruch."
  exit 1
fi
if ! grep -q '</WebUI>' "${USER_XML}"; then
  echo "FEHLER: WebUI-Tag kaputt — Abbruch."
  exit 1
fi

echo "  ${USER_XML}"
wc -c "${USER_XML}" | awk '{print "  Groesse:" $1 " Bytes"}'

echo "==> 2/3 Docker-Image"
if docker pull "${IMAGE}" 2>/dev/null; then
  echo "  pulled ${IMAGE}"
else
  echo "  pull failed — building locally from GitHub"
  if command -v git >/dev/null 2>&1; then
    if [[ -d "${SRC_DIR}/.git" ]]; then
      git -C "${SRC_DIR}" pull --ff-only || true
    else
      rm -rf "${SRC_DIR}"
      git clone --depth 1 "${REPO}" "${SRC_DIR}"
    fi
  else
    mkdir -p "${SRC_DIR}"
    curl -fsSL "https://codeload.github.com/PaulG67/filme-metadata/tar.gz/refs/heads/main" \
      | tar -xz -C "${SRC_DIR}" --strip-components=1
  fi
  docker build -t "${IMAGE}" "${SRC_DIR}"
  echo "  tagged ${IMAGE}"
fi

echo "==> 3/3 Fertig"
echo
echo "Seite neu laden (F5), dann:"
echo "  Docker -> Container hinzufuegen -> Template filme-metadata"
echo "  Jellyfin-URL und Admin-API-Key setzen"
echo "  WebUI: http://UNRAID-IP:8792"
echo "  Nicht am Router nach aussen freigeben."
