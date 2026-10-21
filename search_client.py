"""Jackett search and metadata download; no tracker URLs or secrets in errors."""
import html
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.etree import ElementTree
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, parse_qsl
from urllib.request import Request, HTTPRedirectHandler, build_opener

from core import magnet_hash
from net import APIError
from torrent_meta import MAX_TORRENT, torrent_hash


def plain(value, limit=500):
    text = html.unescape(re.sub(r'<[^>]*>', '', str(value or '')))
    return re.sub(r'\s+', ' ', re.sub(r'[\x00-\x1f\x7f]', ' ', text)).strip()[:limit]


def quality(title):
    found = []
    for pattern in (r'\b(?:2160|1080|720|576|480)[pi]\b|\b(?:4K|UHD)\b',
                    r'\b(?:BluRay|BDRemux|REMUX|BDRip|WEB[ .-]?DL|WEBRip|HDRip|DVDRip|HDTV|CAMRip|TS)\b',
                    r'\b(?:HEVC|AV1|HDR10\+?|HDR|Dolby Vision)\b'):
        match = re.search(pattern, title, re.I)
        if match:
            found.append(match[0])
    return ' · '.join(found) or 'качество не указано'


def voices(title):
    """Only name studios explicitly present in the release title."""
    patterns = [
        (r'кураж[ ._-]*б[ао]мбей|kuraj[ ._-]*bamb[ae]y|kurazh[ ._-]*bamb[ae]y|kurazh|kuraj', 'Кураж-Бамбей'),
        (r'paramount[ ._-]*comedy', 'Paramount Comedy'),
        (r'lost[ ._-]*film', 'LostFilm'), (r'new[ ._-]*studio', 'NewStudio'),
        (r'кубик[ ._-]*в[ ._-]*кубе|kubik[ ._-]*v[ ._-]*kube', 'Кубик в Кубе'),
        (r'alex[ ._-]*film', 'AlexFilm'), (r'baiba[ck]o|байбако', 'BaibaKo'),
        (r'jaskier|яскиер', 'Jaskier'), (r'cold[ ._-]*film', 'ColdFilm'),
        (r'idea[ ._-]*film', 'IdeaFilm'), (r'fox[ ._-]*life', 'Fox Life'),
        (r'novafilm|новафильм', 'NovaFilm'), (r'ams[tт]udio', 'AMedia Studio'),
        (r'amedia|амедиа', 'AMedia'), (r'red[ ._-]*head[ ._-]*sound', 'Red Head Sound'),
        (r'дубляж', 'Дубляж'), (r'гоблин|goblin|пучков', 'Дмитрий Пучков (Гоблин)'),
        (r'гаврилов', 'Андрей Гаврилов'), (r'володарский', 'Леонид Володарский'),
        (r'ж[иы]вов', 'Юрий Живов'), (r'сербин', 'Юрий Сербин'),
        (r'резка|hdrezka', 'HDRezka'), (r'tvshows', 'TVShows'),
        (r'le[ ._-]*production', 'LE-Production'), (r'лостфильм', 'LostFilm'),
        (r'пифагор', 'Пифагор'), (r'невафильм', 'Невафильм'),
        (r'мосфильм(?:[ -]*мастер)?', 'Мосфильм'), (r'св[ -]*дубль', 'СВ-Дубль'),
        (r'озз|ozz', 'Ozz'), (r'greb[ ._-]*creative', 'Greb&Creative'),
    ]
    found = []
    for pattern, label in patterns:
        if re.search(r'(?<!\w)(?:'+pattern+r')(?!\w)', title, re.I) and label not in found:
            found.append(label)
    # Preserve unfamiliar studio names instead of limiting the display to a dictionary.
    for match in re.finditer(r'(?i)\b(?:MVO|DVO|AVO|VO|DUB)\s*\(([^)]+)\)', title):
        label = plain(match[1], 100).strip(' -')
        if label and not any(re.search(pattern, label, re.I) for pattern, _ in patterns):
            found.append(label)
    original = bool(re.search(r'(?i)\b(?:original|оригинал)\b', title))
    codes = re.findall(r'(?i)\b(?:MVO|DVO|AVO|VO|DUB)\b', title)
    if not found and codes:
        found.append(' / '.join(dict.fromkeys(c.upper() for c in codes)) + ' — студия не указана')
    if original:
        found.append('Оригинал')
    if found:
        return ', '.join(dict.fromkeys(found))[:240]
    return 'не указаны в названии'


