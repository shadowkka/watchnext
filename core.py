"""Pure queue policy; sizes are decimal bytes (GB = 10**9)."""
import base64
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

GB = 10**9
VIDEO = {'.mkv', '.mp4', '.avi', '.m4v', '.ts', '.mov', '.webm'}


def magnet_hash(value):
    parsed = urlsplit(value.strip())
    if parsed.scheme != 'magnet':
        raise ValueError('Нужна magnet-ссылка.')
    for xt in parse_qs(parsed.query).get('xt', []):
        if xt.lower().startswith('urn:btih:'):
            h = xt[9:]
            if re.fullmatch(r'[a-fA-F0-9]{40}', h):
                return h.lower()
            if re.fullmatch(r'[a-zA-Z2-7]{32}', h):
                return base64.b32decode(h.upper()).hex()
    raise ValueError('Нужен magnet с BTIH (v1 или hybrid). Чистый v2 пока не поддерживается.')


def safe_path(root, relative):
    root = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise ValueError('Недопустимый путь файла.')
    target = root / rel
    # Reject symlinks even when they point inside the managed tree.
    cursor = target
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError('Символические ссылки в управляемой папке запрещены.')
        cursor = cursor.parent
    if not target.resolve().is_relative_to(root):
        raise ValueError('Путь выходит за пределы управляемой папки.')
    return target


def slug(title):
    s = re.sub(r'[^\w .()-]', '_', title, flags=re.UNICODE).strip(' .')
    if not s or len(s) > 100:
        raise ValueError('Название должно содержать от 1 до 100 допустимых символов.')
    return s


def parse_start(value):
    text = str(value).strip()
    match = re.fullmatch(r'(?i)S(\d{1,3})[ ._-]*E(\d{1,4})', text)
    if not match:
        match = re.fullmatch(r'(\d{1,3})(?:[ x:,]+(\d{1,4}))?', text)
    if not match:
        raise ValueError('Начало: S02E05 или сезон и серия: 2 5.')
    season = int(match[1])
    number = int(match[2] or 1)
    if not 0 <= season <= 999 or not 1 <= number <= 9999:
        raise ValueError('Сезон 0–999, серия 1–9999.')
    return season, number


def episode_count(episode):
    return episode.get('end_number', episode.get('number', 1)) - episode.get('number', 1) + 1


def last_season(pack):
    return max((e.get('season', pack['season']) for e in pack.get('episodes', [])), default=pack['season'])


def episode_label(episode, fallback=1):
    label = f"S{episode.get('season', fallback):02d}E{episode['number']:02d}"
    end = episode.get('end_number', episode['number'])
    return label + (f'-E{end:02d}' if end != episode['number'] else '')


def _folder_season(path, fallback):
    for part in reversed(path.parts[:-1]):
        m = re.search(r'(?i)(?:^|[\W_])(?:season|сезон|s)[ ._-]*(\d{1,3})(?!\d)', part)
        if not m:
            m = re.search(r'(?i)(?:^|[\W_])(\d{1,3})[ ._-]*(?:сезон|season)(?:$|[\W_])', part)
        if m:
            return int(m[1])
    return fallback


def _range_tail(tail, first, season):
    """Only consume an adjacent episode/range token, never resolution or a year in a title."""
    explicit = re.match(r'(?i)^[ ._+\-]*(?:S(\d{1,3})[ ._-]*)?E(\d{1,4})(?=$|[^\da-zа-я]|E\d)', tail)
    bare = re.match(r'(?i)^\s*[-–]\s*(\d{1,4})(?![\da-zа-я])', tail)
    if explicit:
        if explicit[1] is not None and int(explicit[1]) != season:
            raise ValueError('Один видеофайл содержит разные сезоны; его нужно разделить.')
        end = int(explicit[2])
        prefix = tail[:explicit.start(2)]
        if '-' not in prefix and '–' not in prefix and end != first + 1:
            raise ValueError('В объединённом файле номера серий идут не подряд.')
        rest = tail[explicit.end():]
        if re.match(r'(?i)^[ ._+\-]*E\d', rest):
            if end != first + 1:
                raise ValueError('В объединённом файле номера серий идут не подряд.')
            return _range_tail(rest, end, season)
    elif bare:
        end = int(bare[1])
    else:
        return first
    if end <= first or end - first > 99:
        raise ValueError('Неоднозначный диапазон серий в имени файла.')
    return end


