"""Shared show subscriptions. Network work stays outside the media queue thread."""
import copy
import json
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from html import escape
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core import episode_count, slug
from net import APIError, RichText
from search_client import Jackett, plain, resolution, voice_tags, release_numbers

INTERVAL = 2*3600


class TVmaze:
    def get(self, path, query=None):
        url='https://api.tvmaze.com'+path+('?' + urlencode(query) if query else '')
        try:
            with urlopen(Request(url,headers={'User-Agent':'SerialQueue/1.0 (personal media library)'}),timeout=20) as r:
                data=r.read(6*1024*1024+1)
            if len(data)>6*1024*1024: raise ValueError()
            return json.loads(data)
        except Exception:
            raise APIError('TVmaze не ответил. Подписки сохраняются; проверка повторится автоматически.') from None

    def find(self, query, jellyfin=None):
        # Prefer a verified external ID already attached to the user's library.
        if jellyfin is not None and hasattr(jellyfin,'http'):
            try:
                items=jellyfin.http.call('/Items',query={'recursive':'true','includeItemTypes':'Series','searchTerm':query,'fields':'ProviderIds,OriginalTitle','limit':10})['Items']
                exact=[i for i in items if i.get('Name','').casefold()==query.casefold()]
                if len(exact)==1:
                    ids=exact[0].get('ProviderIds',{})
                    for field,param in [('Imdb','imdb'),('Tvdb','thetvdb')]:
                        if ids.get(field):
                            show=self.get('/lookup/shows',{param:ids[field]})
                            if isinstance(show,dict) and show.get('id'):return [self.show(show)]
                    query=exact[0].get('OriginalTitle') or query
            except (APIError,KeyError,TypeError): pass
        data=self.get('/search/shows',{'q':query})
        if not isinstance(data,list):raise APIError('Не удалось прочитать каталог TVmaze.')
        return [self.show(v['show']) for v in data[:8] if isinstance(v,dict) and isinstance(v.get('show'),dict)]

    @staticmethod
    def show(value):
        return {'id':int(value['id']),'name':plain(value.get('name'),100),'premiered':plain(value.get('premiered'),10),
                'status':plain(value.get('status'),40),'language':plain(value.get('language'),40)}

    def episodes(self, show_id):
        data=self.get(f'/shows/{int(show_id)}/episodes')
        if not isinstance(data,list):raise APIError('Не удалось прочитать серии TVmaze.')
        rows=[]
        for e in data[:10000]:
            try:
                sn,en=int(e['season']),int(e['number'])
                if not 1<=sn<=999 or not 1<=en<=9999:continue
                stamp=e.get('airstamp')
                # A date without a precise time is treated as the end of that UTC date.
                stamp=stamp or (e['airdate']+'T23:59:59+00:00' if e.get('airdate') else '')
                air=datetime.fromisoformat(stamp.replace('Z','+00:00')).astimezone(timezone.utc).timestamp() if stamp else None
                if air is not None:rows.append({'season':sn,'number':en,'air':air,'name':plain(e.get('name'),100)})
            except (TypeError,ValueError,KeyError):continue
        return rows


def release_is_show(title,sub):
    def normal(text):return re.sub(r'[^\w]+',' ',text.casefold()).strip()
    names={normal(sub['show']['name']),normal(sub['title'])} | {normal(v) for v in sub.get('aliases',[])}
    for part in title.split('/'):
        prefix=re.split(r'(?i)\[|\(|\bS\d|\b(?:\d+\s+)?(?:сезон|season)',part)[0]
        if normal(prefix) in names:return True
    return False


def release_matches(title, season, number):
    hints=release_numbers(title)
    # Precise ranges support an episode inside a season pack, including doubled episodes.
    if season not in hints['seasons']:return False
    if len(hints['seasons'])==1 and hints['episodes']:
        return hints['episodes'][0]<=number<=hints['episodes'][1]
    # An unnumbered season pack is only advertised for the premiere, never as proof of later episodes.
    return number==1 and not hints['episodes']


