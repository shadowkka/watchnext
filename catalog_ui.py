"""Family-scoped Telegram catalogue UI over the same services as the web app."""
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from html import escape
from urllib.parse import urlencode, urlsplit
from catalog_client import CatalogClient, parse_history, public_web_url
from net import APIError, RichText

GENRES={'':'Любой','Action':'Боевик','Adventure':'Приключения','Animation':'Мультфильм','Comedy':'Комедия','Crime':'Криминал','Documentary':'Документальный','Drama':'Драма','Family':'Семейный','Fantasy':'Фэнтези','History':'История','Horror':'Ужасы','Mystery':'Детектив','Romance':'Мелодрама','Sci-Fi':'Фантастика','Thriller':'Триллер','War':'Военный','Western':'Вестерн'}
COUNTRIES={'':'Любая','United States':'США','United Kingdom':'Великобритания','Russia':'Россия','Soviet Union':'СССР','France':'Франция','Germany':'Германия','Italy':'Италия','Spain':'Испания','Canada':'Канада','Australia':'Австралия','Japan':'Япония','South Korea':'Южная Корея','China':'Китай','Hong Kong':'Гонконг','India':'Индия','Sweden':'Швеция','Denmark':'Дания','Norway':'Норвегия','Finland':'Финляндия','Poland':'Польша','Czech Republic':'Чехия','Mexico':'Мексика','Brazil':'Бразилия','Argentina':'Аргентина','Iran':'Иран','Turkey':'Турция','New Zealand':'Новая Зеландия','Belgium':'Бельгия','Netherlands':'Нидерланды'}


