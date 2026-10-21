#!/usr/bin/env bash
# Docker's official apt repository. Does not remove/replace another Docker installation.
set -euo pipefail
if command -v docker >/dev/null 2>&1; then
  echo 'Docker уже установлен. Его версия и данные не изменялись.'
  exit 0
fi
. /etc/os-release
if [[ "${ID:-}" != ubuntu ]]; then
  echo 'Этот помощник рассчитан на Ubuntu. Установи Docker и Compose по docs.docker.com.'
  exit 1
fi
if [[ "${1:-}" != --yes ]]; then
  read -r -p 'Установить Docker Engine и Compose из официального apt-репозитория Docker? [y/N] ' answer
  [[ "$answer" == y || "$answer" == Y ]] || exit 0
fi
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
arch=$(dpkg --print-architecture)
codename=${UBUNTU_CODENAME:-$VERSION_CODENAME}
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $codename
Components: stable
Architectures: $arch
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker info >/dev/null
sudo docker compose version
printf '\nDocker установлен. Вернись к ./install.sh без sudo.\n'
