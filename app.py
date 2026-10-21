"""Single-process Telegram + qBittorrent + Jellyfin queue worker."""
import json
import hashlib
import hmac
import logging
import os
import shutil
import secrets
import time
from pathlib import Path

from core import GB, episode_count, episode_label, episodes, finished, last_season, low_seasons, magnet_hash, movie_files, parse_start, plan, safe_path, slug
from net import APIError, Jellyfin, Qbit, Telegram
from torrent_meta import torrent_hash
from ui import MenuUI

LOG = logging.getLogger('serial-queue')
HELP = "Открой /menu: добавление, выбор раздачи и настройки доступны кнопками. /cancel — отменить текущий ввод."


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {
            'count': 5, 'cap': None, 'reserve': 5 * GB, 'notify': 2,
            'paused': False, 'offset': 0, 'seasons': [], 'replies': {}, 'alerts': {}}

    def save(self):
        temp = self.path.with_suffix('.tmp')
        with temp.open('w') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, self.path)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def library_relative(season, e):
    title = slug(season['title'])
    if season.get('kind') == 'movie':
        return f"{title}/{title}{Path(e['name']).suffix.lower()}"
    sn = e.get('season', season['season'])
    return f"{title}/Season {sn:02d}/{title} - {episode_label(e, sn)}{Path(e['name']).suffix.lower()}"


