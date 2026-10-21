"""Configure the dedicated Telegram bot interactively without exposing its token."""
import getpass
import os
from pathlib import Path
from net import HTTP

root = Path(__file__).resolve().parent
os.chdir(root)
path = root / '.env'
if not path.exists():
    raise SystemExit('Сначала выполни python3 prepare.py.')
token = getpass.getpass('Токен бота от BotFather (ввод скрыт): ').strip()
if not token or '\n' in token or "'" in token:
    raise SystemExit('Некорректный токен.')
api = HTTP('https://api.telegram.org/bot' + token)
me = api.call('/getMe')
if not me.get('ok'):
    raise SystemExit('Telegram отклонил токен.')
print('Бот: @' + me['result']['username'])
input('Отправь этому боту /start в личном чате, затем нажми Enter здесь. ')
updates = api.call('/getUpdates', data={'timeout': 10}, timeout=20)
if not updates.get('ok'):
    raise SystemExit('Не удалось прочитать сообщения. Нужен отдельный бот без webhook.')
users = {}
for update in updates['result']:
    msg = update.get('message', {})
    if msg.get('chat', {}).get('type') == 'private':
        u = msg['from']
        if not u.get('is_bot'):
            users[int(u['id'])] = u.get('username') or u.get('first_name', '')
if not users:
    raise SystemExit('Личных сообщений нет. Отправь /start и запусти настройку повторно.')
for uid, name in users.items():
    print(f'{uid}: {name}')
owner = int(input('Введи свой числовой ID из списка: ').strip())
if owner not in users or owner <= 0:
    raise SystemExit('Этот ID не найден среди личных сообщений.')
values = {'TELEGRAM_BOT_TOKEN': token, 'TELEGRAM_OWNER_ID': str(owner)}
lines = path.read_text().splitlines()
for key, value in values.items():
    lines = [line for line in lines if not line.startswith(key + '=')]
    lines.append(f"{key}='{value}'")
tmp = path.with_suffix('.telegram.tmp')
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as f:
    f.write('\n'.join(lines) + '\n')
os.replace(tmp, path)
print('Токен и ID сохранены локально. Далее: docker compose up -d --build queue.')