class CatalogUI:
    def catalog_session(self,uid,sid):
        session=self.state.get('catalogs',{}).get(str(uid))
        if not session or session['id']!=sid or session['expires']<time.time():raise ValueError('Меню устарело. Открой /discover заново.')
        return session

    def catalog_home(self,uid,mode='home'):
        self.state.setdefault('ui_input',{}).pop(str(uid),None)
        self.state.setdefault('dialogs',{}).pop(str(uid),None)
        session={'id':secrets.token_urlsafe(8),'expires':time.time()+86400,'movies':[],'skip':[],
                 'filters':{'yearFrom':1990,'yearTo':time.localtime().tm_year,'rating':7,'count':3,'genre':'','country':'','excludeGenre':'','excludeCountry':''}}
        self.state.setdefault('catalogs',{})[str(uid)]=session
        sid=session['id']
        if mode=='random':self.catalog_random_menu(uid,sid);return
        self.ui_show(uid,'🍿 Что посмотреть?\nКаталог и просмотренное общие с веб-интерфейсом.',[
            [('🎭 Актёры и режиссёры','catalog_prompt',sid,'person')],
            [('🎞 Подборки','catalog_load',sid,'collections','')],
            [('🎲 Случайный фильм','catalog_random',sid)],
            [('✅ Просмотренное','catalog_history',sid)],self.ui_nav()])

    def catalog_jobs(self):
        if not hasattr(self,'_catalog_jobs'):self._catalog_jobs={}
        return self._catalog_jobs

    def catalog_submit(self,uid,sid,task,fn,*args):
        s=self.catalog_session(uid,sid);jobs=self.catalog_jobs()
        if any(key[0]==uid for key in jobs):raise ValueError('Предыдущий запрос ещё завершается. Подожди немного.')
        if len(jobs)>=4:raise ValueError('Каталог занят. Повтори через несколько секунд.')
        if not hasattr(self,'_catalog_pool'):self._catalog_pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='catalog')
        nonce=secrets.token_urlsafe(6)
        s.update(task=task,nonce=nonce,phase='working')
        s['message_id']=self.ui_show(uid,'Подбираю данные… Очередь продолжает работать.',[[('Отменить','catalog_home')],self.ui_nav()])
        # Request arguments are snapshots. Background code never receives Store/session references.
        jobs[(uid,sid,nonce,task)]=self._catalog_pool.submit(fn,*args)
        self.store.save()

    def catalog_prompt(self,uid,sid,field):
        self.catalog_session(uid,sid)
        self.state.setdefault('ui_input',{})[str(uid)]={'kind':'catalog_'+field,'sid':sid}
        prompts={'person':'Напиши имя актёра или режиссёра. Можно неполное, например «Том Х» — покажу варианты.',
                 'years':'Введи годы от и до через пробел, например 1990 2026.',
                 'rating':'Минимальный рейтинг TMDB: число от 0 до 10.',
                 'listyears':'Введи годы от и до для фильмографии/подборки, например 1990 2026.'}
        self.ui_show(uid,prompts[field],[[('← Назад','catalog_random' if field in ('years','rating') else 'catalog_list',sid,0)] if field!='person' else [('← Каталог','catalog_home')],self.ui_nav()])

    def catalog_text(self,uid,kind,value):
        pending=self.state.get('ui_input',{}).get(str(uid),{});sid=pending.get('sid');s=self.catalog_session(uid,sid)
        field=kind.removeprefix('catalog_')
        if field=='upload':raise ValueError('Пришли CSV/JSON как файл, не ссылку. Импорт профиля Кинопоиска по ссылке пока не поддерживается.')
        if field=='person':
            if not 2<=len(value.strip())<=100:raise ValueError('Введи от 2 до 100 символов имени.')
            self.catalog_load(uid,sid,'people',value.strip())
        elif field in ('years','listyears'):
            try:a,b=map(int,value.split())
            except ValueError:raise ValueError('Нужны два года через пробел.') from None
            if not 1870<=a<=b<=time.localtime().tm_year:raise ValueError('Проверь диапазон годов.')
            if field=='years':s['filters'].update(yearFrom=a,yearTo=b);self.catalog_random_menu(uid,sid)
            else:s['list_years']=[a,b];self.catalog_list(uid,sid)
        elif field=='rating':
            try:rating=float(value.replace(',','.'))
            except ValueError:raise ValueError('Нужен рейтинг от 0 до 10.') from None
            if not 0<=rating<=10:raise ValueError('Нужен рейтинг от 0 до 10.')
            s['filters']['rating']=rating;self.catalog_random_menu(uid,sid)
        self.state.setdefault('ui_input',{}).pop(str(uid),None);self.store.save()

    def catalog_load(self,uid,sid,task,value):
        self.catalog_session(uid,sid)
        routes={'people':'/api/explore/people?'+urlencode({'query':value}), 'person':'/api/explore/person/'+value,
                'collections':'/api/explore/collections','collection':'/api/explore/collection/'+value,
                'history':'/api/discover/status'}
        if task not in routes:raise ValueError('Неизвестный раздел')
        self.catalog_submit(uid,sid,task,CatalogClient().call,routes[task])

    def catalog_random_menu(self,uid,sid):
        s=self.catalog_session(uid,sid);f=s['filters']
        rows=[[(f"Годы: {f['yearFrom']}–{f['yearTo']}",'catalog_prompt',sid,'years')],
              [(f"Рейтинг TMDB от {f['rating']}",'catalog_prompt',sid,'rating')],
              [('Жанр: '+GENRES.get(f['genre'],'Любой'),'catalog_options',sid,'genre',0)],
              [('Страна: '+COUNTRIES.get(f['country'],'Любая'),'catalog_options',sid,'country',0)],
              [('Исключить жанр: '+(GENRES.get(f['excludeGenre']) if f['excludeGenre'] else 'нет'),'catalog_options',sid,'excludeGenre',0)],
              [('Исключить страну: '+(COUNTRIES.get(f['excludeCountry']) if f['excludeCountry'] else 'нет'),'catalog_options',sid,'excludeCountry',0)],
              [('Предложить: '+str(f['count']),'catalog_options',sid,'count',0)],
              [('🎲 Подобрать фильмы','catalog_pick',sid)], [('✅ Просмотренное','catalog_history',sid)],self.ui_nav()]
        self.ui_show(uid,'🎲 Случайный выбор TMDB\nПросмотренное и уже показанные в этой подборке фильмы исключаются.',rows)

    def catalog_options(self,uid,sid,field,page=0):
        s=self.catalog_session(uid,sid)
        if field in ('genre','excludeGenre'):options=list(GENRES.items())
        elif field in ('country','excludeCountry'):options=list(COUNTRIES.items())
        elif field=='count':options=[(i,str(i)) for i in (1,3,5,10)]
        elif field=='role':options=[('','Все роли'),('actor','Актёр'),('director','Режиссёр')]
        elif field=='kind':options=[('','Все'),('movie','Фильмы'),('series','Сериалы')]
        elif field=='order':options=[('source','Порядок источника'),('new','Сначала новые'),('old','Сначала старые'),('title','По названию')]
        else:raise ValueError('Неизвестный фильтр')
        page=min(max(0,int(page)),(len(options)-1)//8)
        rows=[[(label,'catalog_set',sid,field,key)] for key,label in options[page*8:page*8+8]]
        nav=[]
        if page:nav.append(('←','catalog_options',sid,field,page-1))
        if (page+1)*8<len(options):nav.append(('→','catalog_options',sid,field,page+1))
        if nav:rows.append(nav)
        rows.append([('← Назад','catalog_list' if field in ('role','kind','order') else 'catalog_random',sid,0)])
        self.ui_show(uid,'Выбери значение:',rows)

    def catalog_visible(self,s):
        rows=[]
        for i,m in enumerate(s.get('movies',[])):
            if m.get('watched') and s.get('exclude_watched',True):continue
            if s.get('role') and s['role'] not in m.get('roles',[]):continue
            if s.get('kind') and m.get('kind')!=s['kind']:continue
            if s.get('list_years') and not (m.get('year') and s['list_years'][0]<=m['year']<=s['list_years'][1]):continue
            rows.append((i,m))
        order=s.get('order','source')
        if order in ('new','old'):rows.sort(key=lambda r:(not bool(r[1].get('year')),(-1 if order=='new' else 1)*(r[1].get('year') or 0)))
        elif order=='title':rows.sort(key=lambda r:r[1].get('title','').casefold())
        return rows

    def catalog_list(self,uid,sid,page=0):
        s=self.catalog_session(uid,sid);items=self.catalog_visible(s);page=min(max(0,int(page)),max(0,(len(items)-1)//6))
        lines=['<b>'+escape(s.get('title','Фильмы'))+'</b>',escape(s.get('source','')),
               f'Найдено: {len(items)} · страница {page+1} из {max(1,(len(items)+5)//6)}']
        rows=[]
        for index,movie in items[page*6:page*6+6]:
            label=movie.get('title','Без названия')[:120];year=movie.get('year') or '—'
            lines.append('\n'+escape(label)+f' ({year})')
            rows.append([(label[:70]+' · '+str(year),'catalog_card',sid,index)])
        if not items:lines.append('\nПод эти фильтры ничего не найдено.')
        if s.get('truncated'):lines.append('\nИсточник вернул ограниченный список. Фильмография может быть неполной.')
        if s.get('stale'):lines.append('\nИсточник временно недоступен: показана сохранённая версия.')
        nav=[]
        if page:nav.append(('←','catalog_list',sid,page-1))
        if (page+1)*6<len(items):nav.append(('Дальше →','catalog_list',sid,page+1))
        if nav:rows.append(nav)
        if s.get('mode')=='random':rows += [[('🎲 Ещё варианты','catalog_pick',sid)],[('Фильтры','catalog_random',sid)]]
        else:
            rows += [[('Тип','catalog_options',sid,'kind',0),('Роль','catalog_options',sid,'role',0)],
                     [('Годы','catalog_prompt',sid,'listyears'),('Порядок','catalog_options',sid,'order',0)],
                     [('Сбросить годы','catalog_reset_years',sid)],
                     [('Исключать просмотренное: '+('да' if s.get('exclude_watched',True) else 'нет'),'catalog_toggle_seen',sid)]]
        rows += [[('← Каталог','catalog_home')],self.ui_nav()]
        self.ui_show(uid,RichText('\n'.join(lines)),rows)

    def catalog_card(self,uid,sid,index):
        s=self.catalog_session(uid,sid)
        if not 0<=index<len(s.get('movies',[])):raise ValueError('Фильм больше недоступен')
        s['chosen']=index
        self.catalog_submit(uid,sid,'card',CatalogClient().card,dict(s['movies'][index]))

    def catalog_card_show(self,uid,sid,movie,photo=None):
        s=self.catalog_session(uid,sid);index=s['chosen']
        rating=movie.get('tmdbRating') if movie.get('tmdbRating') is not None else movie.get('rating')
        lines=['<b>'+escape(str(movie.get('title',''))[:160])+'</b>',str(movie.get('year') or '—')+(' · '+('Сериал' if movie.get('kind')=='series' else 'Фильм'))]
        if rating:lines.append(('TMDB' if movie.get('tmdbRating') else 'IMDb')+': '+str(round(float(rating),1)))
        if movie.get('description'):lines.append('\n'+escape(str(movie['description'])[:500]))
        if movie.get('descriptionSource'):lines.append('\nОписание: '+escape(str(movie['descriptionSource'])[:70]))
        if movie.get('tmdbFound'):lines.append('Данные: TMDB · themoviedb.org')
        rows=[[('🔎 Найти раздачи','catalog_release',sid,index)],
              [('✅ Уже смотрел','catalog_seen',sid,index)], [('← К списку','catalog_list',sid,0)],self.ui_nav()]
        caption=RichText('\n'.join(lines))
        if photo and hasattr(self.tg,'photo'):
            markup={'inline_keyboard':[[self.ui_button(uid,*button) for button in row] for row in rows]}
            self.store.save()
            try:self.tg.photo(photo,caption,uid,markup)
            except APIError:self.ui_show(uid,caption,rows)
        else:self.ui_show(uid,caption,rows)

    def catalog_document(self,uid,document):
        if uid!=self.tg.owner:raise ValueError('Загрузить общий список может только владелец бота.')
        pending=self.state.get('ui_input',{}).get(str(uid),{})
        if pending.get('kind')!='catalog_upload':raise ValueError('Сначала открой «Просмотренное → Загрузить список».')
        sid=pending['sid'];self.catalog_session(uid,sid)
        name=str(document.get('file_name',''))
        if not name.lower().endswith(('.json','.csv','.tsv','.txt')):raise ValueError('Поддерживаются CSV, TSV и JSON.')
        if document.get('file_size',0)>1024*1024:raise ValueError('Файл должен быть не больше 1 МБ.')
        tg=self.tg
        def prepare():return {'rows':parse_history(tg.download_document(document),name),'name':name[:100]}
        self.catalog_submit(uid,sid,'preview',prepare)
        self.state['ui_input'].pop(str(uid),None)

    def catalog_poll(self):
        for key,future in list(self.catalog_jobs().items()):
            if not future.done():continue
            self.catalog_jobs().pop(key)
            uid,sid,nonce,task=key;s=self.state.get('catalogs',{}).get(str(uid))
            if uid not in self.users or not s or s['id']!=sid or s.get('nonce')!=nonce or s['expires']<time.time():continue
            old=getattr(self,'_ui_message',None);self._ui_message=s.get('message_id')
            try:
                data=future.result();s['phase']='ready'
                if task in ('people','collections','person','collection','pick'):s['revision']=s.get('revision',0)+1
                if task in ('people','collections'):
                    keyname='people' if task=='people' else 'collections';s[keyname]=data.get(keyname,[])[:30]
                    rows=[]
                    for index,item in enumerate(s[keyname]):
                        label=item.get('name') or item.get('title','')
                        if task=='people':label+=' · '+str(item.get('birthYear',''))
                        rows.append([(label[:95],'catalog_choose',sid,keyname,index)])
                    rows += [[('← Каталог','catalog_home')],self.ui_nav()]
                    self.ui_show(uid,'Выбери персону:' if task=='people' and rows[:-2] else 'Выбери подборку:' if task=='collections' else 'Ничего не найдено. Попробуй полное имя.',rows)
                elif task in ('person','collection','pick'):
                    s.update(movies=data.get('movies',[])[:1000],source=data.get('source',''),truncated=data.get('truncated',False),stale=data.get('stale',False))
                    s['mode']='random' if task=='pick' else task
                    s['title']=data.get('person',{}).get('name') or data.get('collection',{}).get('title') or 'Случайные фильмы'
                    if task=='pick':s['skip']=list(dict.fromkeys(s.get('skip',[])+[m['imdb_id'] for m in s['movies']]))[-5000:]
                    else:
                        for field in ('kind','role','order','list_years'):s.pop(field,None)
                    self.catalog_list(uid,sid)
                elif task=='card':
                    photo=data.pop('_photo',None);s['movies'][s['chosen']].update(data)
                    self.catalog_card_show(uid,sid,data,photo)
                elif task=='seen':
                    s['movies'][s['chosen']]['watched']=True
                    self.catalog_list(uid,sid)
                elif task=='history':
                    rows=[[('← Каталог','catalog_home')],self.ui_nav()]
                    if uid==self.tg.owner:rows.insert(0,[('Загрузить CSV/JSON','catalog_upload',sid)])
                    self.ui_show(uid,'В общем списке просмотренного: '+str(data.get('watchedCount',0))+'.\nОн используется и вебом, и ботом. Кнопка «Уже смотрел» дополняет этот список.',rows)
                elif task=='preview':
                    if uid!=self.tg.owner:continue
                    s['import_rows']=data['rows']
                    self.ui_show(uid,'Файл: '+data['name']+'\nСтрок: '+str(len(data['rows']))+'.\nВсе распознанные строки будут считаться просмотренными. Списки «Буду смотреть» загружать не нужно. Добавить в общий список?',[[('✅ Импортировать','catalog_import',sid)],[('Отмена','catalog_home')]])
                elif task=='import':
                    s.pop('import_rows',None)
                    self.ui_show(uid,f"Просмотренное обновлено. Распознано: {data.get('accepted',0)}, добавлено: {data.get('added',0)}, всего: {data.get('watchedCount',0)}.",[[('← Каталог','catalog_home')],self.ui_nav()])
                elif task=='jellyfin':
                    rows=[[('▶️ Открыть в Jellyfin','url',data['url'])]] if data.get('url') else []
                    self.ui_show(uid,data.get('reason') or 'Открой карточку в Jellyfin. При необходимости войди в свой профиль.',rows+[self.ui_nav()])
            except Exception as exc:
                s['phase']='error'
                message=str(exc) if isinstance(exc,(APIError,ValueError)) else 'Не удалось загрузить данные. Повтори запрос.'
                try:self.ui_show(uid,message,[[('← Каталог','catalog_home')],self.ui_nav()])
                except APIError:pass
            finally:self._ui_message=old;self.store.save()

        # Network futures do not survive a process restart; old menus stay recoverable.
        for uid,s in self.state.get('catalogs',{}).items():
            if s.get('phase')=='working' and not any(k[:3]==(int(uid),s['id'],s.get('nonce')) for k in self.catalog_jobs()):
                s['phase']='error'
                self.store.save()

    def catalog_action(self,uid,action,*args):
        if action=='catalog_home':self.catalog_home(uid,args[0] if args else 'home');return
        sid=args[0];s=self.catalog_session(uid,sid)
        if s.get('phase')=='working':raise ValueError('Запрос ещё выполняется. Можно отменить его и вернуться в каталог.')
        if action=='catalog_prompt':self.catalog_prompt(uid,sid,args[1])
        elif action=='catalog_load':self.catalog_load(uid,sid,args[1],args[2])
        elif action=='catalog_choose':
            kind,index=args[1:];items=s.get(kind,[])
            if kind not in ('people','collections') or not 0<=index<len(items):raise ValueError('Вариант больше недоступен')
            self.catalog_load(uid,sid,'person' if kind=='people' else 'collection',items[index]['id'])
        elif action=='catalog_random':self.catalog_random_menu(uid,sid)
        elif action=='catalog_options':self.catalog_options(uid,sid,args[1],args[2])
        elif action=='catalog_set':
            field,value=args[1:];options=GENRES if field in ('genre','excludeGenre') else COUNTRIES if field in ('country','excludeCountry') else (1,3,5,10) if field=='count' else ('','actor','director') if field=='role' else ('','movie','series') if field=='kind' else ('source','new','old','title') if field=='order' else ()
            if value not in options:raise ValueError('Недопустимый фильтр')
            if field in ('role','kind','order'):s[field]=value;self.catalog_list(uid,sid)
            else:s['filters'][field]=value;self.catalog_random_menu(uid,sid)
        elif action=='catalog_pick':
            payload=dict(s['filters'],skip=list(s.get('skip',[])))
            self.catalog_submit(uid,sid,'pick',CatalogClient().call,'/api/discover/pick',payload)
        elif action=='catalog_list':self.catalog_list(uid,sid,args[1] if len(args)>1 else 0)
        elif action=='catalog_reset_years':s.pop('list_years',None);self.catalog_list(uid,sid)
        elif action=='catalog_toggle_seen':s['exclude_watched']=not s.get('exclude_watched',True);self.catalog_list(uid,sid)
        elif action in ('catalog_card','catalog_release','catalog_seen'):
            index=args[1]
            if not 0<=index<len(s.get('movies',[])):raise ValueError('Фильм больше недоступен')
            movie=s['movies'][index]
            if action=='catalog_card':self.catalog_card(uid,sid,index)
            elif action=='catalog_release':
                query=(movie.get('title','')+' '+str(movie.get('year') or '')).strip()[:120]
                self.search_start(uid,query);search=self.state['searches'][str(uid)]
                search['kind']='series' if movie.get('kind')=='series' else 'movie'
                self.search_run(uid,search['id'])
            else:
                s['chosen']=index
                self.catalog_submit(uid,sid,'seen',CatalogClient().call,'/api/discover/seen',{'movie':dict(movie)})
        elif action=='catalog_history':self.catalog_load(uid,sid,'history','')
        elif action=='catalog_upload':
            if uid!=self.tg.owner:raise ValueError('Импорт доступен только владельцу.')
            self.state.setdefault('ui_input',{})[str(uid)]={'kind':'catalog_upload','sid':sid}
            self.ui_show(uid,'Пришли CSV/JSON-файл просмотренного до 1 МБ (UTF-8). Можно использовать IMDb/TMDB/Кинопоиск ID либо название и год. После проверки потребуется подтверждение. По ссылке профиля Кинопоиска импорт пока не выполняется.',[[('Отмена','catalog_home')]])
        elif action=='catalog_import':
            if uid!=self.tg.owner or not s.get('import_rows'):raise ValueError('Подготовленный импорт недоступен.')
            self.catalog_submit(uid,sid,'import',CatalogClient().call,'/api/discover/watched',{'rows':list(s['import_rows'])})
        else:raise ValueError('Меню устарело. Открой /discover.')
        self.store.save()

    def catalog_jellyfin(self,uid,pack,index=None):
        self.catalog_home(uid);s=self.state['catalogs'][str(uid)]
        def load():
            client=CatalogClient();item=client.call('/api/jellyfin/link?'+urlencode({'pack':pack,**({'index':index} if index is not None else {})}))
            if not item.get('available'):return {'reason':item.get('reason','Файл ещё не проиндексирован')}
            settings=client.call('/api/web-settings');base=settings.get('jellyfinUrl') or os.environ.get('JELLYFIN_PUBLIC_URL')
            if not base:
                host=os.environ.get('BIND_IP','')
                if not host or host in ('0.0.0.0','127.0.0.1'):return {'reason':'Задай адрес Jellyfin в настройках веб-интерфейса.'}
                base='http://'+host+':'+os.environ.get('JELLYFIN_PORT','8096')
            parsed=urlsplit(base)
            if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.username or parsed.password:raise ValueError('Недопустимый адрес Jellyfin')
            return {'url':base.rstrip('/')+'/web/index.html#!/details?'+urlencode({'id':item['itemId'],'serverId':item['serverId']})}
        self.catalog_submit(uid,s['id'],'jellyfin',load)
