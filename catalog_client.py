"""Bot adapter to the shared web catalogue. No TMDB token or separate history."""
import csv
import io
import json
import os
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from urllib.error import HTTPError, URLError
from search_client import NoRedirect
from net import APIError


class CatalogClient:
    def __init__(self):
        self.base = os.environ.get('CATALOG_URL','http://web:8787').rstrip('/')
        url = urlsplit(self.base)
        if url.scheme not in ('http','https') or not url.netloc or url.username or url.password:
            raise ValueError('Некорректный адрес общего каталога')
        self.headers = {'Accept':'application/json'}
        if os.environ.get('API_TOKEN'):
            self.headers['Authorization'] = 'Bearer '+os.environ['API_TOKEN']

    def call(self, route, body=None, binary=False):
        if not route.startswith('/api/') or '\n' in route:
            raise ValueError('Неизвестный запрос каталога')
        headers = dict(self.headers)
        payload = None
        if body is not None:
            payload = json.dumps(body,ensure_ascii=False).encode()
            headers['Content-Type']='application/json'
        try:
            with build_opener(NoRedirect).open(Request(self.base+route,data=payload,headers=headers),timeout=90) as response:
                raw=response.read(6*1024*1024+1)
            if len(raw)>6*1024*1024:raise APIError('Ответ каталога слишком большой. Уточни поиск.')
            return raw if binary else json.loads(raw)
        except HTTPError as exc:
            if exc.code==400:
                try:
                    detail=json.loads(exc.read(2000)).get('detail','')
                    if isinstance(detail,str) and len(detail)<350:raise APIError(detail) from None
                except (ValueError,UnicodeError):pass
            raise APIError('Общий каталог временно недоступен. Повтори запрос позже.') from None
        except (URLError,TimeoutError,OSError,ValueError):
            raise APIError('Общий каталог не ответил. Очередь продолжает работать.') from None

    def card(self, movie):
        identifier=movie['imdb_id']
        data=self.call('/api/discover/details',{'ids':[identifier]})
        cards=data.get('movies',[])
        if not cards:raise APIError('Карточка больше недоступна. Открой список снова.')
        result=dict(cards[0])
        try:result['_photo']=self.call('/api/discover/poster/'+identifier,binary=True)
        except APIError:pass
        return result


def parse_history(raw, name):
    if len(raw)>1024*1024:raise ValueError('Файл должен быть не больше 1 МБ.')
    try:text=raw.decode('utf-8-sig')
    except UnicodeError:raise ValueError('Нужен CSV/JSON в кодировке UTF-8.') from None
    if name.lower().endswith('.json'):
        value=json.loads(text)
        if isinstance(value,dict):
            value=next((value[k] for k in ('watched','ratings','movies','films','items') if isinstance(value.get(k),list)),None)
        rows=value
    elif name.lower().endswith(('.csv','.tsv','.txt')):
        first=text.splitlines()[0] if text.splitlines() else ''
        delimiter=max((';',',','\t'),key=first.count)
        rows=list(csv.DictReader(io.StringIO(text),delimiter=delimiter))
    else:raise ValueError('Поддерживаются CSV, TSV и JSON. Выгрузка профиля Кинопоиска по ссылке пока не поддерживается.')
    if not isinstance(rows,list) or not rows or len(rows)>20000 or not all(isinstance(r,dict) for r in rows):
        raise ValueError('Нужен список от 1 до 20 000 фильмов.')
    keys={'imdb_id','imdbId','imdb','IMDb ID','tmdb_id','tmdbId','kinopoisk_id','kinopoiskId','kp_id','url','link','title','name','Название','Name'}
    if not any(keys.intersection(row) for row in rows):raise ValueError('Не найдены ID фильмов или названия. Проверь формат файла.')
    return rows


def public_web_url():
    address=os.environ.get('WATCHNEXT_PUBLIC_URL','').strip()
    if not address:
        host=os.environ.get('BIND_IP','').strip()
        address='http://'+host+':'+os.environ.get('WEB_PORT','8787') if host and host not in ('0.0.0.0','127.0.0.1') else ''
    url=urlsplit(address)
    return address.rstrip('/') if url.scheme in ('http','https') and url.netloc and not url.username and not url.password else ''
