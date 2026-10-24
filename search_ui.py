"""Family-scoped background searches. Only the main worker mutates queue state."""
import re
from html import escape
import secrets
import time
from queue import Queue, Empty, Full
from concurrent.futures import ThreadPoolExecutor

from core import GB, slug, parse_start
from net import APIError, RichText
from search_client import Jackett, plain, voices, release_numbers, resolution, voice_tags


def suggested_title(title, query, kind):
    title = re.sub(r'^(?:\[[^]]*\]\s*)+', '', title).split('/')[0].strip()
    title = re.split(r'(?i)\b(?:сезон|season|s\d{1,3}e\d)', title)[0].strip(' .,:-')
    title = title or query
    year = re.search(r'\b(19\d{2}|20\d{2})\b', title)
    if kind == 'movie' and not year:
        year = re.search(r'\b(19\d{2}|20\d{2})\b', query)
        if year:
            title += ' (' + year[0] + ')'
    return slug(plain(title, 95))


def progressive_query(events, uid, sid, generation, query, kind):
    def update(result, completed, total):
        try:events.put_nowait((uid,sid,generation,result))
        except Full:pass  # The final Future always carries the complete result.
    return Jackett().search(query, kind, on_update=update)


class SearchUI:
    def search_session(self, uid, sid):
        session = self.state.get('searches', {}).get(str(uid))
        if not session or session['id'] != sid or session['expires'] < time.time():
            raise ValueError('Результаты поиска устарели. Начни новый поиск через /search.')
        return session

    def search_start(self, uid, query=''):
        if len(query) > 120:
            raise ValueError('Запрос слишком длинный. Укажи название до 120 символов.')
        self.state.setdefault('dialogs', {}).pop(str(uid), None)
        self.state.setdefault('ui_input', {}).pop(str(uid), None)
        session = {'id':secrets.token_urlsafe(8), 'query':query, 'phase':'kind', 'expires':time.time()+86400}
        self.state.setdefault('searches', {})[str(uid)] = session
        self.ui_show(uid, ('Ищем: ' + query + '\n' if query else '') + 'Что ищем?', [
            [('🎬 Фильм', 'search_kind', session['id'], 'movie'), ('📺 Сериал', 'search_kind', session['id'], 'series')],
            self.ui_nav()])

    def search_input(self, uid, sid, kind):
        self.search_session(uid, sid)
        if kind == 'start':
            self.search_seasons(uid, sid)
            return
        self.state.setdefault('dialogs', {}).pop(str(uid), None)
        self.state.setdefault('ui_input', {})[str(uid)] = {'kind':'search_' + kind, 'sid':sid}
        prompts = {'query':'Напиши название для поиска. Можно добавить год, сезон или качество.',
                   'title':'Как назвать в библиотеке Jellyfin? Для фильма можно добавить год в скобках.',
                   'start':'С какого сезона и серии начать? Введи два числа, например 2 5. Или напиши «Авто» — с начала раздачи.'}
        self.ui_show(uid, prompts[kind], [[('← К поиску', 'search_page', sid, 0)], self.ui_nav()])

    def search_text(self, uid, kind, text):
        pending = self.state.get('ui_input', {}).get(str(uid)) or {}
        sid = pending.get('sid')
        s = self.search_session(uid, sid)
        if kind == 'search_query':
            query = plain(text, 121)
            if not 2 <= len(query) <= 120:
                raise ValueError('Введи название длиной от 2 до 120 символов.')
            s['query'] = query
            self.search_run(uid, sid)
        elif kind == 'search_title':
            s['title'] = slug(text)
            self.search_details(uid, sid, s['chosen'], preserve=True)
        elif kind == 'search_start':
            if s.get('kind') != 'series':
                raise ValueError('Для фильма сезон и серия не нужны.')
            if text.casefold() == 'авто':
                s.update(season=1, start_episode=1, auto_season=True)
            else:
                sn, en = parse_start(text)
                s.update(season=sn, start_episode=en, auto_season=False)
            self.search_details(uid, sid, s['chosen'], preserve=True)
        self.state.setdefault('ui_input', {}).pop(str(uid), None)
        self.store.save()

    def ui_jobs_pending(self):
        return bool(self.catalog_jobs()) or bool(self.search_jobs())

    def search_jobs(self):
        if not hasattr(self, '_search_jobs'):
            self._search_jobs = {}
        return self._search_jobs

    def search_submit(self, uid, sid, task, fn, *args):
        jobs = self.search_jobs()
        # Bound outstanding requests, including cancelled/superseded sessions.
        if any(key[0] == uid and not future.done() and not (task=='import' and key[2]=='query') for key, future in jobs.items()):
            raise ValueError('Предыдущий запрос ещё завершается. Подожди немного и повтори.')
        if len(jobs) >= 4:
            raise ValueError('Поиск сейчас занят. Повтори чуть позже.')
        if not hasattr(self, '_search_pool'):
            self._search_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='jackett')
        jobs[(uid, sid, task)] = self._search_pool.submit(fn, *args)

    def search_run(self, uid, sid):
        s = self.search_session(uid, sid)
        if not hasattr(self,'_search_events'):self._search_events=Queue(maxsize=128)
        generation=s.get('generation',0)+1
        self.search_submit(uid, sid, 'query', progressive_query, self._search_events, uid, sid, generation, s['query'], s['kind'])
        s['generation']=generation
        s.update(phase='working', error='', running=True, displayed=True, results=[], warnings=[])
        s['message_id'] = self.ui_show(uid, 'Ищу раздачи: ' + s['query'] + '\nЗагрузки и очередь продолжают работать.',
                                       [[('Отменить поиск', 'search_cancel', sid)], self.ui_nav()])
        self.store.save()

    def search_prefs(self, uid):
        prefs = self.state.setdefault('search_preferences', {}).setdefault(str(uid), {})
        prefs.pop('voice',None)  # migrate the old cross-query voice filter
        return prefs

    def search_visible(self, uid, session):
        p = {} if session.get('curated') else self.search_prefs(uid)
        rows = []
        for index, r in enumerate(session.get('results', [])):
            if p.get('quality','all') != 'all' and resolution(r['title']) != p['quality']: continue
            if session.get('voice','all') != 'all' and session['voice'] not in voice_tags(r['title']): continue
            if p.get('seeds',0) and (r['seeders'] is None or r['seeders'] < p['seeds']): continue
            if p.get('size',0) and (r['size'] is None or r['size'] > p['size']*GB): continue
            if session.get('season_filter') and session['season_filter'] not in release_numbers(r['title'])['seasons']:continue
            rows.append((index,r))
        if p.get('sort') in ('size-asc','size-desc'):
            rows.sort(key=lambda pair:(pair[1].get('size') is None,(-1 if p['sort']=='size-desc' else 1)*(pair[1].get('size') or 0),pair[0]))
            return rows
        rows.sort(key=lambda pair: (0 if resolution(pair[1]['title'])==p.get('prefer') else 1,
                                   -(pair[1]['seeders'] if pair[1]['seeders'] is not None else -1), pair[0]))
        return rows

    def search_preference_label(self, p, field):
        value = p.get(field)
        if field=='sort':return {'size-asc':'размер ↑','size-desc':'размер ↓'}.get(value,'по сидам')
        if field=='prefer': return value or 'по сидам'
        if field in ('quality','voice'): return value if value and value!='all' else 'все'
        if field=='seeds': return str(value) if value else 'без ограничения'
        return f'{value} ГБ' if value else 'без ограничения'

    def search_options(self, uid, sid=''):
        if sid: self.search_session(uid, sid)
        p = self.search_prefs(uid)
        rows = [[('Выше: '+self.search_preference_label(p,'prefer'), 'search_filter',sid,'prefer',0)],
                [('Качество: '+self.search_preference_label(p,'quality'), 'search_filter',sid,'quality',0)],
                [('Минимум сидов: '+self.search_preference_label(p,'seeds'), 'search_filter',sid,'seeds',0)],
                [('Размер до: '+self.search_preference_label(p,'size'), 'search_filter',sid,'size',0)],
                [('Сбросить фильтры', 'search_filters_reset',sid)]]
        rows.insert(0,[('Сортировка: '+self.search_preference_label(p,'sort'),'search_filter',sid,'sort',0)])
        if sid and self.search_session(uid,sid).get('kind')=='series':
            rows.insert(1,[('Сезон: '+str(self.search_session(uid,sid).get('season_filter') or 'все'),'search_filter',sid,'season',0)])
        if sid:
            rows.insert(2,[('Озвучка в этом поиске: '+self.search_preference_label({'voice':self.search_session(uid,sid).get('voice')},'voice'), 'search_filter',sid,'voice',0)])
        rows += [[('← Результаты', 'search_page', sid, 0)]] if sid else [[('← Настройки', 'settings')]]
        self.ui_show(uid, 'Качество, сиды и размер сохраняются для следующих запросов. Озвучка выбирается только для текущего поиска.\nПриоритет поднимает качество выше, фильтры скрывают неподходящие раздачи. Внутри группы — сначала больше сидов.\nОзвучка определяется по названию раздачи. При ограничении размера или сидов результаты без этих данных скрываются.', rows)

    def search_filter_choices(self, uid, sid, field):
        qualities = ['2160p','1080p','1080i','720p','576p','480p']
        if field=='sort':return [('Больше сидов','seeds'),('Размер: меньше сначала','size-asc'),('Размер: больше сначала','size-desc')]
        if field=='season':
            if not sid or self.search_session(uid,sid).get('kind')!='series':raise ValueError('Сезоны доступны для сериалов.')
            seasons=sorted({n for r in self.search_session(uid,sid).get('results',[]) for n in release_numbers(r['title'])['seasons']})
            return [('Все сезоны',0)]+[(f'Сезон {n}',n) for n in seasons]
        if field=='prefer': return [('По числу сидов','')]+[(q,q) for q in qualities]
        if field=='quality': return [('Любое качество','all')]+[(q,q) for q in qualities]+[('Не указано','unknown')]
        if field=='voice':
            if not sid: raise ValueError('Озвучку можно выбрать в результатах текущего поиска.')
            session = self.search_session(uid,sid)
            candidates = self.search_visible(uid,dict(session,voice='all'))
            counts = {}
            for _,r in candidates:
                for v in voice_tags(r['title']): counts[v]=counts.get(v,0)+1
            labels = sorted(counts,key=str.casefold)
            return [('Любая озвучка','all')]+[(f'{v} · {counts[v]}',v) for v in labels]
        if field=='seeds': return [('Без ограничения',0)]+[(str(n),n) for n in (1,5,10,20,50,100)]
        if field=='size': return [('Без ограничения',0)]+[(f'{n} ГБ',n) for n in (1,2,5,10,15,20,30,50,100)]
        raise ValueError('Неизвестный фильтр поиска.')

    def search_filter_menu(self, uid, sid, field, page=0):
        options = self.search_filter_choices(uid,sid,field)
        page = min(max(0,page),(len(options)-1)//10)
        rows = [[(label,'search_filter_set',sid,field,value)] for label,value in options[page*10:(page+1)*10]]
        nav = []
        if page: nav.append(('← Назад','search_filter',sid,field,page-1))
        if (page+1)*10 < len(options): nav.append(('Дальше →','search_filter',sid,field,page+1))
        if nav: rows.append(nav)
        rows.append([('← Фильтры и порядок','search_options',sid)])
        prompt = {'sort':'Как сортировать раздачи?', 'season':'Показать раздачи, содержащие этот сезон (включая сборники):', 'prefer':'Какое качество показывать первым?', 'quality':'Оставить только это качество:',
                  'voice':'Оставить раздачи с этой озвучкой:', 'seeds':'Минимальное число сидов:', 'size':'Максимальный размер всей раздачи:'}[field]
        if field=='voice' and len(options)==1: prompt += '\nСначала выполни поиск — появятся озвучки из его результатов.'
        if field=='size': prompt += '\nДля сериала это размер всей раздачи, а не одной серии. Лимиты загрузки очереди задаются отдельно.'
        self.ui_show(uid,RichText('<b>'+escape(prompt)+'</b>'),rows)

    def search_page(self, uid, sid, page=0):
        s = self.search_session(uid, sid)
        s['displayed']=True
        s['page']=page
        phase = s['phase']
        if phase in ('working', 'importing'):
            self.ui_show(uid, 'Запрос ещё выполняется.', [[('Обновить', 'search_page', sid, 0)], self.ui_nav()])
            return
        if phase == 'error':
            self.ui_show(uid, s.get('error', 'Поиск прервался.') + '\nЗапрос: ' + s['query'],
                         [[('Повторить поиск', 'search_run', sid)], [('Новый поиск', 'search_new')], self.ui_nav()])
            return
        rows = self.search_visible(uid, s)
        if not rows:
            self.ui_show(uid, ('По текущим фильтрам раздач нет. Измени или сбрось фильтры.' if s.get('results') else 'По запросу «' + s['query'] + '» раздач не найдено. Попробуй другое название или проверь трекеры в Jackett.'),
                         [[('⚙️ Фильтры и порядок', 'search_options', sid)], [('Изменить запрос', 'search_input', sid, 'query')], [('Новый поиск', 'search_new')], self.ui_nav()])
            return
        page = min(max(0, page), (len(rows)-1)//5)
        prefs = self.search_prefs(uid)
        lines = [f"<b>🔎 {escape(s['query'])}</b>  ·  {len(rows)} раздач"]
        if s.get('running'):lines.append('Поиск продолжается на остальных трекерах. Раздачи уже можно выбирать.')
        if self.search_prefs(uid).get('sort') in ('size-asc','size-desc'):lines.append('Порядок: '+self.search_preference_label(prefs,'sort'))
        lines.append(('Сначала: '+self.search_preference_label(prefs,'prefer')+' · затем по сидам') if prefs.get('prefer') else 'Сначала больше сидов') if prefs.get('sort') not in ('size-asc','size-desc') else None
        active = [label+': '+self.search_preference_label(prefs,key) for key,label in [('quality','Качество'),('seeds','Сиды от'),('size','Размер до')] if prefs.get(key) not in (None,0,'all','')]
        if s.get('season_filter'):active.append('Сезон: '+str(s['season_filter']))
        if s.get('voice','all')!='all': active.append('Озвучка: '+s['voice'])
        if active: lines.append(escape(' · '.join(active)[:260]))
        if s.get('warnings'):
            lines.append(escape('Не ответили: ' + ', '.join(s['warnings'])))
        buttons = []
        for n in range(page*5, min(len(rows), page*5+5)):
            index, result = rows[n]
            size = f"{result['size']/GB:.2f} ГБ" if result['size'] is not None else 'размер неизвестен'
            seeds = str(result['seeders']) if result['seeders'] is not None else '?'
            peers = str(result['peers']) if result['peers'] is not None else '?'
            title = suggested_title(result['title'],s['query'],s['kind'])
            hints = release_numbers(result['title'])['seasons'] if s['kind']=='series' else []
            span = (' · S'+str(hints[0]).zfill(2)+('–'+str(hints[-1]).zfill(2) if len(hints)>1 else '')) if hints else ''
            lines.append(f"\n<b>{n+1}. {escape(title)}{span}</b>\n{escape(result['quality'])} · {size}\n🎙 {escape(voices(result['title'])[:130])}\nСиды: {seeds} · Пиры: {peers} · {escape(result['tracker'])}")
            buttons.append([(f"{n+1} · {result['quality']} · {size} · ↑{seeds}", 'search_pick', sid, index)])
        nav = []
        if page:
            nav.append(('← Назад', 'search_page', sid, page-1))
        if (page+1)*5 < len(rows):
            nav.append(('Дальше →', 'search_page', sid, page+1))
        if nav:
            buttons.append(nav)
        lines.append(f'\nСтраница {page+1} из {(len(rows)+4)//5}. Выбери раздачу кнопкой по её номеру.')
        buttons += [[('⚙️ Фильтры и порядок', 'search_options', sid)], [('Изменить запрос', 'search_input', sid, 'query')], [('Новый поиск', 'search_new')], self.ui_nav()]
        self.ui_show(uid, RichText('\n'.join(lines)), buttons)

    def search_details(self, uid, sid, index, preserve=False):
        s = self.search_session(uid, sid)
        if s['phase'] not in ('ready', 'details', 'added'):
            raise ValueError('Дождись результатов или начни новый поиск.')
        if not 0 <= index < len(s.get('results', [])):
            raise ValueError('Эта раздача больше недоступна. Повтори поиск.')
        r = s['results'][index]
        if not preserve or s.get('chosen') != index:
            title = suggested_title(r['title'], s['query'], s['kind'])
            # Capture a release year from its full title, not only its first language.
            year = re.search(r'\b(19\d{2}|20\d{2})\b', r['title'])
            if s['kind']=='movie' and year and not re.search(r'\b(19\d{2}|20\d{2})\b', title):
                title = slug(title[:85] + ' (' + year[0] + ')')
            ctx=s.get('follow_context')
            if ctx:
                s.update(title=ctx['title'],season=ctx['season'],start_episode=ctx['episode'],auto_season=False)
            else:
                s.update(title=title, season=s.get('season_filter') or 1, start_episode=1, auto_season=not bool(s.get('season_filter')))
        s.update(chosen=index, phase='details')
        size = f"{r['size']/GB:.2f} ГБ" if r['size'] is not None else 'неизвестен'
        text = f"<b>🎬 {escape(s['title'])}</b>\n{escape(r['quality'])} · {size}\n\n🎙 <b>Озвучка:</b> {escape(voices(r['title']))}\nСиды: {r['seeders'] if r['seeders'] is not None else '?'} · Пиры: {r['peers'] if r['peers'] is not None else '?'}\nТрекер: {escape(r['tracker'])}\n\n<b>Полное название раздачи</b>\n<i>{escape(r['title'])}</i>"
        if s['kind']=='series':
            text += '\nНачало: ' + ('автоматически, с начала раздачи' if s['auto_season'] else f"сезон {s['season']}, серия {s['start_episode']}")
        cap = self.state.get('cap')
        if s['kind']=='movie' and cap is not None and r['size'] is not None and r['size']>cap:
            text += '\n⚠️ Фильм больше текущего лимита ГБ: он будет ждать увеличения лимита.'
        rows = [[('✅ Добавить в очередь', 'search_add', sid, index)], [('Изменить название', 'search_input', sid, 'title')]]
        if s['kind']=='series':
            rows.append([('🔔 Следить за новыми сериями','follow_search',sid)])
            rows.append([('Выбрать сезон и серию', 'search_input', sid, 'start')])
        visible = [i for i, _ in self.search_visible(uid,s)]
        page = visible.index(index)//5 if index in visible else 0
        rows += [[('← Результаты', 'search_page', sid, page)], self.ui_nav()]
        self.ui_show(uid, RichText(text), rows)

    def search_start_context(self, uid, sid):
        s = self.search_session(uid, sid)
        if s.get('kind') != 'series' or s['phase'] != 'details' or 'chosen' not in s:
            raise ValueError('Сначала выбери раздачу сериала из результатов.')
        self.state.setdefault('ui_input', {}).pop(str(uid), None)
        return s, release_numbers(s['results'][s['chosen']]['title'])

    def search_seasons(self, uid, sid, page=0):
        s, hints = self.search_start_context(uid, sid)
        known = hints['seasons']
        page = min(max(0, page), (len(known)-1)//20 if known else 49)
        numbers = known[page*20:(page+1)*20] if known else list(range(page*20+1, min(1000,page*20+21)))
        buttons = [(f'Сезон {n}', 'search_season', sid, n) for n in numbers]
        rows = [buttons[n:n+4] for n in range(0,len(buttons),4)]
        nav = []
        if page: nav.append(('← Сезоны', 'search_seasons', sid, page-1))
        if (page+1)*20 < (len(known) if known else 999): nav.append(('Сезоны →', 'search_seasons', sid, page+1))
        if nav: rows.append(nav)
        rows += [[('Авто — с начала раздачи', 'search_auto', sid)], [('← К раздаче', 'search_preview', sid)]]
        text = 'Выбери начальный сезон.'
        text += '\nСезоны распознаны из названия раздачи.' if known else '\nВ названии сезоны не указаны. Номера ниже — выбор начала, наличие проверится по файлам после добавления.'
        self.ui_show(uid, text, rows)

    def search_episodes(self, uid, sid, season, page=0):
        s, hints = self.search_start_context(uid, sid)
        if not 0 <= season <= 999 or (hints['seasons'] and season not in hints['seasons']):
            raise ValueError('Выбери сезон из списка.')
        start, end = hints['episodes'] or (1,9999)
        page = min(max(0,page),(end-start)//40)
        numbers = range(start+page*40,min(end+1,start+(page+1)*40))
        buttons = [(str(n), 'search_episode', sid, season, n) for n in numbers]
        rows = [buttons[n:n+5] for n in range(0,len(buttons),5)]
        nav = []
        if page: nav.append(('← Серии', 'search_episodes', sid, season, page-1))
        if start+(page+1)*40 <= end: nav.append(('Серии →', 'search_episodes', sid, season, page+1))
        if nav: rows.append(nav)
        rows += [[('← Сезоны', 'search_seasons', sid, 0)], [('Авто — с начала раздачи', 'search_auto', sid)], [('← К раздаче', 'search_preview', sid)]]
        text = f'Сезон {season}. С какой серии начать?'
        text += f'\nВ названии указаны серии {start}–{end}.' if hints['episodes'] else '\nСписок серий в названии не указан. Выбери номер; файлы проверятся после добавления.'
        self.ui_show(uid, text, rows)

    def search_merge(self, session, result):
        rows=session.setdefault('results',[])
        identities={r.get('magnet') or r.get('link'):i for i,r in enumerate(rows)}
        for row in result.get('results',[]):
            key=row.get('magnet') or row.get('link')
            if key in identities:rows[identities[key]]=row
            else:identities[key]=len(rows);rows.append(row)
        session['warnings']=result.get('warnings',[])
        session['total']=result.get('total',len(rows))

    def search_progress_poll(self):
        events=getattr(self,'_search_events',None)
        if events is None:return
        latest={}
        while True:
            try:
                uid,sid,generation,result=events.get_nowait();latest[(uid,sid,generation)]=result
            except Empty:break
        for (uid,sid,generation),result in latest.items():
            s=self.state.get('searches',{}).get(str(uid))
            if uid not in self.users or not s or s['id']!=sid or not s.get('running') or s.get('generation',0)!=generation:continue
            self.search_merge(s,result)
            if s['phase']=='working' and s.get('results'):s['phase']='ready'
            future=self.search_jobs().get((uid,sid,'query'))
            if s['phase']=='ready' and s.get('displayed') and not (future and future.done()):
                old=getattr(self,'_ui_message',None);self._ui_message=s.get('message_id')
                try:self.search_page(uid,sid,s.get('page',0))
                except APIError:pass
                finally:self._ui_message=old
            self.store.save()

    def search_poll(self):
        self.search_progress_poll()
        self.catalog_poll()
        jobs = self.search_jobs()
        for key, future in list(jobs.items()):
            if not future.done():
                continue
            jobs.pop(key)
            uid, sid, task = key
            s = self.state.get('searches', {}).get(str(uid))
            if uid not in self.users or not s or s['id']!=sid or (not s.get('running',s['phase']=='working') if task=='query' else s['phase']!='importing'):
                continue
            old_mid = getattr(self, '_ui_message', None)
            self._ui_message = s.get('message_id')
            try:
                result = future.result()
                if task=='query':
                    self.search_merge(s,result)
                    s['running']=False
                    if s['phase']=='working':s['phase']='ready'
                    if s['phase']=='ready' and s.get('displayed',True):self.search_page(uid,sid,s.get('page',0))
                else:
                    magnet, data = result
                    text = self.add(s['title'], s['season'], magnet, s['start_episode'], s['kind'], s['auto_season'], torrent_data=data)
                    s['phase'] = 'added'
                    self.ui_home(uid, text)
            except (APIError, ValueError, OSError) as exc:
                s['running']=False
                s.update(phase='error', error=str(exc) if isinstance(exc,(APIError,ValueError)) else 'Не удалось сохранить раздачу. Повтори поиск.')
                try:
                    if task!='query' or s.get('displayed',True):self.search_page(uid, sid)
                except APIError:
                    pass  # Telegram/search failure must never stop the media queue.
            except Exception:
                s.update(phase='error', running=False, error='Поиск прервался. Попробуй ещё раз.')
            finally:
                self._ui_message = old_mid
                self.store.save()
        # Pending network tasks do not survive a process restart.
        for uid_string, s in list(self.state.get('searches', {}).items()):
            uid = int(uid_string)
            if s.get('running') and s['phase']!='working' and not any(k[:2]==(uid,s['id']) and k[2]=='query' for k in jobs):s['running']=False
            if s['phase'] in ('working','importing') and not any(k[:2]==(uid,s['id']) for k in jobs):
                s.update(phase='error', error='Поиск прервался при перезапуске бота. Его можно повторить.')
                self.store.save()

    def search_action(self, uid, action, *args):
        if action=='search_new':
            self.search_start(uid)
            return
        if action in ('search_options','search_filter','search_filter_set','search_filters_reset'):
            sid = args[0] if args else ''
            if sid: self.search_session(uid,sid)
            if action=='search_options': self.search_options(uid,sid)
            elif action=='search_filter': self.search_filter_menu(uid,sid,args[1],args[2])
            elif action=='search_filters_reset':
                p=self.search_prefs(uid)
                for field in ('quality','voice','seeds','size','sort'): p.pop(field,None)
                if sid:
                    self.search_session(uid,sid).pop('voice',None)
                    self.search_session(uid,sid).pop('season_filter',None)
                self.search_options(uid,sid)
            else:
                field,value=args[1],args[2]
                if value not in [v for _,v in self.search_filter_choices(uid,sid,field)]:
                    raise ValueError('Этот вариант фильтра больше недоступен. Открой фильтры снова.')
                if field=='season':self.search_session(uid,sid)['season_filter']=value
                elif field=='voice': self.search_session(uid,sid)['voice']=value
                else: self.search_prefs(uid)[field]=value
                self.search_options(uid,sid)
            self.store.save()
            return
        sid = args[0]
        s = self.search_session(uid, sid)
        if s['phase'] in ('working','importing') and action not in ('search_page','search_cancel'):
            raise ValueError('Запрос уже выполняется. Дождись результата или отмени его.')
        if action=='search_kind':
            if args[1] not in ('movie','series'):
                raise ValueError('Выбери фильм или сериал.')
            s['kind'] = args[1]
            if s['query']:
                self.search_run(uid, sid)
            else:
                self.search_input(uid, sid, 'query')
        elif action=='search_seasons':
            self.search_seasons(uid, sid, args[1])
        elif action=='search_season':
            self.search_episodes(uid, sid, args[1])
        elif action=='search_episodes':
            self.search_episodes(uid, sid, args[1], args[2])
        elif action=='search_episode':
            s, hints = self.search_start_context(uid, sid)
            sn, en = args[1], args[2]
            parse_start(f'{sn} {en}')
            if hints['seasons'] and sn not in hints['seasons']:
                raise ValueError('Выбери сезон из списка.')
            if hints['episodes'] and not hints['episodes'][0] <= en <= hints['episodes'][1]:
                raise ValueError('Выбери серию из списка.')
            s.update(season=sn, start_episode=en, auto_season=False)
            self.search_details(uid, sid, s['chosen'], preserve=True)
        elif action=='search_auto':
            s, _ = self.search_start_context(uid, sid)
            s.update(season=1, start_episode=1, auto_season=True)
            self.search_details(uid, sid, s['chosen'], preserve=True)
        elif action=='search_preview':
            s, _ = self.search_start_context(uid, sid)
            self.search_details(uid, sid, s['chosen'], preserve=True)
        elif action=='search_input':
            self.search_input(uid, sid, args[1])
        elif action=='search_run':
            self.search_run(uid, sid)
        elif action=='search_page':
            self.search_page(uid, sid, args[1])
        elif action=='search_pick':
            self.search_details(uid, sid, args[1])
        elif action=='search_add':
            if s['phase']!='details' or s.get('chosen')!=args[1]:
                raise ValueError('Открой раздачу из результатов и нажми «Добавить» ещё раз.')
            self.search_submit(uid, sid, 'import', Jackett().resolve, dict(s['results'][args[1]]))
            s['phase']='importing'
            s['message_id'] = self.ui_show(uid, 'Подготавливаю выбранную раздачу для очереди…',
                                           [[('Отмена', 'search_cancel', sid)], self.ui_nav()])
        elif action=='search_cancel':
            self.state.get('searches', {}).pop(str(uid), None)
            self.ui_home(uid, 'Поиск отменён.')
        else:
            raise ValueError('Меню поиска устарело. Открой /search.')
        self.store.save()