def scan_subscription(sub):
    now=time.time()
    episodes=TVmaze().episodes(sub['show']['id'])
    pending=[e for e in episodes if sub['since']<e['air']<=now and f"{e['season']}:{e['number']}" not in sub.get('dismissed',[])]
    events=copy.deepcopy(sub.get('events',{}))
    for e in sorted(pending,key=lambda e:(e['season'],e['number']))[-100:]:
        key=f"{e['season']}:{e['number']}"
        events.setdefault(key,dict(e,calendar={},ready={},choices=[]))
    valid_keys={f"{e['season']}:{e['number']}" for e in sorted(pending,key=lambda e:(e['season'],e['number']))[-100:]}
    events={k:e for k,e in events.items() if k in valid_keys}
    awaiting=[e for e in events.values() if not e.get('choices') and not e.get('handled')]
    warning=''
    if awaiting:
        try:
            result=Jackett().search(sub.get('query') or sub['show']['name'],'series')
            warning=', '.join(result['warnings'])
            for e in awaiting:
                matches=[r for r in result['results'] if release_is_show(r['title'],sub) and release_matches(r['title'],e['season'],e['number'])
                         and (not sub.get('quality') or sub['quality'] in ('Любое', 'any', '') or resolution(r['title'])==sub['quality'])
                         and (not sub.get('voice') or sub['voice'] in ('Любая', 'any', '') or sub['voice'] in voice_tags(r['title']))
                         and r.get('seeders') is not None and r['seeders']>0]
                # Preserve original release titles: selection still requires explicit confirmation.
                e['choices']=matches[:5]
        except (APIError,ValueError,OSError):warning='Поиск раздач недоступен, повторю проверку.'
    future=sorted((e for e in episodes if e['air']>now),key=lambda e:e['air'])
    return {'events':events,'next_episode':future[0] if future else None,'warning':warning,'_generation':sub.get('generation'),'_revision':sub.get('revision',0)}