def voice_tags(title):
    # Studios, translation type and language are separate facets, not one display string.
    tags = {v.strip() for v in voices(title).split(', ') if v.strip() and 'студия не указана' not in v and v!='не указаны в названии'}
    tags.update(v.upper() for v in re.findall(r'(?i)\b(?:MVO|DVO|AVO|VO|DUB)\b',title))
    for code,pattern in [('Rus',r'(?i)\b(?:rus|russian|рус(?:ский|ская|ское)?)\b'),
                         ('Ukr',r'(?i)\b(?:ukr|ukrainian|укр(?:аинский)?)\b')]:
        # Subtitle language is not an audio track.
        audio = re.split(r'(?i)\bsub(?:titles)?\b|субтитры',title)[0]
        if re.search(pattern,audio): tags.add(code)
    if any(v in tags for v in ('Кураж-Бамбей','LostFilm','NewStudio','Paramount Comedy','Кубик в Кубе','HDRezka')):
        tags.add('Rus')
    return tags


def resolution(title):
    m = re.search(r'(?i)\b(2160|1080|720|576|480)([pi])\b', title)
    if m:
        return m[1]+m[2].lower()
    return '2160p' if re.search(r'(?i)\b(?:4K|UHD)\b', title) else 'unknown'


def release_numbers(title):
    """Bounded hints from release titles; never treat years/resolution as seasons."""
    title = title.replace('–', '-').replace('—', '-')
    seasons = set()
    patterns = [r'(?i)\bS(\d{1,3})(?:\s*-\s*S?(\d{1,3}))?',
                r'(?i)(?:сезон(?:ы|ов)?|seasons?)\s*[:：]?\s*(\d{1,3})(?:\s*-\s*(\d{1,3}))?',
                r'(?i)(?<!\d)(\d{1,3})(?:\s*-\s*(\d{1,3}))?\s*(?:сезон(?:ы|ов)?|seasons?)\b']
    for pattern in patterns:
        for m in re.finditer(pattern, title):
            start, end = int(m[1]), int(m[2] or m[1])
            if 0 <= start <= end <= 999 and end-start < 100:
                seasons.update(range(start, end+1))
    episodes = []
    patterns = [r'(?i)\bS\d{1,3}E(\d{1,4})(?:\s*-\s*E?(\d{1,4}))?',
                r'(?i)(?:серии?|эпизоды?|episodes?)\s*[:：]?\s*(\d{1,4})(?:\s*-\s*(\d{1,4}))?',
                r'(?i)(?<!\d)(\d{1,4})(?:\s*-\s*(\d{1,4}))?\s*(?:из|of)\s*\d{1,4}']
    if len(seasons) == 1:
        for pattern in patterns:
            m = re.search(pattern, title)
            if m:
                start, end = int(m[1]), int(m[2] or m[1])
                if 1 <= start <= end <= 9999:
                    episodes = [start, end]
                    break
    return {'seasons': sorted(seasons), 'episodes': episodes}


