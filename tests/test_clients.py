import json
import os
import unittest
from unittest.mock import patch
from core import low_seasons
from net import Jellyfin, Qbit, Telegram, RichText

class HTTPFake:
    def __init__(self): self.calls = []
    def call(self, path, data=None, query=None, **kwargs):
        self.calls.append((path, data, query))
        if path.endswith('/login'): return 'Ok.'
        if path.endswith('/version'): return 'v5.2.4'
        if path.endswith('/preferences'): return {'use_unwanted_folder': True}
        if path == '/Users': return [{'Name': 'viewer', 'Id': 'u'}]
        if path == '/Items':
            return {'Items': [{'Id': 'episode', 'Path': '/data/library/episode.mkv',
                               'UserData': {'Played': True}}], 'TotalRecordCount': 1}
        if path == '/Sessions': return [{'NowPlayingItem': {'Id': 'episode'}}]
        if path.endswith('/sendMessage'): return {'ok': True, 'result': {'message_id': 7}}
        return ''

class Clients(unittest.TestCase):
    @patch.dict(os.environ, {'QBIT_PASSWORD': 'test-only'})
    def test_qbit_paths_preferences_and_metadata_only_add(self):
        q = Qbit(); q.http = HTTPFake(); q.login(); q.login()
        calls = q.http.calls
        self.assertEqual(sum(p.endswith('/login') for p, _, _ in calls), 1)
        required = json.loads(next(d['json'] for p, d, _ in calls if p.endswith('/setPreferences')))
        self.assertFalse(required['use_unwanted_folder']); self.assertFalse(required['preallocate_all'])
        q.add({'magnet': 'magnet:example', 'download_root': '/data/downloads/a'})
        data = calls[-1][1]
        self.assertEqual(data['stopCondition'], 'MetadataReceived')
        self.assertEqual(data['autoTMM'], 'false')
        self.assertEqual(data['useDownloadPath'], 'false')
    @patch.dict(os.environ, {'QBIT_PASSWORD': 'test-only'})
    def test_qbit_accepts_empty_success_reply(self):
        q = Qbit()
        class EmptyLoginHTTP(HTTPFake):
            def call(self, path, **kwargs):
                if path.endswith('/login'): return ''
                return super().call(path, **kwargs)
        q.http = EmptyLoginHTTP()
        q.login()
        self.assertTrue(q.logged_in)
    @patch.dict(os.environ, {'JELLYFIN_API_KEY': 'test-only', 'JELLYFIN_USERNAME': 'viewer'})
    def test_jellyfin_resolves_active_item_id_to_managed_path(self):
        j = Jellyfin(); j.http = HTTPFake()
        played, active = j.snapshot()
        self.assertIn('/data/library/episode.mkv', played)
        self.assertIn('/data/library/episode.mkv', active)
        self.assertEqual(j.http.calls[1][2]['userId'], 'u')
    @patch.dict(os.environ, {'JELLYFIN_API_KEY': 'test-only'})
    def test_jellyfin_uses_authorization_supported_by_12(self):
        j = Jellyfin()
        self.assertEqual(j.http.headers, {'Authorization': 'MediaBrowser Token="test-only"', 'Accept':'application/json; profile=PascalCase'})

    @patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test-only', 'TELEGRAM_OWNER_ID': '123'})
    def test_send_uses_configured_private_owner(self):
        tg = Telegram(); tg.http = HTTPFake()
        self.assertEqual(tg.send('example'), 7)
        self.assertEqual(tg.http.calls[0][1]['chat_id'], 123)
    @patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN':'test-only','TELEGRAM_OWNER_ID':'123'})
    def test_html_is_explicit_and_edit_keeps_format(self):
        tg=Telegram();tg.http=HTTPFake()
        tg.send(RichText('<b>Title</b> &amp; text'))
        self.assertEqual(tg.http.calls[-1][1]['parse_mode'],'HTML')
        with patch.object(tg,'call',return_value=True) as call:
            tg.edit(RichText('<b>Changed</b>'),123,7,{})
            self.assertEqual(call.call_args.kwargs['parse_mode'],'HTML')
        tg.send('User <title> & ordinary notification')
        self.assertNotIn('parse_mode',tg.http.calls[-1][1])

    def test_pending_next_season_suppresses_old_request(self):
        seasons = [{'title': 'Show', 'season': 1, 'episodes': [{'watched': False}]},
                   {'title': 'Show', 'season': 2, 'episodes': []}]
        self.assertEqual(low_seasons(seasons, 2), [])

if __name__ == '__main__': unittest.main()
