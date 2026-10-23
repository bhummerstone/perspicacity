#!/bin/bash
set -Eeuo pipefail
trap 'echo "Docker setup failed; inspect /var/log/cloud-init-output.log" >&2' ERR
export DEBIAN_FRONTEND=noninteractive

source /etc/os-release
if [[ "$ID" != ubuntu || "$(dpkg --print-architecture)" != amd64 ]]; then
  echo "Docker bootstrap requires the selected Ubuntu amd64 image." >&2
  exit 1
fi

install -m 0755 -d /etc/apt/keyrings
curl --fail --location --retry 3 --proto '=https' --tlsv1.2 \
  https://download.docker.com/linux/ubuntu/gpg --output /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
systemctl is-active --quiet docker
docker --host unix:///var/run/docker.sock info --format '{{.ServerVersion}}'
docker buildx version
docker compose version