def number(value):
    try:
        return max(0, int(value)) if value is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Jackett:
    def __init__(self):
        self.base = os.environ.get('JACKETT_URL', 'http://jackett:9117').rstrip('/')
        self.key = os.environ.get('JACKETT_API_KEY', '')

    def request(self, path, query=None, limit=6 * 1024 * 1024, timeout=5):
        if not self.key:
            raise ValueError('Поиск Jackett ещё не настроен. Добавление по magnet доступно через /add.')
        url = self.base + path + ('?' + urlencode(query) if query else '')
        try:
            with build_opener(NoRedirect).open(Request(url, headers={'Accept':'application/json'}), timeout=timeout) as response:
                data = response.read(limit+1)
        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get('Location', '')
                if location.startswith('magnet:'):
                    magnet_hash(location)
                    return location.encode()
            raise APIError('Jackett не смог обработать запрос. Проверь индексаторы в его настройках.') from None
        except (URLError, OSError, TimeoutError):
            raise APIError('Jackett или трекер не ответил вовремя. Попробуй повторить поиск.') from None
        if len(data) > limit:
            raise ValueError('Ответ Jackett слишком большой. Уточни название для поиска.')
        return data

    def indexers(self):
        raw = self.request('/api/v2.0/indexers/all/results/torznab/api',
                           {'apikey':self.key, 't':'indexers', 'configured':'true'})
        try:
            root = ElementTree.fromstring(raw)
            ids = [e.attrib.get('id','') for e in root.findall('indexer')]
        except (ElementTree.ParseError, ValueError):
            raise APIError('Не удалось получить список трекеров Jackett.') from None
        ids = [i for i in dict.fromkeys(ids) if re.fullmatch(r'[a-z0-9][a-z0-9_-]*', i)]
        if len(ids)>16:
            raise ValueError('Для поиска подключено больше 16 трекеров. Сократи список в Jackett.')
        return ids

    def search(self, query, kind):
        ids = self.indexers()
        payload = {'Results':[], 'Indexers':[]}
        if ids:
            # Independent requests keep working trackers available when another one times out.
            with ThreadPoolExecutor(max_workers=min(4,len(ids)), thread_name_prefix='indexer') as pool:
                pending = {pool.submit(self.request, '/api/v2.0/indexers/'+i+'/results',
                                       {'apikey':self.key,'Query':query}):i for i in ids}
                for future in as_completed(pending):
                    i = pending[future]
                    try:
                        page = json.loads(future.result())
                        if not isinstance(page, dict) or not isinstance(page.get('Results'),list) or not isinstance(page.get('Indexers'),list) or not page['Indexers']:
                            raise ValueError()
                        payload['Results'].extend(page['Results'])
                        payload['Indexers'].extend(page['Indexers'])
                    except (APIError, ValueError, KeyError, TypeError, UnicodeError, OSError):
                        payload['Indexers'].append({'Name':i,'Status':1})
        return self.normalize(payload, kind)

    def normalize(self, payload, kind):
        try:
            entries = payload['Results']
            indexers = payload.get('Indexers', [])
            if not isinstance(entries, list) or not isinstance(indexers, list):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise APIError('Jackett вернул неожиданный ответ. Проверь его настройки.') from None
        if not indexers:
            raise ValueError('В Jackett пока нет подключённых трекеров. Добавь их на странице Jackett.')
        failures = [plain(i.get('Name') or i.get('ID') or 'трекер', 60) for i in indexers if i.get('Status') not in (2, 'OK')]
        rows, seen = [], set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = plain(entry.get('Title'),1500)
            magnet = entry.get('MagnetUri') or ''
            link = entry.get('Link') or ''
            try:
                identity = magnet_hash(magnet) if magnet else link
            except ValueError:
                magnet = ''
                identity = link
            categories = [number(n) for n in entry.get('Category', [])]
            if any(n is not None and 6000 <= n < 7000 for n in categories):
                continue
            relevant = (2000, 3000) if kind == 'movie' else (5000, 6000)
            known = [n for n in categories if n is not None and 1000 <= n < 8000]
            if known and not any(relevant[0] <= n < relevant[1] for n in known):
                continue
            if not title or not identity or identity in seen:
                continue
            seen.add(identity)
            rows.append({'title':title, 'size':number(entry.get('Size')), 'seeders':number(entry.get('Seeders')),
                         'peers':number(entry.get('Peers')), 'tracker':plain(entry.get('Tracker'),60),
                         'quality':quality(title), 'magnet':magnet, 'link':link})
        rows.sort(key=lambda r: (-(r['seeders'] if r['seeders'] is not None else -1), r['size'] or 0, r['title']))
        if not rows and failures and len(failures) == len(indexers):
            raise APIError('Трекеры не ответили: ' + ', '.join(failures) + '. Проверь подключение и вход в Jackett.')
        return {'results':rows[:500], 'total':len(rows), 'warnings':failures}

    def resolve(self, result):
        if result.get('magnet'):
            magnet_hash(result['magnet'])
            return result['magnet'], None
        link = urlsplit(result.get('link') or '')
        base = urlsplit(self.base)
        prefix = base.path.rstrip('/') + '/dl/'
        if (link.scheme != base.scheme or link.netloc != base.netloc or not link.path.startswith(prefix) or
                link.username or link.password or link.fragment or '..' in link.path.split('/')):
            raise ValueError('Jackett не дал безопасную ссылку на torrent-файл. Попробуй другую раздачу.')
        data = self.request(link.path, dict(parse_qsl(link.query)), limit=MAX_TORRENT)
        if data.startswith(b'magnet:'):
            magnet = data.decode(); magnet_hash(magnet)
            return magnet, None
        h = torrent_hash(data)
        return 'magnet:?xt=urn:btih:' + h, data
