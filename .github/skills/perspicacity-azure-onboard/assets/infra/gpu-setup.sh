#!/bin/bash
set -Eeuo pipefail
trap 'echo "GPU setup failed; inspect /var/log/cloud-init-output.log" >&2' ERR
export DEBIAN_FRONTEND=noninteractive
apt-get install -y "linux-headers-$(uname -r)"
installer=$(mktemp /var/tmp/nvidia-grid-XXXXXX.run)
trap 'rm -f "$installer"' EXIT
curl --fail --location --retry 3 --proto '=https' --tlsv1.2 \
  'https://download.microsoft.com/download/a7cb6d36-3bbc-43d6-9e88-e0842e6f9ab9/NVIDIA-Linux-x86_64-595.91.07-grid-azure.run' \
  --output "$installer"
sh "$installer" --silent --dkms -M open
nvidia-smi
