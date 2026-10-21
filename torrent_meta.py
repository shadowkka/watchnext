"""Bounded bencode parsing: hash the original info bytes, never a re-encoding."""
import hashlib
import re

MAX_TORRENT = 8 * 1024 * 1024


def torrent_hash(data):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_TORRENT:
        raise ValueError('Неверный размер torrent-файла (максимум 8 МБ).')
    info_span = None
    nodes = 0

    def parse(pos, depth=0):
        nonlocal info_span, nodes
        nodes += 1
        if depth > 48 or nodes > 200000 or pos >= len(data):
            raise ValueError('Некорректный torrent-файл.')
        token = data[pos:pos+1]
        if token == b'i':
            end = data.find(b'e', pos+1)
            raw = data[pos+1:end]
            if end < 0 or not re.fullmatch(rb'(?:0|-?[1-9][0-9]{0,19})', raw):
                raise ValueError('Некорректное число в torrent-файле.')
            return int(raw), end+1
        if token in (b'd', b'l'):
            mapping = token == b'd'
            result = {} if mapping else []
            pos += 1
            while pos < len(data) and data[pos:pos+1] != b'e':
                if mapping:
                    key, pos = parse(pos, depth+1)
                    if not isinstance(key, bytes) or key in result:
                        raise ValueError('Некорректный словарь в torrent-файле.')
                    start = pos
                    value, pos = parse(pos, depth+1)
                    result[key] = value
                    if depth == 0 and key == b'info':
                        info_span = (start, pos)
                else:
                    value, pos = parse(pos, depth+1)
                    result.append(value)
            if pos >= len(data):
                raise ValueError('Незавершённый torrent-файл.')
            return result, pos+1
        colon = data.find(b':', pos, pos+12)
        raw = data[pos:colon]
        if colon < 0 or not re.fullmatch(rb'(?:0|[1-9][0-9]{0,8})', raw):
            raise ValueError('Некорректная строка в torrent-файле.')
        end = colon+1+int(raw)
        if end > len(data):
            raise ValueError('Незавершённый torrent-файл.')
        return data[colon+1:end], end

    root, end = parse(0)
    if end != len(data) or not isinstance(root, dict) or not info_span:
        raise ValueError('Не найдено описание раздачи в torrent-файле.')
    info = root[b'info']
    if (not isinstance(info, dict) or not isinstance(info.get(b'pieces'), bytes) or
            not info[b'pieces'] or len(info[b'pieces']) % 20 or
            not isinstance(info.get(b'piece length'), int) or info[b'piece length'] <= 0):
        raise ValueError('Нужен torrent-файл v1 или hybrid с BTIH.')
    return hashlib.sha1(data[slice(*info_span)]).hexdigest()