class Worker(MenuUI):
    def __init__(self, store, qbit, jellyfin, telegram, data_root='/data'):
        self.store, self.q, self.j, self.tg = store, qbit, jellyfin, telegram
        self.root = Path(data_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.library = self.root / 'library'
        self.library.mkdir(exist_ok=True)
        self.movies = self.root / 'movies'
        self.movies.mkdir(exist_ok=True)
        self.grace = int(os.environ.get('DELETE_GRACE_SECONDS', '120'))

    @property
    def refresh_needed(self):
        return self.state.get('refresh_needed', False)

    @refresh_needed.setter
    def refresh_needed(self, value):
        self.state['refresh_needed'] = value

    @property
    def state(self):
        return self.store.data

    @property
    def users(self):
        return {self.tg.owner} | {int(uid) for uid in self.state.get('members', {})}

    def broadcast(self, text, delivered=None, reply_target=None, menu_pack=None):
        """Notifications go to each member; Telegram message IDs are scoped to their chat."""
        delivered = delivered if delivered is not None else {}
        for uid in sorted(self.users):
            if str(uid) in delivered:
                continue
            try:
                rows = [[self.ui_button(uid, '🏠 Главное меню', 'home')]]
                target = menu_pack or reply_target
                if target:
                    rows.insert(0, [self.ui_button(uid, 'Раздача и файлы', 'files', target, 0)])
                if reply_target:
                    rows.insert(0, [self.ui_button(uid, 'Добавить продолжение', 'next', reply_target),
                                    self.ui_button(uid, 'Не предлагать продолжение', 'finish', reply_target)])
                self.store.save()
                delivered[str(uid)] = self.tg.send(text, chat_id=uid, reply_markup={'inline_keyboard': rows})
                if reply_target is not None:
                    self.state['replies'][f'{uid}:{delivered[str(uid)]}'] = reply_target
                self.store.save()
            except APIError as exc:
                if str(exc) in ('HTTP 403', 'HTTP 400'):
                    # One blocked/private chat must not stop the family's downloads.
                    LOG.warning('Уведомление недоступно для пользователя %s', uid)
                    delivered[str(uid)] = None
                    self.store.save()
                    continue
                raise
        return delivered

    def private_sender(self, message):
        uid = message.get('from', {}).get('id')
        if (isinstance(uid, int) and uid > 0 and
                message.get('chat', {}).get('id') == uid and
                message.get('chat', {}).get('type') == 'private'):
            return uid
        return None

    def reply_error(self, message, exc):
        uid = self.private_sender(message)
        if uid is not None and (uid in self.users or message.get('text', '').startswith('/start join_')):
            self.tg.send(str(exc), chat_id=uid)

    def accept_invite(self, uid, payload, sender):
        if uid in self.users:
            self.tg.send('Доступ уже есть. /status — общая очередь, /help — команды.', chat_id=uid)
            return
        invite = self.state.get('invite') or {}
        digest = hashlib.sha256(payload.encode()).hexdigest()
        if (invite.get('expires', 0) <= time.time() or
                not hmac.compare_digest(digest, invite.get('digest', ''))):
            raise ValueError('Приглашение недействительно или уже использовано. Попроси владельца новую ссылку через /invite.')
        self.state.setdefault('members', {})[str(uid)] = {
            'name': sender.get('username') or sender.get('first_name') or str(uid),
            'joined': time.time()}
        self.state.pop('invite', None)
        self.store.save()
        self.tg.send('Доступ открыт. Теперь ты можешь управлять общей очередью. /help — команды, /status — состояние.', chat_id=uid)
        self.tg.send('К общей очереди подключился пользователь: ' +
                     self.state['members'][str(uid)]['name'] + '. /users — список доступа.', chat_id=self.tg.owner)

    def alert(self, key, message):
        # Persist only after sending; retry on network failures. No repeated alert on success.
        if not self.state['alerts'].get(key):
            self.broadcast(message)
            self.state['alerts'][key] = True
            self.store.save()

    def season_by_id(self, value):
        matches = [s for s in self.state['seasons'] if s['hash'].startswith(value.lower())]
        if len(matches) != 1:
            raise ValueError('Раздача не найдена. Открой /files и выбери её заново.')
        return matches[0]

    def add(self, title, number, magnet, start_episode=1, kind='series', auto_season=False, torrent_data=None):
        title = slug(title.strip())
        number, start_episode = parse_start(f'{number} {start_episode}')
        if kind not in ('series', 'movie'):
            raise ValueError('Выбери «Сериал» или «Фильм».')
        h = magnet_hash(magnet)
        if torrent_data is not None and torrent_hash(torrent_data) != h:
            raise ValueError('torrent-файл не соответствует выбранной раздаче.')
        previous = next((s for s in self.state['seasons'] if s['hash'] == h), None)
        torrent_exists = bool(self.q.get('info', hashes=h))
        if previous is not None and torrent_exists:
            return f'{previous["title"]}: эта раздача уже добавлена. /files — файлы и управление.'
        if previous is None and torrent_exists:
            raise ValueError('Этот торрент уже существует в qBittorrent. Внешние загрузки я не забираю под управление.')
        # An explicit re-add after manual torrent removal replaces the stale queue record.
        # Keep the old library title if published files still exist under that name.
        if previous is not None and any(e['published'] and self.dest(previous, e).exists() for e in previous['episodes']):
            if previous.get('kind', 'series') != kind:
                raise ValueError('У этой раздачи ещё есть файлы в библиотеке. Сохрани прежний тип при восстановлении.')
            title = previous['title']
            for e in previous['episodes']:
                dst = self.dest(previous, e)
                if not e['published'] or not dst.is_file():
                    continue
                src = self.source(previous, e)
                if not src.exists():
                    src.parent.mkdir(parents=True, exist_ok=True)
                    os.link(dst, src)
                elif not os.path.samefile(src, dst):
                    raise ValueError('В библиотеке остался файл этой раздачи с другим исходником. Сначала удали его через Jellyfin.')
        # Keep identical capitalization for Jellyfin and filesystem paths across seasons.
        for s in self.state['seasons']:
            if s['title'].casefold() == title.casefold() and s.get('kind', 'series') == kind:
                title = s['title']
                break
        pack = {'hash': h, 'title': title, 'season': number, 'kind': kind, 'auto_season': auto_season,
            'start_episode': start_episode, 'magnet': magnet.strip(), 'download_root': str(self.root / 'downloads' / h),
            'episodes': [], 'notified': False, 'phase': 'metadata', 'submitted': False, 'created': time.time()}
        if torrent_data is not None:
            folder = self.store.path.parent / 'torrents'
            folder.mkdir(exist_ok=True)
            file = folder / (h + '.torrent')
            temp = file.with_suffix('.tmp')
            temp.write_bytes(torrent_data)
            os.chmod(temp, 0o600)
            os.replace(temp, file)
            pack['torrent_file'] = str(file.resolve())
        elif previous is not None and previous.get('torrent_file'):
            pack['torrent_file'] = previous['torrent_file']
        if previous is not None and previous['phase'] not in ('complete', 'retiring') and previous.get('kind', 'series') == kind:
            pack['recovery'] = [dict(e) for e in previous['episodes'] if e['watched'] or e.get('discarded') or e['published']]
        if previous is None:
            self.state['seasons'].append(pack)
        else:
            self.state['seasons'][self.state['seasons'].index(previous)] = pack
            self.state['replies'] = {key: value for key, value in self.state['replies'].items() if value != h}
            for prefix in ('pack:', 'metadata:', 'missing:'):
                self.state['alerts'].pop(prefix + h, None)
        self.ui_autostart()
        self.store.save()
        start = 'фильм' if kind == 'movie' else ('сезон автоматически' if auto_season else f'начало S{number:02d}E{start_episode:02d}')
        paused = ' Сохраняю включённую вами паузу. Продолжить можно кнопкой в меню.' if self.state['paused'] else ' Загрузка начнётся автоматически в пределах лимита.'
        action = 'Восстановлен' if previous is not None else 'Добавлен'
        return f'{action} {title}, {start}. Получаю список файлов.' + paused

    def set_start(self, pack, season, number):
        if pack.get('kind') == 'movie':
            raise ValueError('Для фильма сезон и начальная серия не нужны.')
        point = (season, number)
        if pack['episodes'] and not any((e.get('season', pack['season']), e.get('end_number', e['number'])) >= point for e in pack['episodes']):
            raise ValueError('Начиная с этой позиции в раздаче нет серий.')
        if self.q.get('info', hashes=pack['hash']):
            self.stop(pack)
        fallback = pack['season']
        for e in pack['episodes']:
            e.setdefault('season', fallback)
            old = e.get('skipped_start', False)
            skip = (e['season'], e.get('end_number', e['number'])) < point
            e['skipped_start'] = skip
            if skip and not old and not e['watched'] and not e.get('discarded'):
                e['removed'] = not (e['selected'] or e['published'] or self.source(pack, e).exists())
            elif old and not skip and not e['watched'] and not e.get('discarded'):
                e['removed'], e['selected'], e['published'] = False, False, False
        pack['season'], pack['start_episode'] = season, number
        pack['auto_season'] = False
        pack.pop('error', None)
        if not pack['episodes']:
            pack['phase'] = 'metadata'
        pack['notified'] = False
        pack.pop('notification_delivered', None)
        self.state['alerts'].pop('pack:' + pack['hash'], None)
        self.store.save()

    def validate_overlap(self, pack):
        covered = set()
        for other in self.state['seasons']:
            if other.get('phase') == 'missing' or other is pack or other['title'].casefold() != pack['title'].casefold() or other.get('kind', 'series') != pack.get('kind', 'series'):
                continue
            for e in other['episodes']:
                if not finished(e) or e['published']:
                    covered.update((e.get('season', other['season']), n) for n in range(e['number'], e.get('end_number', e['number']) + 1))
        for e in pack['episodes']:
            if not finished(e) and any((e['season'], n) in covered for n in range(e['number'], e['end_number'] + 1)):
                raise ValueError('Эти серии уже есть в другой раздаче этого сериала.')

    def status(self):
        count, cap = self.state['count'], self.state['cap']
        lines = [f"{'Пауза. ' if self.state['paused'] else ''}Лимит: " +
                 (f'{count} серий; ' if count is not None else '') +
                 (f'{cap / GB:g} ГБ; ' if cap is not None else '') +
                 f"резерв {self.state['reserve'] / GB:g} ГБ.", 'Количество ограничивает только серии; ГБ — серии и фильмы вместе.']
        reserved = 0
        for s in self.ordered_seasons():
            eps = s['episodes']
            left = sum(episode_count(e) for e in eps if not finished(e))
            published = sum(episode_count(e) for e in eps if e['published'] and not finished(e))
            reserved += sum(e['size'] for e in eps if e['selected'] and not finished(e))
            phase = {'metadata': 'получаю список файлов', 'ready': 'в очереди',
                     'needcheck': 'обновляю файлы', 'checking': 'проверяю файлы',
                     'retiring': 'освобождаю место', 'complete': 'раздача завершена',
                     'missing': 'удалена из qBittorrent; добавь заново через /add'}[s['phase']]
            if s['phase'] == 'ready' and s.get('download_idle'):
                phase = 'выбранные файлы скачаны; раздача остановлена'
            seasons = sorted({e.get('season', s['season']) for e in eps})
            span = 'фильм' if s.get('kind') == 'movie' else (','.join(f'S{n:02d}' for n in seasons) or f"S{s['season']:02d}")
            lines.append(f"{s['title']} {span}: осталось {left}, готовы {published}. {s.get('error', '') or phase}")
            reason = self.waiting_reason(s)
            if reason:
                lines.append(reason)
            if s.get('kind') != 'movie':
                lines.append(f"Начало: S{s['season']:02d}E{s.get('start_episode', 1):02d}")
        lines.append(f'Выбрано файлов на {reserved / GB:.2f} ГБ; свободно на диске {shutil.disk_usage(self.root).free / GB:.2f} ГБ.')
        return '\n'.join(lines)

    def waiting_reason(self, pack):
        if pack['phase'] != 'ready' or pack.get('error'):
            return ''
        pending = next((e for e in pack['episodes'] if not finished(e) and not e['selected']), None)
        if pending is None:
            return ''
        if self.state['paused']:
            return 'Очередь на паузе. Нажми «Продолжить» в главном меню.'
        chosen = [(p, e) for p in self.state['seasons'] if p['phase'] != 'missing'
                  for e in p['episodes'] if e['selected'] and not finished(e)]
        count = sum(episode_count(e) for p, e in chosen if p.get('kind') != 'movie')
        used = sum(e['size'] for _, e in chosen)
        cap = self.state['cap']
        if pack.get('kind') != 'movie' and self.state['count'] is not None and count + episode_count(pending) > self.state['count']:
            return f"Ждёт места в лимите серий: занято {count} из {self.state['count']}. Фильмы этот лимит не занимают."
        if cap is not None and used + pending['size'] > cap:
            return f"Ждёт общего лимита объёма: занято {used / GB:.2f} из {cap / GB:g} ГБ; следующий файл {pending['size'] / GB:.2f} ГБ."
        if pending['size'] > max(0, shutil.disk_usage(self.root).free - self.state['reserve']):
            return 'Ждёт свободного места на диске с учётом резерва.'
        return 'Ожидает очередного цикла планирования или завершения предыдущих файлов.'

    def prompt(self, uid, text, choices=None):
        rows = [[choice] for choice in (choices or [])] + [['Отмена']]
        self.tg.send(text, chat_id=uid, reply_markup={
            'keyboard': rows, 'resize_keyboard': True, 'one_time_keyboard': True})

    def dialog(self, uid, text):
        d = self.state['dialogs'][str(uid)]
        step = d['step']
        if step == 'kind':
            kind = {'сериал': 'series', 'фильм': 'movie'}.get(text.casefold())
            if kind is None:
                self.prompt(uid, 'Выбери «Сериал» или «Фильм».', ['Сериал', 'Фильм'])
                return
            d.update(kind=kind, step='title')
            prompt = 'Как назвать сериал?' if kind == 'series' else 'Как назвать фильм? Можно добавить год в скобках.'
            choices = None
        elif step == 'title':
            d['title'] = slug(text)
            d['step'] = 'season' if d['kind'] == 'series' else 'magnet'
            prompt = 'С какого сезона начать? Введи номер или выбери «Авто» — самый ранний сезон в раздаче.' if d['kind'] == 'series' else 'Пришли magnet-ссылку на фильм.'
            choices = ['Авто'] if d['kind'] == 'series' else None
        elif step == 'season':
            if text.casefold() == 'авто':
                d.update(season=1, auto_season=True)
            elif text.isdigit() and 0 <= int(text) <= 999:
                d.update(season=int(text), auto_season=False)
            else:
                raise ValueError('Введи номер сезона 0–999 или нажми «Авто».')
            d['step'] = 'episode'
            prompt, choices = 'С какой серии начать? Введи номер, например 1 или 5.', ['1']
        elif step == 'episode':
            if not text.isdigit() or not 1 <= int(text) <= 9999:
                raise ValueError('Нужен номер серии 1–9999.')
            d.update(start_episode=int(text), step='magnet')
            prompt, choices = 'Пришли magnet-ссылку на раздачу сериала. Можно несколько сезонов в одной раздаче.', None
        elif step == 'magnet':
            response = self.add(d['title'], d.get('season', 1), text,
                                start_episode=d.get('start_episode', 1), kind=d['kind'],
                                auto_season=d.get('auto_season', False))
            self.state['dialogs'].pop(str(uid), None)
            self.store.save()
            self.tg.send(response, chat_id=uid, reply_markup={'remove_keyboard': True})
            self.ui_home(uid)
            return
        else:
            raise ValueError('Состояние добавления неизвестно. Начни заново: /add.')
        self.store.save()
        self.prompt(uid, prompt, choices)

    def command(self, message):
        uid = self.private_sender(message)
        if uid is None:
            return
        text = message.get('text', '').strip()
        start = text.split()
        if len(start) == 2 and start[0].split('@')[0] == '/start' and start[1].startswith('join_'):
            self.accept_invite(uid, start[1][5:], message.get('from', {}))
            return
        if uid not in self.users:
            return
        if text in ('/cancel', 'Отмена'):
            self.state.setdefault('follow_dialogs',{}).pop(str(uid),None)
            self.state.get('searches', {}).pop(str(uid), None)
            self.state.setdefault('dialogs', {}).pop(str(uid), None)
            self.state.setdefault('ui_input', {}).pop(str(uid), None)
            self.store.save()
            self.tg.send('Ввод отменён.', chat_id=uid, reply_markup={'remove_keyboard': True})
            self.ui_home(uid)
            return
        if text.split(maxsplit=1) and text.split(maxsplit=1)[0].split('@')[0] in ('/follow','/subscriptions'):
            cmd=text.split(maxsplit=1)[0].split('@')[0]
            if cmd=='/subscriptions':self.follow_list(uid)
            else:self.follow_begin(uid,text.split(maxsplit=1)[1] if len(text.split(maxsplit=1))>1 else '')
            return
        if text.split(maxsplit=1) and text.split(maxsplit=1)[0].split('@')[0] == '/search':
            query = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ''
            self.search_start(uid, query)
            return
        if self.ui_command(uid, text):
            return
        if text.startswith('/'):
            self.state.setdefault('ui_input', {}).pop(str(uid), None)
        elif str(uid) in self.state.get('ui_input', {}):
            pending = self.state['ui_input'][str(uid)]
            self.ui_value(uid, pending['kind'], pending.get('hash'), text)
            return
        if text.split('@', 1)[0] == '/add' and len(text.split()) == 1:
            self.state.setdefault('dialogs', {})[str(uid)] = {'step': 'kind'}
            self.store.save()
            self.prompt(uid, 'Что добавляем?', ['Сериал', 'Фильм'])
            return
        if str(uid) in self.state.get('dialogs', {}) and not text.startswith('/'):
            self.dialog(uid, text)
            return
        reply = message.get('reply_to_message', {}).get('message_id')
        if text.startswith('magnet:'):
            target = self.state['replies'].get(f'{uid}:{reply}')
            if target is None and uid == self.tg.owner:
                target = self.state['replies'].get(str(reply))  # migrate existing owner-only replies
            if not target:
                raise ValueError('Ответь ссылкой на уведомление о конце сезона или используй /add Название | 1 | magnet:…')
            s = self.season_by_id(target)
            response = self.add(s['title'], last_season(s) + 1, text)
        elif text.startswith('/add '):
            parts = text[5:].split('|', 3)
            if len(parts) == 3:
                sn, en = parse_start(parts[1])
                magnet = parts[2].strip()
            elif len(parts) == 4:
                sn, en = parse_start(parts[1].strip() + ' ' + parts[2].strip())
                magnet = parts[3].strip()
            else:
                raise ValueError('/add Название | S02E05 | magnet:…')
            response = self.add(parts[0], sn, magnet, start_episode=en)
        else:
            args = text.split()
            if not args:
                return
            cmd = args[0].split('@')[0]
            if cmd in ('/start', '/help'):
                response = HELP
            elif cmd in ('/invite', '/users', '/revoke'):
                if uid != self.tg.owner:
                    raise ValueError('Управлять доступом может только владелец бота.')
                if cmd == '/invite':
                    token = secrets.token_urlsafe(24)
                    self.state['invite'] = {'digest': hashlib.sha256(token.encode()).hexdigest(),
                                            'expires': time.time() + 86400}
                    response = ('Одноразовая ссылка для подключения к общей очереди:\n' +
                                f'https://t.me/{self.tg.bot_username}?start=join_{token}' +
                                '\nДействует 24 часа. Перешли её жене; ей нужно открыть ссылку и нажать «Запустить». Новая /invite отменит прежнюю ссылку.')
                elif cmd == '/users':
                    response = f'{self.tg.owner} — владелец\n' + '\n'.join(
                        f"{member_id} — {info['name']}" for member_id, info in self.state.get('members', {}).items())
                else:
                    if len(args) != 2 or not args[1].isdigit():
                        raise ValueError('/revoke ID — ID из /users.')
                    target = int(args[1])
                    if target == self.tg.owner:
                        raise ValueError('Доступ владельца сохраняется.')
                    removed = self.state.get('members', {}).pop(str(target), None)
                    self.state.get('searches', {}).pop(str(target), None)
                    self.state.get('follow_dialogs',{}).pop(str(target),None)
                    self.state.setdefault('dialogs', {}).pop(str(target), None)
                    self.state.setdefault('ui_input', {}).pop(str(target), None)
                    self.state['buttons'] = {k: v for k, v in self.state.get('buttons', {}).items() if v['uid'] != target}
                    response = 'Доступ отозван.' if removed else 'Этот пользователь не был добавлен.'
            elif cmd == '/from':
                if len(args) not in (3, 4):
                    raise ValueError('/from ID S02E05 или /from ID 2 5')
                pack = self.season_by_id(args[1])
                sn, en = parse_start(' '.join(args[2:]))
                self.set_start(pack, sn, en)
                self.ui_autostart()
                response = f'Начало изменено: сезон {sn}, серия {en}. Более ранние серии исключены из очереди.'
            elif cmd == '/retry' and len(args) == 2:
                pack = self.season_by_id(args[1])
                pack.pop('error', None)
                if not pack['episodes']:
                    pack['phase'] = 'metadata'
                self.state['alerts'].pop('pack:' + pack['hash'], None)
                response = 'Повторю разбор раздачи.'
            elif cmd == '/status':
                response = self.status()
            elif cmd == '/limit':
                count, cap = None, None
                if len(args) == 3 and args[1] == 'episodes':
                    count = int(args[2])
                elif len(args) == 3 and args[1] == 'gb':
                    cap = int(float(args[2]) * GB)
                elif len(args) == 4 and args[1] == 'both':
                    count, cap = int(args[2]), int(float(args[3]) * GB)
                else:
                    raise ValueError('/limit episodes 5, /limit gb 15 или /limit both 5 15')
                if (count is not None and not 1 <= count <= 1000) or (cap is not None and not GB / 10 <= cap <= 100000 * GB):
                    raise ValueError('Количество 1–1000, место 0.1–100000 ГБ.')
                self.state['count'], self.state['cap'] = count, cap
                response = 'Лимит изменён. Уже выбранные серии останутся до просмотра.'
            elif cmd in ('/notify', '/reserve') and len(args) == 2:
                value = float(args[1])
                if not 0 <= value <= 10000 or (cmd == '/notify' and not value.is_integer()):
                    raise ValueError('Нужно неотрицательное число; для /notify — целое.')
                self.state['notify' if cmd == '/notify' else 'reserve'] = int(value if cmd == '/notify' else value * GB)
                response = 'Настройка сохранена.'
            elif cmd in ('/pause', '/resume'):
                self.state['paused'] = cmd == '/pause'
                if self.state['paused']:
                    self.state['pause_reason'] = 'user'
                else:
                    self.state.pop('pause_reason', None)
                response = 'Пауза.' if self.state['paused'] else 'Продолжаю.'
            elif cmd == '/finish' and len(args) == 2:
                self.season_by_id(args[1])['notified'] = True
                response = 'Продолжение этого сезона запрашивать не буду.'
            elif cmd == '/files' and len(args) == 2:
                s = self.season_by_id(args[1])
                response = '\n'.join((episode_label(e, s['season']) + (' · пропущено' if e.get('skipped_start') else '') + ': ' if s.get('kind') != 'movie' else '') + f"{e['name']} ({e['size'] / GB:.2f} ГБ)" for e in s['episodes']) or 'Ещё нет метаданных.'
            else:
                response = HELP
        self.store.save()
        self.ui_home(uid, response)

    def ordered_seasons(self):
        order = {}
        for s in self.state['seasons']:
            order.setdefault(s['title'].casefold(), len(order))
        return sorted(self.state['seasons'], key=lambda s: (order[s['title'].casefold()], s['season']))

    def source(self, s, e):
        return safe_path(self.root, 'downloads/' + s['hash'] + '/' + e['name'])

    def dest(self, s, e):
        return safe_path(self.movies if s.get('kind') == 'movie' else self.library, library_relative(s, e))

    def stop(self, s):
        self.q.post('stop', hashes=s['hash'])

    def tick(self):
        infos = {t['hash']: t for t in self.q.get('info')}
        all_files = {}
        # Register metadata first. MetadataReceived prevents downloading an entire pack.
        for s in self.ordered_seasons():
            h = s['hash']
            info = infos.get(h)
            if s['phase'] in ('retiring', 'complete'):
                if info:
                    self.q.post('delete', hashes=h, deleteFiles='false')
                elif s['phase'] == 'retiring':
                    folder = safe_path(self.root, 'downloads/' + h)
                    if folder.exists():
                        shutil.rmtree(folder)
                    s['phase'] = 'complete'
                    self.store.save()
                continue
            if not info:
                if s['phase'] == 'metadata' and not s.get('submitted'):
                    if s.get('error'):
                        continue
                    try:
                        self.q.add(s)
                    except ValueError as exc:
                        s['error'] = str(exc)
                        self.store.save()
                        self.alert('pack:' + h, s['title'] + ': ' + str(exc))
                        continue
                    s['submitted'] = True
                    self.store.save()
                elif s['phase'] != 'missing':
                    s['phase'] = 'missing'
                    for e in s['episodes']:
                        e['selected'] = False
                    self.store.save()
                    self.alert('missing:' + h, f"{s['title']}: раздача удалена из qBittorrent. Чтобы загрузить её снова, выбери «Восстановить» в /files. Остальная очередь продолжает работать.")
                continue
            if not s.get('submitted'):
                s['submitted'] = True
                self.store.save()
            if s['phase'] == 'missing':
                # A user restored the known torrent manually; metadata will be parsed again.
                s['phase'], s['episodes'] = 'metadata', []
                s.pop('error', None)
                self.state['alerts'].pop('missing:' + h, None)
                self.store.save()
            if Path(info['save_path']).resolve() != Path(s['download_root']).resolve():
                self.stop(s)
                raise APIError(f'Папка торрента {h[:8]} изменена. Управление остановлено.')
            if s.get('error'):
                self.stop(s)
                continue
            files = self.q.get('files', hash=h)
            if not files:
                if time.time() - s['created'] > 600:
                    self.alert('metadata:' + h, f"{s['title']}: за 10 минут не получен список файлов. Возможно, нет пиров. Попытки продолжатся.")
                continue
            all_files[h] = files
            if s['phase'] == 'metadata':
                self.stop(s)
                self.q.priorities(h, files, set())
                try:
                    if s.get('kind') == 'movie':
                        s['episodes'] = movie_files(files)
                    else:
                        if s.get('auto_season'):
                            detected = episodes(files, 1, require_start=False)
                            s['season'] = min(e['season'] for e in detected)
                        s['episodes'] = episodes(files, s['season'], s.get('start_episode', 1))
                    recovered = {(e['index'], e['name'], e['size']): e for e in s.get('recovery', [])}
                    for e in s['episodes']:
                        self.source(s, e)  # validate every torrent path before allowing downloads
                        old = recovered.get((e['index'], e['name'], e['size']))
                        if old:
                            e['watched'] = old['watched']
                            e['discarded'] = old.get('discarded', False)
                            e['published'] = old['published'] and self.dest(s, e).is_file()
                            e['removed'] = old['removed'] if e['watched'] or e['discarded'] else e['removed']
                            if e['published']:
                                e['removed'] = False
                    s.pop('recovery', None)
                    self.validate_overlap(s)
                    s['phase'] = 'ready'
                    self.store.save()
                    if s.get('kind') == 'movie':
                        self.broadcast(f"{s['title']}: выбран видеофайл {s['episodes'][0]['name']}. /files — информация.", menu_pack=h)
                    else:
                        self.broadcast(f"{s['title']}: распознано {sum(episode_count(e) for e in s['episodes'])} серий в {len(s['episodes'])} файлах. /files — сезоны и порядок файлов.", menu_pack=h)
                except ValueError as exc:
                    s['error'] = str(exc)
                    self.store.save()
                    self.alert('pack:' + h, s['title'] + ': ' + s['error'])
        if self.state['paused']:
            for s in self.state['seasons']:
                if s['hash'] in infos:
                    self.stop(s)
            return
        played, active = self.j.snapshot()
        now = time.time()
        # Persist the watched decision before unlinking; retries complete interrupted deletions.
        for s in self.state['seasons']:
            if s['phase'] == 'missing':
                continue
            for e in s['episodes']:
                path = str(self.dest(s, e))
                if e['published'] and not finished(e):
                    if not (self.movies if s.get('kind') == 'movie' else self.library).is_dir():
                        raise APIError('Папка библиотеки недоступна. Удаление остановлено.')
                    if not self.dest(s, e).exists():
                        source = self.source(s, e)
                        if not source.is_file() or source.stat().st_size != e['size']:
                            raise APIError('Опубликованный файл и исходник недоступны. Проверь диск.')
                        # Jellyfin removed the library hardlink. Retire the source through the
                        # same stopped-torrent cleanup path, without altering viewing history.
                        e['discarded'] = True
                        e['seen_at'] = None
                        continue
                    if path in played and path not in active:
                        e['seen_at'] = e['seen_at'] or now
                        if now - e['seen_at'] >= self.grace:
                            e['watched'] = True
                    else:
                        e['seen_at'] = None
        self.store.save()
        # Stop torrent, exclude watched files, unlink both hardlinks, then recheck.
        # qBittorrent must not retain pieces for a physically deleted file in its resume data.
        blocked = set()
        for s in self.state['seasons']:
            h = s['hash']
            if h not in all_files:
                continue
            info = infos[h]
            pending = [e for e in s['episodes'] if finished(e) and not e['removed']]
            if pending or s['phase'] in ('needcheck', 'checking'):
                blocked.add(h)
                self.stop(s)
                if 'checking' in info['state'].lower():
                    continue
                if not info['state'].startswith('stopped'):
                    continue
                if s['phase'] == 'checking':
                    s['phase'] = 'retiring' if all(e['removed'] for e in s['episodes']) else 'ready'
                    self.store.save()
                    continue
                # Re-fetch sessions before deleting, including episodes marked on an earlier tick.
                _, active_now = self.j.snapshot()
                if any(str(self.dest(s, e)) in active_now for e in pending):
                    continue
                keep = {e['index'] for e in s['episodes'] if e['selected'] and not finished(e)}
                self.q.priorities(h, all_files[h], keep)
                for e in pending:
                    self.dest(s, e).unlink(missing_ok=True)
                    self.source(s, e).unlink(missing_ok=True)
                    e['removed'], e['published'], e['selected'] = True, False, False
                    self.refresh_needed = True
                s['phase'] = 'needcheck'
                self.store.save()
                self.q.post('recheck', hashes=h)
                s['phase'] = 'checking'
                self.store.save()
        rows = [(s['hash'] + ':' + str(e['index']), dict(e, count_units=0 if s.get('kind') == 'movie' else episode_count(e))) for s in self.ordered_seasons()
                if not s.get('error') and s['phase'] != 'missing' for e in s['episodes']]
        show_order = {}
        pack_info = {}
        for pack in self.ordered_seasons():
            show_order.setdefault(pack['title'].casefold(), len(show_order))
            pack_info[pack['hash']] = (show_order[pack['title'].casefold()], pack['season'], 0 if pack.get('kind') == 'movie' else 1)
        rows.sort(key=lambda row: (pack_info[row[0].split(':')[0]][2], pack_info[row[0].split(':')[0]][0],
                                   row[1].get('season', pack_info[row[0].split(':')[0]][1]), row[1]['number']))
        free = shutil.disk_usage(self.root).free
        outstanding = 0
        for s in self.state['seasons']:
            if s['phase'] == 'missing':
                continue
            progress = {f['index']: f['progress'] for f in all_files.get(s['hash'], [])}
            outstanding += sum(int(e['size'] * (1 - progress.get(e['index'], 0)))
                               for e in s['episodes'] if e['selected'] and not finished(e))
        available = max(0, free - self.state['reserve'] - outstanding)
        selected = plan(rows, self.state['count'], self.state['cap'], available)
        low_disk = free < self.state['reserve'] + outstanding
        if low_disk:
            self.alert('disk', 'Недостаточно свободного места с учётом резерва и незавершённых серий. Загрузки приостановлены; после освобождения места продолжатся.')
        else:
            self.state['alerts'].pop('disk', None)
        for s in self.ordered_seasons():
            h = s['hash']
            if h not in all_files or h in blocked or s.get('error'):
                continue
            files = all_files[h]
            by_index = {f['index']: f for f in files}
            keep = set()
            for e in s['episodes']:
                if h + ':' + str(e['index']) in selected:
                    e['selected'] = True
                    keep.add(e['index'])
                if finished(e) or not e['selected']:
                    continue
                f = by_index[e['index']]
                if f['progress'] >= 1 and not e['published']:
                    src, dst = self.source(s, e), self.dest(s, e)
                    if not src.is_file() or src.stat().st_size != e['size']:
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if dst.exists():
                        if not os.path.samefile(src, dst):
                            raise APIError('В библиотеке уже существует другой файл с таким именем.')
                    else:
                        os.link(src, dst)  # one inode: publication does not double disk usage
                    e['published'] = True
                    self.refresh_needed = True
            self.store.save()  # reservations precede enabling any additional files
            self.q.priorities(h, files, keep)
            needs_download = any(by_index[index]['progress'] < 1 for index in keep)
            s['download_idle'] = bool(keep) and not needs_download
            if needs_download and not low_disk:
                self.q.post('start', hashes=h)
            else:
                self.stop(s)
        for s in low_seasons(self.state['seasons'], self.state['notify']):
            left = sum(episode_count(e) for e in s['episodes'] if not finished(e))
            delivered = self.broadcast(f"{s['title']}: в добавленной раздаче осталось {left} непросмотренных серий.\nПришли ответом magnet следующего сезона ({last_season(s) + 1}).\nПродолжение можно отключить кнопкой в меню раздачи (/files).",
                                       s.setdefault('notification_delivered', {}), reply_target=s['hash'])
            for uid, mid in delivered.items():
                if mid is not None:
                    self.state['replies'][f'{uid}:{mid}'] = s['hash']
            s['notified'] = True
            self.store.save()
        if rows and not selected and not low_disk and any(not finished(e) for _, e in rows):
            self.alert('limit', 'Следующий файл не помещается в лимит серий, общий объём ГБ или свободное место. Проверь /limit и /reserve. Сдвоенный файл занимает два места в лимите серий.')
        else:
            self.state['alerts'].pop('limit', None)
        if self.refresh_needed:
            self.j.refresh()
            self.refresh_needed = False
        self.store.save()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    store = Store(os.environ.get('STATE_FILE', '/state/state.json'))
    worker = Worker(store, Qbit(), Jellyfin(), Telegram())
    next_tick = 0
    while True:
        try:
            worker.q.login()
            for update in worker.tg.updates(worker.state['offset']):
                message = update.get('message', {})
                try:
                    if 'callback_query' in update:
                        worker.callback(update['callback_query'])
                    else:
                        worker.command(message)
                except (ValueError, OverflowError) as exc:
                    worker.reply_error(message, exc)
                worker.state['offset'] = update['update_id'] + 1
                store.save()
            worker.search_poll()
            try:
                worker.follow_cycle()
            except Exception:
                LOG.exception('Ошибка проверки подписок; медиаочередь продолжает работу.')
            if time.monotonic() >= next_tick:
                worker.tick()
                next_tick = time.monotonic() + 20
            if worker.state['alerts'].pop('service', None):
                store.save()
                LOG.info('Связь с сервисами восстановлена.')
        except (APIError, OSError, KeyError, ValueError) as exc:
            worker.q.logged_in = False
            # Fail closed: do not let incomplete reservations exhaust the disk while control is down.
            for s in worker.state['seasons']:
                try:
                    worker.stop(s)
                except (APIError, OSError):
                    pass
            LOG.error('Цикл остановлен: %s%s', type(exc).__name__, ': ' + str(exc) if isinstance(exc, APIError) else '')
            try:
                worker.alert('service', 'Очередь приостановлена: один из сервисов недоступен или настройки некорректны. Проверь Jellyfin, qBittorrent и .env. Повторю автоматически.')
            except (APIError, OSError):
                pass
            time.sleep(10)


if __name__ == '__main__':
    main()