def episodes(files, season, start_episode=1, require_start=True):
    """Parse every season in one torrent; one row per physical video, including ranges."""
    result, covered = [], set()
    for f in files:
        p = Path(f['name'])
        if p.suffix.lower() not in VIDEO or re.search(r'(?i)(?:^|[\W_])(samples?|trailers?)(?:$|[\W_])', str(p)):
            continue
        m = re.search(r'(?i)(?:^|[\W_])S(\d{1,3})[ ._-]*E(\d{1,4})(?!\d)', p.stem)
        if not m:
            m = re.search(r'(?i)(?<!\d)(\d{1,3})x(\d{1,4})(?!\d)', p.stem)
        if m:
            actual_season, number = int(m[1]), int(m[2])
        else:
            actual_season = _folder_season(p, season)
            m = re.search(r'(?i)(?:^|[\W_])(?:episode|ep|e|серия|серии)[ ._-]*(\d{1,4})(?!\d)', p.stem)
            if not m:
                m = re.match(r'^(\d{1,3})(?=$|[ ._\-])', p.stem)
            if not m:
                raise ValueError(f'Не распознана серия: {p.name}. Нужен номер S01E01, 1x01, «Серия 01» или 01 в начале имени.')
            number = int(m[1])
        end = _range_tail(p.stem[m.end():], number, actual_season)
        if number < 1 or int(f['size']) <= 0:
            raise ValueError('Некорректный номер или размер серии.')
        ids = {(actual_season, n) for n in range(number, end + 1)}
        if covered & ids:
            raise ValueError(f'Дубли серий в раздаче: {p.name}. Выбери раздачу с одной версией каждой серии.')
        covered.update(ids)
        skipped = (actual_season, end) < (season, start_episode)
        result.append({'index': int(f['index']), 'name': f['name'], 'size': int(f['size']),
                       'season': actual_season, 'number': number, 'end_number': end,
                       'skipped_start': skipped, 'watched': False, 'removed': skipped,
                       'selected': False, 'published': False, 'seen_at': None})
    if not result:
        raise ValueError('В раздаче нет распознаваемых видеофайлов серий.')
    if require_start and all(e['skipped_start'] for e in result):
        raise ValueError(f'В раздаче нет серий начиная с S{season:02d}E{start_episode:02d}.')
    return sorted(result, key=lambda e: (e['season'], e['number']))


def movie_files(files):
    candidates = [f for f in files if Path(f['name']).suffix.lower() in VIDEO and
                  int(f['size']) > 0 and not re.search(r'(?i)(?:^|[\W_])(samples?|trailers?)(?:$|[\W_])', f['name'])]
    if not candidates:
        raise ValueError('В раздаче фильма не найден видеофайл.')
    f = max(candidates, key=lambda f: (int(f['size']), -int(f['index'])))
    return [{'index': int(f['index']), 'name': f['name'], 'size': int(f['size']),
             'season': 1, 'number': 1, 'end_number': 1, 'watched': False,
             'removed': False, 'selected': False, 'published': False, 'seen_at': None}]


def finished(episode):
    """A manually discarded episode is consumed without marking it watched in Jellyfin."""
    return episode.get('watched', False) or episode.get('discarded', False) or episode.get('skipped_start', False)


def plan(rows, count, cap, available):
    """Preserve reservations, then extend FIFO. count_units=0 exempts movies from episode count, not bytes."""
    chosen = {key for key, e in rows if e['selected'] and not finished(e)}
    used = sum(e['size'] for key, e in rows if key in chosen)
    units = sum(e.get('count_units', episode_count(e)) for key, e in rows if key in chosen)
    for key, e in rows:
        if finished(e) or key in chosen:
            continue
        if count is not None and units + e.get('count_units', episode_count(e)) > count:
            break
        if cap is not None and used + e['size'] > cap:
            break
        if e['size'] > available:
            break
        chosen.add(key)
        used += e['size']
        units += e.get('count_units', episode_count(e))
        available -= e['size']
    return chosen


def low_seasons(seasons, threshold):
    """Ask only about the latest registered season of each show, at most once."""
    latest = {}
    for s in seasons:
        if s.get('kind') == 'movie' or s.get('phase') == 'missing':
            continue
        key = s['title'].casefold()
        if key not in latest or last_season(s) > last_season(latest[key]):
            latest[key] = s
    return [s for s in latest.values() if s.get('episodes') and not s.get('error') and not s.get('notified') and
            sum(episode_count(e) for e in s['episodes'] if not finished(e)) <= threshold]