class SubscriptionUI:
    def follow_all(self):return self.state.setdefault('subscriptions',{})

    def follow_sub(self,key):
        sub=self.follow_all().get(key)
        if not sub:raise ValueError('Подписка удалена. Открой /subscriptions.')
        return sub

    def follow_jobs(self):
        if not hasattr(self,'_follow_jobs'):self._follow_jobs={}
        return self._follow_jobs

    def follow_submit(self,key,kind,fn,*args):
        jobs=self.follow_jobs()
        if (key,kind) in jobs or len(jobs)>=3:raise ValueError('Предыдущая проверка ещё идёт. Подожди немного.')
        if not hasattr(self,'_follow_pool'):self._follow_pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='subscriptions')
        jobs[(key,kind)]=self._follow_pool.submit(fn,*args)

    def follow_begin(self,uid,query='',library_title=None):
        self.state.setdefault('dialogs',{}).pop(str(uid),None)
        self.state.setdefault('ui_input',{}).pop(str(uid),None)
        if not query:
            self.state['ui_input'][str(uid)]={'kind':'follow_query'}
            self.ui_show(uid,'На какой сериал подписаться? Напиши название. Можно использовать оригинальное название.',[[('← Подписки','follow_list',0)],self.ui_nav()])
        else:
            if not 2<=len(query)<=120:raise ValueError('Название должно содержать от 2 до 120 символов.')
            sid=secrets.token_urlsafe(8)
            self.follow_submit(str(uid),'lookup',TVmaze().find,query,self.j)
            self.state.setdefault('follow_dialogs',{})[str(uid)]={'id':sid,'query':query,'phase':'working','expires':time.time()+86400,'library_title':library_title}
            self.ui_show(uid,'Ищу сериал в каталоге расписания…',[[('← Подписки','follow_list',0)],self.ui_nav()])
        self.store.save()

    def follow_draft(self,uid,sid):
        d=self.state.get('follow_dialogs',{}).get(str(uid))
        if not d or d['id']!=sid or d['expires']<time.time():raise ValueError('Выбор устарел. Начни /follow заново.')
        return d

    def follow_list(self,uid,page=0):
        pairs=list(self.follow_all().items());page=min(max(0,page),max(0,(len(pairs)-1)//8))
        rows=[[(s['title']+(' · пауза' if not s['enabled'] else ''),'follow_open',key)] for key,s in pairs[page*8:(page+1)*8]]
        nav=[]
        if page:nav.append(('← Назад','follow_list',page-1))
        if (page+1)*8<len(pairs):nav.append(('Дальше →','follow_list',page+1))
        if nav:rows.append(nav)
        rows += [[('➕ Подписаться на сериал','follow_new')],self.ui_nav()]
        self.ui_show(uid,RichText('<b>🔔 Подписки на сериалы</b>\n\nСначала — уведомление о выходе серии, затем — о подходящей раздаче. Проверка каждые 2 часа.\n\nПодписки и уведомления общие для семьи.\n<i>Данные расписания: TVmaze · tvmaze.com</i>'),rows)

    def follow_open(self,uid,key):
        s=self.follow_sub(key)
        text=f"<b>🔔 {escape(s['title'])}</b>\n{escape(s['show']['name'])} · {escape(s['show'].get('premiered',''))}\n\nКачество: {escape(s.get('quality') or 'любое')}\nОзвучка: {escape(s.get('voice') or 'любая')}\n{'Включена' if s['enabled'] else 'На паузе'}"
        nxt=s.get('next_episode')
        if nxt:text+=f"\nСледующая: S{nxt['season']:02d}E{nxt['number']:02d} · {datetime.fromtimestamp(nxt['air'],ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК')}"
        if s.get('error'):text+='\n\n'+escape(s['error'])
        if s.get('warning'):text+='\n\n'+escape(s['warning'])
        rows=[[('Качество','follow_quality',key)],[('Озвучка','follow_voice',key)],[('Проверить сейчас','follow_check',key)],
              [('Продолжить' if not s['enabled'] else 'Пауза уведомлений','follow_toggle',key)]]
        for ek,e in list(s.get('events',{}).items())[-8:]:
            if not e.get('handled'):
                rows.append([(f"S{e['season']:02d}E{e['number']:02d}"+(' · раздачи' if e['choices'] else ' · ждём перевод'),'follow_event',key,ek)])
        rows += [[('Удалить подписку','follow_remove',key)], [('← Подписки','follow_list',0)],self.ui_nav()]
        self.ui_show(uid,RichText(text),rows)

    def follow_covered(self,s,e):
        names={s['title'].casefold(),s['show']['name'].casefold()} | {n.casefold() for n in s.get('aliases',[])}
        for p in self.state['seasons']:
            if p.get('kind')=='movie' or p['title'].casefold() not in names:continue
            for f in p['episodes']:
                if f.get('season',p['season'])==e['season'] and f['number']<=e['number']<=f.get('end_number',f['number']):
                    if not f.get('skipped_start') and p['phase']!='missing':return True
        return False

    def follow_notify(self,key,s,budget=4):
        used=0
        if not s['enabled']:return 0
        for ek,e in sorted(s.get('events',{}).items(),key=lambda pair:(pair[1]['season'],pair[1]['number']))[:100]:
            if e.get('handled') or ek in s.get('dismissed',[]):continue
            if self.follow_covered(s,e):e['handled']=True;continue
            ready=bool(e['choices']);stage='ready' if ready else 'calendar'
            label=f"S{e['season']:02d}E{e['number']:02d}"
            title=('Новый сезон — ' if e['number']==1 else '')+s['title']+' · '+label
            if ready:
                r=e['choices'][0]
                text=RichText('<b>✅ Найдена подходящая раздача</b>\n'+escape(title)+'\n'+escape(r['quality'])+' · '+escape(s.get('voice') or 'любая озвучка')+'\n\nВыбери раздачу и подтверди добавление в очередь.')
            else:
                waiting='Ждём озвучку '+s['voice'] if s.get('voice') else 'Ждём подходящую раздачу'
                text=RichText('<b>📅 Серия вышла по расписанию</b>\n'+escape(title)+'\n'+escape(e['name'])+'\n\n'+escape(waiting)+'. Пока подходящая раздача не найдена.')
            for uid in sorted(self.users):
                if str(uid) in e[stage]:continue
                if used>=budget:return used
                used+=1
                try:
                    rows=[[('Выбрать раздачу' if ready else 'Проверить раздачи','follow_event',key,ek)],
                          [('Пропустить серию','follow_dismiss',key,ek)],[('Подписка','follow_open',key)]]
                    mid=self.ui_show(uid,text,rows)
                    e[stage][str(uid)]=mid;self.store.save()
                except APIError as exc:
                    if str(exc) in ('HTTP 400','HTTP 403'):
                        e[stage][str(uid)]=None;self.store.save()
                    continue
        return used

    def follow_cycle(self):
        # A failed metadata service must never enter the queue's global fail-closed handler.
        jobs=self.follow_jobs()
        for (key,kind),future in list(jobs.items()):
            if not future.done():continue
            jobs.pop((key,kind))
            if kind=='lookup':
                uid=int(key);d=self.state.get('follow_dialogs',{}).get(key)
                if uid not in self.users or not d or d['phase']!='working':continue
                try:
                    shows=future.result();d.update(shows=shows,phase='choose')
                    rows=[[(v['name']+' ('+v['premiered'][:4]+')','follow_pick',d['id'],i)] for i,v in enumerate(shows)]
                    rows += [[('Изменить название','follow_new')],[('← Подписки','follow_list',0)]]
                    self.ui_show(uid,'Подтверди сериал:' if shows else 'В каталоге не найдено. Попробуй оригинальное название на английском.',rows)
                except Exception:
                    d['phase']='error'
                    try:self.ui_show(uid,'Каталог расписания недоступен. Можно повторить позже.',[[('Повторить','follow_retry',d['id'])],self.ui_nav()])
                    except APIError:pass
            else:
                s=self.follow_all().get(key)
                if not s:continue
                try:
                    result=future.result()
                    if result.pop('_generation',None)!=s.get('generation') or result.pop('_revision',0)!=s.get('revision',0):
                        s['next_check']=0
                        continue
                    for ek,e in result['events'].items():
                        previous=s.get('events',{}).get(ek,{})
                        for stage in ('calendar','ready'): e[stage].update(previous.get(stage,{}))
                        if previous.get('handled') or ek in s['dismissed']:e['handled']=True
                    s.update(result,error='',checked=time.time())
                except Exception:
                    s.update(error='Не удалось проверить расписание. Повторю через 15 минут.',next_check=time.time()+900)
            self.store.save()
        budget=4
        for key,s in list(self.follow_all().items()):
            if budget<=0:break
            try:budget-=self.follow_notify(key,s,budget)
            except (APIError,ValueError,OSError):pass
        if sum(kind=='scan' for _,kind in jobs)>=1:return
        for key,s in self.follow_all().items():
            if not s['enabled'] or s.get('next_check',0)>time.time():continue
            try:
                self.follow_submit(key,'scan',scan_subscription,copy.deepcopy(s))
                s['next_check']=time.time()+INTERVAL;self.store.save()
            except ValueError:pass
            break

    def follow_action(self,uid,action,*args):
        if action=='follow_search':
            session=self.search_session(uid,args[0])
            r=session['results'][session['chosen']]
            parts=[v.strip() for v in r['title'].split('/') if re.search(r'[A-Za-z]{3}',v)]
            query=re.split(r'\[|\(|(?i:season|сезон)|\bS\d',parts[0])[0].strip() if parts else session['title']
            self.follow_begin(uid,query or session['title'],library_title=session['title'])
            return
        if action=='follow_new':self.follow_begin(uid);return
        if action=='follow_list':self.follow_list(uid,args[0]);return
        if action=='follow_retry':self.follow_begin(uid,self.follow_draft(uid,args[0])['query']);return
        if action=='follow_pick':
            d=self.follow_draft(uid,args[0]);i=args[1]
            if not 0<=i<len(d.get('shows',[])):raise ValueError('Выбери сериал из списка.')
            show=d['shows'][i];key=str(show['id'])
            if key not in self.follow_all():
                if len(self.follow_all())>=20:raise ValueError('Можно отслеживать до 20 сериалов.')
                self.follow_all()[key]={'show':show,'title':slug(d.get('library_title') or show['name']),'aliases':[d['query'],show['name']],'query':show['name'],'since':time.time(),'enabled':False,'events':{},'dismissed':[],'next_check':0,'generation':secrets.token_urlsafe(8),'revision':0,'quality':self.search_prefs(uid).get('prefer','')}
            if self.follow_all()[key].get('activated'):self.follow_open(uid,key)
            else:self.follow_setup(uid,key)
            return
        key=args[0];s=self.follow_sub(key)
        if action=='follow_open':self.follow_open(uid,key)
        elif action=='follow_quality':
            self.ui_show(uid,'Какое качество искать?', [[(q or 'Любое','follow_set',key,'quality',q)] for q in ('','2160p','1080p','720p')]+[[('← Подписка','follow_open',key)]])
        elif action=='follow_voice':
            options=['','Кураж-Бамбей','LostFilm','NewStudio','Кубик в Кубе','Paramount Comedy','HDRezka','TVShows','MVO','DUB','Rus']
            self.ui_show(uid,'Какую озвучку ждать? Можно выбрать студию, тип перевода или язык.',[[(v or 'Любая','follow_set',key,'voice',v)] for v in options]+[[('Другая студия','follow_voice_custom',key)],[('← Подписка','follow_open',key)]])
        elif action=='follow_voice_custom':
            self.state.setdefault('ui_input',{})[str(uid)]={'kind':'follow_voice','key':key}
            self.ui_show(uid,'Напиши точное название студии, как в результатах поиска.',[[('← Подписка','follow_open',key)]])
        elif action=='follow_set':
            field,value=args[1:]
            if field not in ('quality','voice') or not isinstance(value,str) or len(value)>100:raise ValueError('Неверная настройка подписки.')
            s[field]=value;s['next_check']=0;s['revision']=s.get('revision',0)+1
            for e in s['events'].values():e['choices']=[];e['ready']={}
            self.follow_setup(uid,key) if not s['enabled'] and not s.get('activated') else self.follow_open(uid,key)
        elif action=='follow_enable':
            if not s.get('activated'):s['since']=time.time();s['activated']=True
            s['enabled']=True;s['next_check']=0;self.follow_open(uid,key)
        elif action=='follow_toggle':s['enabled']=not s['enabled'];s['next_check']=0;self.follow_open(uid,key)
        elif action=='follow_check':s['next_check']=0;self.follow_open(uid,key)
        elif action=='follow_remove':
            self.ui_show(uid,'Удалить подписку на '+s['title']+'?',[[('Да, удалить','follow_delete',key)],[('Отмена','follow_open',key)]])
        elif action=='follow_delete':self.follow_all().pop(key,None);self.follow_list(uid)
        elif action in ('follow_event','follow_dismiss'):
            ek=args[1];e=s['events'].get(ek)
            if not e:raise ValueError('Серия больше не доступна. Открой подписку.')
            if action=='follow_dismiss':
                s['dismissed']=list(dict.fromkeys(s['dismissed']+[ek]));e['handled']=True;self.follow_open(uid,key)
            elif not e['choices']:
                s['next_check']=0;self.ui_show(uid,'Подходящая раздача пока не найдена. Проверю снова.',[[('← Подписка','follow_open',key)]])
            elif self.follow_covered(s,e):self.ui_show(uid,'Эта серия уже есть в очереди или была просмотрена.',[[('← Подписка','follow_open',key)]])
            else:
                # No unconfirmed content is added by following a notification.
                self.search_start(uid,s['title'])
                search=self.state['searches'][str(uid)]
                search.update(phase='ready',kind='series',results=copy.deepcopy(e['choices']),total=len(e['choices']),warnings=[],follow_context={'title':s['title'],'season':e['season'],'episode':e['number']})
                # Release offers are curated independently from ad-hoc search filters.
                search['curated']=True
                self.search_page(uid,search['id'])
        else:raise ValueError('Меню подписки устарело.')
        self.store.save()

    def follow_setup(self,uid,key):
        s=self.follow_sub(key)
        self.ui_show(uid,RichText('<b>Подписка: '+escape(s['title'])+'</b>\n\nКачество: '+escape(s.get('quality') or 'любое')+'\nОзвучка: '+escape(s.get('voice') or 'любая')+'\n\nСообщать о выходе серии и появлении подходящей раздачи. Старые серии до включения подписки не предлагаются.'),
                     [[('Выбрать качество','follow_quality',key)],[('Выбрать озвучку','follow_voice',key)],[('✅ Включить подписку','follow_enable',key)],[('← Подписки','follow_list',0)]])
