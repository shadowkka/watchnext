"""Read private Telegram updates to show your numeric ID before starting the worker."""
import getpass
from net import HTTP

token = getpass.getpass('Токен нового бота (не отображается): ').strip()
print('Отправь боту /start в Telegram. Очередь в этот момент должна быть выключена.')
response = HTTP('https://api.telegram.org/bot' + token).call('/getUpdates', data={'timeout': 20}, timeout=30)
if not response.get('ok'):
    raise SystemExit('Telegram отклонил запрос. Проверь токен; для этой очереди нужен отдельный бот без webhook.')
users = {}
for update in response['result']:
    message = update.get('message', {})
    if message.get('chat', {}).get('type') == 'private':
        u = message.get('from', {})
        users[u['id']] = u.get('username') or u.get('first_name', '')
for uid, name in users.items():
    print(f'{name}: TELEGRAM_OWNER_ID={uid}')
if not users:
    print('Личных сообщений ещё нет. Отправь /start и запусти скрипт повторно.')
