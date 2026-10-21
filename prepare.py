"""Prepare local folders and .env; never installs software or modifies other services."""
import ipaddress
import os
from pathlib import Path

root = Path(__file__).resolve().parent
os.chdir(root)
for name in ('config/jackett', 'config/qbittorrent', 'config/jellyfin', 'cache/jellyfin', 'state', 'data/library', 'data/movies'):
    (root / name).mkdir(parents=True, exist_ok=True)
if not (root / '.env').exists():
    address = input('LAN IP мини-ПК [127.0.0.1]: ').strip() or '127.0.0.1'
    ipaddress.IPv4Address(address)
    text = (root / '.env.example').read_text().replace('PUID=1000', f'PUID={os.getuid()}').replace('PGID=1000', f'PGID={os.getgid()}').replace('BIND_IP=127.0.0.1', f'BIND_IP={address}')
    (root / '.env').write_text(text)
    os.chmod(root / '.env', 0o600)
    print('Создан .env. Заполни секреты локально после настройки qBittorrent и Jellyfin.')
else:
    print('.env уже существует, оставлен без изменений.')
print('Далее: docker compose up -d qbittorrent jellyfin. Инструкция в README.md.')
