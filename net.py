"""Small HTTP clients. Never put request URLs, credentials or magnets in errors."""
import http.cookiejar
import json
import os
import secrets
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


class RichText(str):
    """Explicit, escaped Telegram HTML; ordinary notifications remain plain text."""


class APIError(RuntimeError):
    pass


class HTTP:
    def __init__(self, base, headers=None):
        self.base = base.rstrip('/')
        self.headers = headers or {}
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path, data=None, query=None, timeout=30, raw=False):
        url = self.base + path + ('?' + urlencode(query) if query else '')
        headers = dict(self.headers)
        payload = None
        if data is not None:
            payload = urlencode(data).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        try:
            with self.opener.open(Request(url, data=payload, headers=headers), timeout=timeout) as r:
                body = r.read().decode()
        except HTTPError as e:
            raise APIError(f'HTTP {e.code}') from None
        except (URLError, TimeoutError, OSError):
            raise APIError('Сервис недоступен или не ответил вовремя.') from None
        if raw:
            return body
        try:
            return json.loads(body) if body else None
        except ValueError:
            raise APIError('Сервис вернул некорректный ответ.') from None


class Qbit:
    def __init__(self):
        base = os.environ.get('QBIT_URL', 'http://qbittorrent:8080')
        self.http = HTTP(base, {'Referer': base + '/'})
        self.logged_in = False

    def login(self):
        if self.logged_in:
            return
        reply = self.http.call('/api/v2/auth/login', data={
            'username': os.environ.get('QBIT_USERNAME', 'admin'),
            'password': os.environ['QBIT_PASSWORD']}, raw=True)
        if reply.strip() not in ('', 'Ok.'):
            raise APIError('Не удалось войти в qBittorrent.')
        version = self.http.call('/api/v2/app/version', raw=True).lstrip('v')
        if int(version.split('.')[0]) < 5:
            raise APIError('Нужен qBittorrent 5.x.')
        # This is a dedicated container: stable paths and sparse allocation are essential.
        prefs = self.http.call('/api/v2/app/preferences')
        required = {'preallocate_all': False, 'incomplete_files_ext': False,
                    'use_unwanted_folder': False, 'temp_path_enabled': False,
                    'use_category_paths_in_manual_mode': False}
        if any(prefs.get(k) != v for k, v in required.items()):
            self.http.call('/api/v2/app/setPreferences', data={'json': json.dumps(required)}, raw=True)
        self.logged_in = True

    def get(self, endpoint, **query):
        return self.http.call('/api/v2/torrents/' + endpoint, query=query)

    def post(self, endpoint, **data):
        return self.http.call('/api/v2/torrents/' + endpoint, data=data, raw=True)

    def add(self, season):
        fields = dict(savepath=season['download_root'], autoTMM='false', useDownloadPath='false',
                      stopped='false', stopCondition='MetadataReceived', contentLayout='Original',
                      category='serial-queue', sequentialDownload='true')
        if not season.get('torrent_file'):
            return self.post('add', urls=season['magnet'], **fields)
        # File metadata is already known: add stopped, inspect its paths/priorities before starting.
        from torrent_meta import MAX_TORRENT, torrent_hash
        path = Path(season['torrent_file'])
        if path.is_symlink() or path.stat().st_size > MAX_TORRENT:
            raise APIError('Сохранённый torrent-файл недоступен или повреждён.')
        data = path.read_bytes()
        if torrent_hash(data) != season['hash']:
            raise APIError('Сохранённый torrent-файл не соответствует раздаче.')
        fields['stopped'] = 'true'
        boundary = 'queue-' + secrets.token_hex(16)
        body = bytearray()
        for key, value in fields.items():
            body.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
        body.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; filename="release.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n').encode())
        body.extend(data)
        body.extend((f'\r\n--{boundary}--\r\n').encode())
        headers = dict(self.http.headers, **{'Content-Type':'multipart/form-data; boundary=' + boundary})
        try:
            with self.http.opener.open(Request(self.http.base + '/api/v2/torrents/add', data=bytes(body), headers=headers), timeout=30) as response:
                result = response.read().decode()
        except HTTPError as exc:
            raise APIError(f'qBittorrent отклонил torrent-файл: HTTP {exc.code}.') from None
        except (URLError, TimeoutError, OSError):
            raise APIError('qBittorrent не ответил при добавлении torrent-файла.') from None
        if result.strip() != 'Ok.':
            try:
                report = json.loads(result)
                accepted = (isinstance(report, dict) and report.get('failure_count') == 0 and
                            (season['hash'] in report.get('added_torrent_ids', []) or
                             report.get('success_count', 0) > 0 or report.get('pending_count', 0) > 0))
            except (ValueError, TypeError):
                accepted = False
            if not accepted:
                raise ValueError('qBittorrent не принял torrent-файл. Выбери другую раздачу или повтори добавление.')
        return result

    def priorities(self, h, files, selected):
        # Set both groups every time: recover safely after a restart or an interrupted update.
        for priority in (0, 1):
            ids = [str(f['index']) for f in files if (f['index'] in selected) == bool(priority)]
            if ids:
                self.post('filePrio', hash=h, id='|'.join(ids), priority=priority)


