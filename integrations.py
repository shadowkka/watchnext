"""Private persisted integration settings; never return secrets through the API."""
import json
import os
from pathlib import Path
import tempfile


def settings_path():
    return Path(os.environ.get('INTEGRATIONS_FILE', str(Path(os.environ.get('STATE_FILE', '/state/state.json')).parent / 'integrations.json')))


def load_settings():
    path = settings_path()
    if path.exists():
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError('Некорректный файл настроек интеграций')
        return data
    return {}


def save_settings(data):
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.integrations-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def telegram_config(data=None):
    data = load_settings() if data is None else data
    token = data.get('telegramToken', os.environ.get('TELEGRAM_BOT_TOKEN', ''))
    owner = str(data.get('telegramOwnerId', os.environ.get('TELEGRAM_OWNER_ID', '')))
    enabled = data.get('telegramEnabled', bool(token and owner))
    return token, owner, bool(enabled)


def public_settings(data=None):
    data = load_settings() if data is None else data
    token, owner, enabled = telegram_config(data)
    return {'telegramConfigured': bool(token and owner), 'telegramEnabled': enabled,
            'telegramOwnerId': owner, 'telegramUsername': data.get('telegramUsername', '')}
