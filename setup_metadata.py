"""Enable Russian TMDb metadata and artwork without replacing existing images.
Run from the compose directory after Jellyfin setup: python3 setup_metadata.py
"""
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from net import HTTP


def main():
    values = dict(os.environ)
    if Path('.env').exists():
        for line in Path('.env').read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                key, _, value = line.partition('=')
                values.setdefault(key, value.strip("'\""))
    base = values.get('JELLYFIN_SETUP_URL', 'http://' + values.get('BIND_IP', '127.0.0.1') + ':' + values.get('JELLYFIN_PORT','8096')).rstrip('/')
    headers = {'Authorization': 'MediaBrowser Token="' + values['JELLYFIN_API_KEY'] + '"', 'Accept':'application/json; profile=PascalCase'}
    j = HTTP(base, headers)
    libraries = j.call('/Library/VirtualFolders')
    backup = Path('state/backups/libraries-before-online-metadata.json')
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        backup.write_text(json.dumps(libraries, ensure_ascii=False, indent=2))
    for lib in libraries:
        if lib.get('CollectionType') not in ('movies', 'tvshows'):
            continue
        options = lib['LibraryOptions']
        available = j.call('/Libraries/AvailableOptions', query={
            'libraryContentType': lib['CollectionType'], 'isNewLibrary': 'true'})
        types = {entry['Type']: entry for entry in options.get('TypeOptions', [])}
        for item in available['TypeOptions']:
            entry = types.setdefault(item['Type'], {'Type': item['Type']})
            for field in ('MetadataFetchers', 'ImageFetchers'):
                names = [provider['Name'] for provider in item[field]]
                enabled = [name for name in entry.get(field, []) if name in names]
                if 'TheMovieDb' in names and 'TheMovieDb' not in enabled:
                    enabled.insert(0, 'TheMovieDb')
                if field == 'ImageFetchers' and 'Screen Grabber' in names and 'Screen Grabber' not in enabled:
                    enabled.append('Screen Grabber')
                entry[field] = enabled
                entry['MetadataFetcherOrder' if field == 'MetadataFetchers' else 'ImageFetcherOrder'] = enabled
            entry.setdefault('ImageOptions', item['DefaultImageOptions'])
        options.update(EnableInternetProviders=True, PreferredMetadataLanguage='ru',
                       MetadataCountryCode='RU', AutomaticRefreshIntervalDays=30, TypeOptions=list(types.values()))
        body = json.dumps({'Id': lib['ItemId'], 'LibraryOptions': options}).encode()
        with urlopen(Request(base + '/Library/VirtualFolders/LibraryOptions', data=body,
                             headers={**headers, 'Content-Type': 'application/json'}), timeout=30) as response:
            assert response.status == 204
        print('Online metadata and images enabled:', lib['Name'])
    j.call('/Library/Refresh', data={}, raw=True)
    start = 0
    while True:
        page = j.call('/Items', query={'recursive': 'true', 'includeItemTypes': 'Series,Season,Episode,Movie',
                                      'startIndex': start, 'limit': 100})
        for item in page['Items']:
            j.call('/Items/' + item['Id'] + '/Refresh?metadataRefreshMode=Default&imageRefreshMode=Default'
                   '&replaceAllMetadata=false&replaceAllImages=false', data={}, raw=True)
        start += len(page['Items'])
        if not page['Items'] or start >= page['TotalRecordCount']:
            break
    print('Refresh of missing metadata and artwork queued; existing images and viewing history preserved.')


if __name__ == '__main__':
    main()