class Jellyfin:
    def __init__(self):
        self.http = HTTP(os.environ.get('JELLYFIN_URL', 'http://jellyfin:8096'),
                         {'Accept': 'application/json; profile=PascalCase', 'Authorization': 'MediaBrowser Token="' + os.environ['JELLYFIN_API_KEY'] + '"'})

    def snapshot(self):
        users = self.http.call('/Users')
        match = [u for u in users if u['Name'] == os.environ['JELLYFIN_USERNAME']]
        if len(match) != 1:
            raise APIError('Не найден единственный пользователь JELLYFIN_USERNAME.')
        uid = match[0]['Id']
        items, start = [], 0
        while True:
            page = self.http.call('/Items', query={'userId': uid, 'recursive': 'true',
                'fields': 'Path,MediaSources', 'enableUserData': 'true',
                'includeItemTypes': 'Episode,Movie,Video', 'startIndex': start, 'limit': 200})
            batch = page['Items']
            items.extend(batch)
            start += len(batch)
            if not batch or start >= page['TotalRecordCount']:
                break
        # A failed session query aborts the snapshot, so no deletion happens blindly.
        sessions = self.http.call('/Sessions')
        active_ids, active_paths = set(), set()
        for session in sessions:
            item = session.get('NowPlayingItem') or {}
            if item.get('Id'):
                active_ids.add(item['Id'])
            if item.get('Path'):
                active_paths.add(item['Path'])
        played, active = set(), active_paths
        for item in items:
            paths = {item.get('Path')} | {s.get('Path') for s in item.get('MediaSources', [])}
            paths.discard(None)
            if item.get('UserData', {}).get('Played'):
                played.update(paths)
            if item.get('Id') in active_ids:
                active.update(paths)
        return played, active

    def refresh(self):
        self.http.call('/Library/Refresh', data={}, raw=True)


class Telegram:
    def __init__(self, token=None, owner=None):
        self.owner = int(owner if owner is not None else os.environ['TELEGRAM_OWNER_ID'])
        if self.owner <= 0:
            raise ValueError('TELEGRAM_OWNER_ID должен быть ID личного аккаунта.')
        self._token=token if token is not None else os.environ['TELEGRAM_BOT_TOKEN']
        self.http = HTTP('https://api.telegram.org/bot' + self._token)
        self._bot_username = None

    @property
    def bot_username(self):
        if self._bot_username is None:
            self._bot_username = self.call('getMe')['username']
        return self._bot_username

    def call(self, method, **data):
        response = self.http.call('/' + method, data=data, timeout=35)
        if not response or not response.get('ok'):
            raise APIError('Telegram отклонил запрос.')
        return response['result']

    def send(self, text, chat_id=None, reply_markup=None):
        data = {'chat_id': self.owner if chat_id is None else chat_id, 'text': str(text) if isinstance(text,RichText) else text[:4000]}
        if isinstance(text,RichText):
            data['parse_mode']='HTML'
        if reply_markup is not None:
            data['reply_markup'] = json.dumps(reply_markup, ensure_ascii=False)
        return self.call('sendMessage', **data)['message_id']

    def edit(self, text, chat_id, message_id, reply_markup):
        data = {'chat_id':chat_id, 'message_id':message_id, 'text':str(text) if isinstance(text,RichText) else text[:4000],
                'reply_markup':json.dumps(reply_markup,ensure_ascii=False)}
        if isinstance(text,RichText): data['parse_mode']='HTML'
        return self.call('editMessageText', **data)

    def answer(self, query_id, text='', alert=False):
        return self.call('answerCallbackQuery', callback_query_id=query_id, text=text, show_alert='true' if alert else 'false')

    def updates(self, offset, timeout=10):
        return self.call('getUpdates', offset=offset, timeout=timeout,
                         allowed_updates=json.dumps(['message', 'callback_query']))

    def download_document(self, document):
        item=self.call('getFile',file_id=document['file_id'])
        file=item.get('file_path','')
        if not file or not re.fullmatch(r'[A-Za-z0-9_./-]+',file) or '..' in file.split('/') or file.startswith('/'):
            raise APIError('Telegram не дал допустимый путь файла.')
        if item.get('file_size',0)>1024*1024:raise ValueError('Файл должен быть не больше 1 МБ.')
        try:
            with self.http.opener.open(Request('https://api.telegram.org/file/bot'+self._token+'/'+file),timeout=30) as response:
                raw=response.read(1024*1024+1)
        except (HTTPError,URLError,TimeoutError,OSError):raise APIError('Не удалось получить файл из Telegram.') from None
        if len(raw)>1024*1024:raise ValueError('Файл должен быть не больше 1 МБ.')
        return raw

    def photo(self, image, caption, uid, markup):
        if not isinstance(image,bytes) or len(image)>2*1024*1024:raise ValueError('Слишком большая обложка.')
        boundary='WatchNext'+secrets.token_hex(16)
        fields={'chat_id':str(uid),'caption':str(caption),'parse_mode':'HTML','reply_markup':json.dumps(markup,ensure_ascii=False)}
        chunks=[]
        for key,value in fields.items():
            chunks.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+key+'"\r\n\r\n'+value+'\r\n').encode())
        chunks.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="photo"; filename="poster.jpg"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
        chunks += [image,('\r\n--'+boundary+'--\r\n').encode()]
        try:
            with self.http.opener.open(Request(self.http.base+'/sendPhoto',data=b''.join(chunks),headers={'Content-Type':'multipart/form-data; boundary='+boundary}),timeout=30) as response:
                result=json.loads(response.read(1024*1024))
        except (HTTPError,URLError,TimeoutError,OSError,ValueError):raise APIError('Telegram не принял обложку.') from None
        if not result.get('ok'):raise APIError('Telegram не принял обложку.')
        return result['result']['message_id']
