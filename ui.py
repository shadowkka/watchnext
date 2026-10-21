"""Private Telegram menus. Callback capabilities are persisted and scoped to a user."""
import secrets
import shutil
from html import escape
import time

from core import GB, episode_label, last_season, parse_start, episode_count
from net import APIError, RichText
from search_ui import SearchUI
from subscriptions import SubscriptionUI

COMMANDS = [
    ('search', 'Найти фильм или сериал'), ('subscriptions','Подписки на новые серии'), ('follow','Подписаться на сериал'), ('menu', 'Главное меню'), ('add', 'Magnet — добавить по ссылке'),
    ('files', 'Раздачи и файлы'), ('status', 'Состояние очереди'),
    ('from', 'Выбрать начальную серию'), ('limit', 'Лимиты загрузки'),
    ('notify', 'Когда предлагать продолжение'), ('reserve', 'Резерв места на диске'),
    ('pause', 'Приостановить очередь'), ('resume', 'Продолжить очередь'),
    ('retry', 'Повторить или восстановить раздачу'), ('finish', 'Не предлагать продолжение'),
    ('invite', 'Пригласить близкого'), ('users', 'Управление доступом'),
    ('cancel', 'Отменить текущий ввод'),
]


class MenuUI(SearchUI, SubscriptionUI):
    def ui_button(self, uid, label, *action):
        tokens = self.state.setdefault('buttons', {})
        now = time.time()
        for key, item in list(tokens.items()):
            if item['expires'] < now:
                tokens.pop(key, None)
        own = [key for key, item in tokens.items() if item['uid'] == uid]
        for key in own[:-300]:
            tokens.pop(key, None)
        key = secrets.token_urlsafe(9)
        item = {'uid': uid, 'action': list(action), 'expires': now + 48 * 3600}
        # A button for an old, replaced pack must not modify its replacement.
        for pack in self.state['seasons']:
            if pack['hash'] in action:
                item['pack'] = [pack['hash'], pack.get('created')]
                break
        if len(action)>1 and str(action[0]).startswith('follow_'):
            sub=self.state.get('subscriptions',{}).get(str(action[1]))
            if sub:item['subscription']=[str(action[1]),sub.get('generation')]
        tokens[key] = item
        return {'text': label[:100], 'callback_data': 'ui:' + key}

    def ui_show(self, uid, text, rows):
        markup = {'inline_keyboard': [[self.ui_button(uid, *button) for button in row] for row in rows]}
        self.store.save()  # persist capabilities before exposing the buttons
        mid = getattr(self, '_ui_message', None)
        if mid is not None:
            try:
                self.tg.edit(text, uid, mid, markup)
                return mid
            except APIError as exc:
                # Expired/deleted messages can still be navigated in a fresh message.
                if str(exc) != 'HTTP 400':
                    raise
        return self.tg.send(text, chat_id=uid, reply_markup=markup)

    def ui_home(self, uid, text=None):
        rows = [[('🧲 Magnet', 'command', '/add'), ('📂 Раздачи и файлы', 'packs', 'files', 0)],
                [('📊 Очередь', 'status'), ('⚙️ Настройки', 'settings')],
                [('▶️ Продолжить' if self.state['paused'] else '⏸ Пауза',
                  'command', '/resume' if self.state['paused'] else '/pause')]]
        rows.insert(0, [('🔎 Найти фильм или сериал', 'search_new')])
        rows.insert(1,[('🔔 Подписки на сериалы','follow_list',0)])
        search = self.state.get('searches', {}).get(str(uid))
        if search and search.get('results') and search['expires'] > time.time():
            rows.append([('Результаты последнего поиска', 'search_page', search['id'], 0)])
        if uid == self.tg.owner:
            rows.append([('👥 Доступ для семьи', 'users', 0)])
        if str(uid) in self.state.get('dialogs', {}):
            rows.append([('Продолжить добавление', 'continue_add'), ('Отменить ввод', 'command', '/cancel')])
        self.ui_show(uid, text or 'Управление общей очередью. Выбери действие:', rows)

    def ui_nav(self):
        return [('🏠 Главное меню', 'home')]

    def ui_status(self, uid, page=0):
        packs = self.ordered_seasons()
        page = min(max(0,page),max(0,(len(packs)-1)//4))
        limits = []
        if self.state['count'] is not None: limits.append(f"{self.state['count']} серий")
        if self.state['cap'] is not None: limits.append(f"{self.state['cap']/GB:g} ГБ")
        free = shutil.disk_usage(self.root).free/GB
        lines = ['<b>📊 Общая очередь</b>' + (' · ⏸ Пауза' if self.state['paused'] else ''),
                 'Лимит: '+' · '.join(limits), f"Свободно: {free:.1f} ГБ · резерв {self.state['reserve']/GB:g} ГБ"]
        rows = []
        for p in packs[page*4:(page+1)*4]:
            live = [e for e in p['episodes'] if not (e['watched'] or e.get('discarded') or e.get('skipped_start'))]
            ready = sum(episode_count(e) for e in live if e['published'])
            downloading = sum(episode_count(e) for e in live if e['selected'] and not e['published'])
            left = sum(episode_count(e) for e in live)
            state = {'metadata':'Получаю список файлов','ready':'Ожидает загрузки','needcheck':'Обновляю файлы','checking':'Проверяю файлы','retiring':'Освобождаю место','complete':'Завершено','missing':'Удалена из qBittorrent'}[p['phase']]
            if p.get('download_idle') and p['phase']=='ready': state='Скачано · раздача остановлена'
            elif downloading: state=f'Загружается: {downloading}'
            if p.get('error'): state='Ошибка: '+p['error']
            label = '🎬' if p.get('kind')=='movie' else '📺'
            lines += [f"\n<b>{label} {escape(p['title'][:100])}</b>",f"Готово: {ready} · Осталось: {left}",escape(state)]
            if p.get('kind')!='movie': lines.append(f"Начало: S{p['season']:02d}E{p.get('start_episode',1):02d}")
            reason = self.waiting_reason(p)
            if reason: lines.append('<i>'+escape(reason[:180])+'</i>')
            rows.append([(p['title'][:60]+' · файлы','files',p['hash'],0)])
        if not packs: lines.append('\nОчередь пока пуста.')
        nav = []
        if page: nav.append(('← Назад','status',page-1))
        if (page+1)*4<len(packs): nav.append(('Дальше →','status',page+1))
        if nav: rows.append(nav)
        rows += [[('⚙️ Настройки','settings')],self.ui_nav()]
        self.ui_show(uid,RichText('\n'.join(lines)),rows)

    def ui_settings(self, uid):
        self.ui_show(uid, '\n'.join(self.status().split('\n')[:2]), [
            [('Лимит серий и ГБ', 'limit')], [('Поиск: качество и фильтры', 'search_options', '')], [('Когда предлагать следующий сезон', 'setting', 'notify')],
            [('Резерв свободного места', 'setting', 'reserve')], self.ui_nav()])

    def ui_packs(self, uid, purpose='files', page=0):
        packs = self.ordered_seasons()
        if purpose in ('from', 'finish'):
            packs = [p for p in packs if p.get('kind') != 'movie']
        page = min(max(0, page), max(0, (len(packs) - 1) // 8))
        rows = []
        for p in packs[page * 8:page * 8 + 8]:
            seasons = sorted({e.get('season', p['season']) for e in p['episodes']}) or [p['season']]
            detail = 'фильм' if p.get('kind') == 'movie' else 'сезон ' + ', '.join(map(str, seasons))
            if p.get('error'):
                detail += ' · ошибка'
            elif p['phase'] == 'missing':
                detail += ' · удалена'
            elif p['phase'] == 'complete':
                detail += ' · завершена'
            rows.append([(p['title'][:65] + ' · ' + detail, 'pick', purpose, p['hash'])])
        nav = []
        if page:
            nav.append(('← Назад', 'packs', purpose, page - 1))
        if (page + 1) * 8 < len(packs):
            nav.append(('Дальше →', 'packs', purpose, page + 1))
        if nav:
            rows.append(nav)
        rows += [[('🧲 Magnet', 'command', '/add')], self.ui_nav()]
        prompt = {'files': 'Выбери раздачу:', 'from': 'Для какого сериала изменить начало?',
                  'retry': 'Какую раздачу повторить или восстановить?',
                  'finish': 'Для какого сериала больше не предлагать продолжение?'}[purpose]
        self.ui_show(uid, prompt if packs else 'Здесь пока нет раздач. Можно добавить сериал или фильм.', rows)

    def ui_pack(self, h):
        return self.season_by_id(h)

    def ui_files(self, uid, h, page=0):
        p = self.ui_pack(h)
        eps = p['episodes']
        page = min(max(0, page), max(0, (len(eps) - 1) // 8))
        lines = ['<b>'+escape(p['title'])+'</b>']
        if p['phase'] == 'ready' and p.get('download_idle'):
            lines.append('Выбранные файлы скачаны. Раздача остановлена; после просмотра следующая серия загрузится автоматически.')
        reason = self.waiting_reason(p)
        if reason:
            lines.append(escape(reason))
        if p.get('kind') != 'movie':
            lines.append(f"Начало: сезон {p['season']}, серия {p.get('start_episode', 1)}.")
        if p.get('error'):
            lines.append('Ошибка: ' + escape(p['error']))
        if p['phase'] == 'missing':
            lines.append('Раздача удалена из qBittorrent. Её можно восстановить кнопкой ниже.')
        for e in eps[page * 8:page * 8 + 8]:
            status = ('просмотрено' if e['watched'] else 'удалено' if e.get('discarded') else
                      'пропущено' if e.get('skipped_start') else 'готово к просмотру' if e['published'] else
                      'загружается' if e['selected'] else 'ожидает места')
            label = 'Фильм' if p.get('kind') == 'movie' else episode_label(e, p['season'])
            lines.append(f"\n<b>{label}</b> · {e['size'] / GB:.2f} ГБ · {status}\n<i>{escape(e['name'][:60])}</i>")
        if not eps:
            lines.append('Список файлов ещё не получен.')
        rows, nav = [], []
        if page:
            nav.append(('← Файлы', 'files', h, page - 1))
        if (page + 1) * 8 < len(eps):
            nav.append(('Файлы →', 'files', h, page + 1))
        if nav:
            rows.append(nav)
            lines.append(f'\nСтраница {page + 1} из {(len(eps) + 7) // 8}')
        if p.get('kind') != 'movie':
            rows.append([('С какой серии начать', 'seasons', h)])
            rows.append([('🔔 Следить за новыми сериями','follow_pack',h)])
            rows.append([('Добавить продолжение', 'next', h), ('Не предлагать продолжение', 'finish', h)])
        rows += [[('⚙️ Лимиты загрузки', 'limit')], [('Восстановить' if p['phase'] == 'missing' else 'Повторить обработку', 'retry', h)],
                 [('← Все раздачи', 'packs', 'files', 0)], self.ui_nav()]
        self.ui_show(uid, RichText('\n'.join(lines)), rows)

    def ui_seasons(self, uid, h, page=0):
        p = self.ui_pack(h)
        if p.get('kind') == 'movie':
            raise ValueError('Для фильма сезон и серия не нужны.')
        seasons = sorted({e.get('season', p['season']) for e in p['episodes']})
        page = min(max(0, page), max(0, (len(seasons) - 1) // 10))
        rows = [[(f'Сезон {sn}', 'episodes', h, sn, 0)] for sn in seasons[page * 10:page * 10 + 10]]
        nav = []
        if page:
            nav.append(('← Назад', 'seasons', h, page - 1))
        if (page + 1) * 10 < len(seasons):
            nav.append(('Дальше →', 'seasons', h, page + 1))
        if nav:
            rows.append(nav)
        self.ui_show(uid, p['title'] + ': выбери начальный сезон. Более ранние серии будут исключены из очереди.',
                     rows + [[('Ввести сезон и серию', 'input', 'start', h)], [('← Раздача', 'files', h, 0)]])

    def ui_episodes(self, uid, h, sn, page=0):
        p = self.ui_pack(h)
        eps = [e for e in p['episodes'] if e.get('season', p['season']) == sn]
        page = min(max(0, page), max(0, (len(eps) - 1) // 12))
        rows = []
        for e in eps[page * 12:page * 12 + 12]:
            end = e.get('end_number', e['number'])
            label = f"Серия {e['number']}" if end == e['number'] else f"Серии {e['number']}–{end} (один файл)"
            rows.append([(label, 'start', h, sn, e['number'])])
        nav = []
        if page:
            nav.append(('← Назад', 'episodes', h, sn, page - 1))
        if (page + 1) * 12 < len(eps):
            nav.append(('Дальше →', 'episodes', h, sn, page + 1))
        if nav:
            rows.append(nav)
        rows += [[('Ввести сезон и серию', 'input', 'start', h)], [('← Сезоны', 'seasons', h)]]
        self.ui_show(uid, f"{p['title']} · сезон {sn}. С какой серии начать? Более ранние серии будут исключены из очереди.", rows)

    def ui_limit(self, uid):
        self.ui_show(uid, '\n'.join(self.status().split('\n')[:2]) + '\nКак ограничить загрузку?', [
            [('По числу серий', 'input', 'count')], [('По гигабайтам', 'input', 'cap')],
            [('По числу серий и гигабайтам', 'input', 'both')], [('← Настройки', 'settings')]])

    def ui_input(self, uid, kind, h=None):
        if kind == 'start':
            self.ui_pack(h)
        self.state.setdefault('ui_input', {})[str(uid)] = {'kind': kind, 'hash': h, 'created': self.ui_pack(h).get('created') if h else None}
        prompt = {'count': 'Сколько серий хранить одновременно? Фильмы учитываются только в общем лимите ГБ. Введи число от 1 до 1000.',
                  'cap': 'Сколько ГБ выделить на серии и фильмы вместе? Введи число, например 15 или 7,5.',
                  'both': 'Введи количество серий и ГБ через пробел, например: 5 15.',
                  'notify': 'За сколько непросмотренных серий до конца предлагать следующий сезон? Введи число (0 — когда всё просмотрено).',
                  'reserve': 'Сколько ГБ оставлять свободными на диске? Введи число.',
                  'start': 'Введи сезон и серию через пробел, например: 2 5.'}[kind]
        presets = {'count': ['3', '5', '10'], 'cap': ['5', '10', '20'], 'both': ['5 10', '5 20'],
                   'notify': ['0', '2', '5'], 'reserve': ['2', '5', '10'], 'start': []}[kind]
        rows = [[(v, 'value', kind, h, v) for v in presets]] if presets else []
        rows += [[('Отмена', 'home')]]
        self.ui_show(uid, prompt, rows)

    def ui_value(self, uid, kind, h, value):
        if kind=='follow_query':
            self.follow_begin(uid,value)
            return
        if kind=='follow_voice':
            key=self.state['ui_input'][str(uid)]['key']
            if not 1<=len(value)<=100:raise ValueError('Название студии: от 1 до 100 символов.')
            self.state['ui_input'].pop(str(uid),None)
            self.follow_action(uid,'follow_set',key,'voice',value.strip())
            return
        if kind.startswith('search_'):
            self.search_text(uid, kind, value)
            return
        value = value.replace(',', '.')
        commands = {'count': '/limit episodes ', 'cap': '/limit gb ', 'both': '/limit both ',
                    'notify': '/notify ', 'reserve': '/reserve '}
        if kind == 'start':
            p = self.ui_pack(h)
            pending = self.state.get('ui_input', {}).get(str(uid))
            if pending and pending.get('created') != p.get('created'):
                raise ValueError('Раздача была заменена. Открой /files и выбери её заново.')
            sn, en = parse_start(value)
            self.set_start(p, sn, en)
            self.ui_autostart()
            self.ui_files(uid, h)
        else:
            command = commands[kind] + value
            try:
                self.command(self.ui_message(uid, command))
            except (ValueError, OverflowError):
                self.state.setdefault('ui_input', {})[str(uid)] = {'kind': kind, 'hash': h}
                self.store.save()
                raise
        self.state.setdefault('ui_input', {}).pop(str(uid), None)
        self.store.save()

    def ui_users(self, uid, page=0):
        if uid != self.tg.owner:
            raise ValueError('Управлять доступом может только владелец бота.')
        members = list(self.state.get('members', {}).items())
        page = min(max(0, page), max(0, (len(members) - 1) // 8))
        rows = [[('Отозвать доступ: ' + info['name'], 'revoke', mid)] for mid, info in members[page * 8:page * 8 + 8]]
        nav = []
        if page:
            nav.append(('← Назад', 'users', page - 1))
        if (page + 1) * 8 < len(members):
            nav.append(('Дальше →', 'users', page + 1))
        if nav:
            rows.append(nav)
        rows += [[('Пригласить близкого', 'command', '/invite')], self.ui_nav()]
        self.ui_show(uid, 'Общая очередь и история просмотра.\nВладелец — вы.\n' +
                     ('Выбери пользователя, чтобы отозвать доступ.' if members else 'Других пользователей пока нет.'), rows)

    @staticmethod
    def ui_message(uid, text):
        return {'from': {'id': uid}, 'chat': {'id': uid, 'type': 'private'}, 'text': text}

    def ui_autostart(self):
        if self.state.get('pause_reason') != 'user':
            self.state['paused'] = False
            self.state.pop('pause_reason', None)

    def ui_command(self, uid, text):
        args = text.split()
        if not args:
            return False
        cmd = args[0].split('@')[0]
        if len(args) != 1:
            return False
        menus = {'/menu': 'home', '/start': 'home', '/help': 'home', '/status': 'status',
                 '/files': 'packs', '/from': 'packs', '/retry': 'packs', '/finish': 'packs',
                 '/limit': 'limit', '/notify': 'setting', '/reserve': 'setting',
                 '/users': 'users', '/revoke': 'users'}
        if cmd not in menus:
            return False
        action = menus[cmd]
        params = {'packs': [cmd[1:], 0], 'setting': [cmd[1:]], 'users': [0]}.get(action, [])
        self.state.setdefault('ui_input', {}).pop(str(uid), None)
        self.ui_action(uid, action, *params)
        return True

    def ui_action(self, uid, action, *args):
        if action.startswith('follow_'):
            if action=='follow_pack':
                self.follow_begin(uid,self.ui_pack(args[0])['title'],library_title=self.ui_pack(args[0])['title'])
                return
            return self.follow_action(uid,action,*args)
        if action.startswith('search_'):
            return self.search_action(uid, action, *args)
        if action == 'home':
            self.state.setdefault('ui_input', {}).pop(str(uid), None)
            # Keep an in-progress /add unless explicitly cancelled or another flow starts.
            self.ui_home(uid)
        elif action == 'command':
            self.command(self.ui_message(uid, args[0]))
        elif action == 'continue_add':
            d = self.state.get('dialogs', {}).get(str(uid))
            if not d:
                self.ui_home(uid, 'Добавление уже завершено или отменено.')
            else:
                prompts = {'kind': 'Что добавляем?', 'title': 'Как назвать?',
                           'season': 'С какого сезона начать? Введи номер или «Авто».',
                           'episode': 'С какой серии начать? Введи номер.', 'magnet': 'Пришли magnet-ссылку.'}
                choices = {'kind': ['Сериал', 'Фильм'], 'season': ['Авто'], 'episode': ['1']}.get(d['step'])
                self.prompt(uid, prompts[d['step']], choices)
        elif action == 'status':
            self.ui_status(uid, args[0] if args else 0)
        elif action == 'settings':
            self.ui_settings(uid)
        elif action == 'packs':
            self.ui_packs(uid, *args)
        elif action == 'pick':
            purpose, h = args
            action = {'files': 'files', 'from': 'seasons', 'retry': 'retry', 'finish': 'finish'}[purpose]
            self.ui_action(uid, action, h, *([0] if action == 'files' else []))
        elif action == 'files':
            self.ui_files(uid, *args)
        elif action == 'seasons':
            self.ui_seasons(uid, *args)
        elif action == 'episodes':
            self.ui_episodes(uid, *args)
        elif action == 'start':
            h, sn, en = args
            self.set_start(self.ui_pack(h), sn, en)
            self.ui_autostart()
            self.ui_files(uid, h)
        elif action == 'retry':
            p = self.ui_pack(args[0])
            if p['phase'] in ('missing', 'complete'):
                text = self.add(p['title'], p['season'], p['magnet'], p.get('start_episode', 1),
                                p.get('kind', 'series'), p.get('auto_season', False))
            else:
                p.pop('error', None)
                if not p['episodes']:
                    p['phase'] = 'metadata'
                self.state['alerts'].pop('pack:' + p['hash'], None)
                text = p['title'] + ': повторю обработку.'
            self.ui_home(uid, text)
        elif action == 'finish':
            p = self.ui_pack(args[0])
            p['notified'] = True
            self.ui_home(uid, p['title'] + ': продолжение больше не предлагаю. Его можно добавить вручную через меню раздачи.')
        elif action == 'next':
            p = self.ui_pack(args[0])
            self.state.setdefault('ui_input', {}).pop(str(uid), None)
            self.state.setdefault('dialogs', {})[str(uid)] = {
                'step': 'magnet', 'kind': 'series', 'title': p['title'],
                'season': last_season(p) + 1, 'start_episode': 1, 'auto_season': False}
            self.store.save()
            self.prompt(uid, f"{p['title']}: пришли magnet следующего сезона ({last_season(p) + 1}).")
        elif action == 'limit':
            self.ui_limit(uid)
        elif action in ('input', 'setting'):
            # Settings input takes precedence over any older add dialog.
            self.state.setdefault('dialogs', {}).pop(str(uid), None)
            self.ui_input(uid, *args)
        elif action == 'value':
            self.ui_value(uid, *args)
        elif action == 'users':
            self.ui_users(uid, *args)
        elif action in ('revoke', 'revoke_yes'):
            if uid != self.tg.owner:
                raise ValueError('Управлять доступом может только владелец бота.')
            target = str(args[0])
            member = self.state.get('members', {}).get(target)
            if not member or int(target) == self.tg.owner:
                raise ValueError('Этот пользователь больше не в списке приглашённых.')
            if action == 'revoke':
                self.ui_show(uid, 'Отозвать доступ у ' + member['name'] + '?',
                             [[('Да, отозвать', 'revoke_yes', target)], [('Отмена', 'users', 0)]])
            else:
                self.state['members'].pop(target, None)
                self.state.get('searches', {}).pop(target, None)
                self.state.get('follow_dialogs',{}).pop(target,None)
                self.state['buttons'] = {k: v for k, v in self.state.get('buttons', {}).items() if str(v['uid']) != target}
                self.state.setdefault('dialogs', {}).pop(target, None)
                self.state.setdefault('ui_input', {}).pop(target, None)
                self.ui_users(uid)
        else:
            raise ValueError('Это меню устарело. Открой /menu заново.')
        self.store.save()

    def callback(self, query):
        uid = query.get('from', {}).get('id')
        message = query.get('message', {})
        valid = (uid in self.users and message.get('chat', {}).get('id') == uid and
                 message.get('chat', {}).get('type') == 'private')
        data = query.get('data', '')
        item = self.state.get('buttons', {}).get(data[3:]) if data.startswith('ui:') else None
        valid = valid and item is not None and item['uid'] == uid and item['expires'] > time.time()
        if valid and item.get('pack'):
            h, created = item['pack']
            valid = any(p['hash'] == h and p.get('created') == created for p in self.state['seasons'])
        if valid and item.get('subscription'):
            key,generation=item['subscription']
            sub=self.state.get('subscriptions',{}).get(key)
            valid=bool(sub) and sub.get('generation')==generation
        try:
            self.tg.answer(query['id'], '' if valid else 'Кнопка устарела или недоступна. Открой /menu.', not valid)
        except APIError as exc:
            if str(exc) != 'HTTP 400':
                raise
        if not valid:
            return
        self._ui_message = message.get('message_id')
        try:
            if item['action'][0] != 'value':
                self.state.setdefault('ui_input', {}).pop(str(uid), None)
            self.ui_action(uid, *item['action'])
        except (ValueError, OverflowError) as exc:
            self.ui_home(uid, str(exc))
        finally:
            self._ui_message = None
