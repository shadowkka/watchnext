import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import HTTPException
import server
from integrations import load_settings, public_settings, save_settings


class Integrations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'INTEGRATIONS_FILE': str(Path(self.tmp.name)/'private.json'),
                                           'TELEGRAM_BOT_TOKEN':'', 'TELEGRAM_OWNER_ID':''})
        self.env.start()
        self.worker = SimpleNamespace(tg=server.DummyTelegram(), state={'offset':123}, store=SimpleNamespace(save=lambda:None))
    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
    def test_validate_then_enable_disable_without_exposing_token(self):
        token='123456789:'+'x'*30
        body=server.TelegramSettingsRequest(telegramToken=token, telegramOwnerId='123',telegramEnabled=True)
        with patch.object(server.HTTP,'call',return_value={'ok':True,'result':{'is_bot':True,'username':'test_bot'}}):
            response=server.update_integrations(body,self.worker)
        self.assertTrue(response['telegramEnabled'])
        self.assertNotIn(token,str(response))
        self.assertEqual(self.worker.tg.owner,123)
        self.assertEqual(self.worker.state['offset'],0)
        self.assertEqual(load_settings()['telegramToken'],token)
        self.assertEqual(Path(os.environ['INTEGRATIONS_FILE']).stat().st_mode&0o777,0o600)
        server.update_integrations(server.TelegramSettingsRequest(telegramOwnerId='123',telegramEnabled=False),self.worker)
        self.assertIsInstance(self.worker.tg,server.DummyTelegram)
        self.assertFalse(public_settings()['telegramEnabled'])
        self.assertEqual(load_settings()['telegramToken'],token)
    def test_bad_token_preserves_previous_settings_and_poller(self):
        save_settings({'telegramEnabled':False})
        old=self.worker.tg
        with self.assertRaises(HTTPException):
            server.update_integrations(server.TelegramSettingsRequest(telegramToken='invalid',telegramOwnerId='123',telegramEnabled=True),self.worker)
        self.assertIs(self.worker.tg,old)
        self.assertEqual(load_settings(),{'telegramEnabled':False})
    def test_jellyfin_matches_published_file_not_similar_title(self):
        episode={'index':4,'published':True}
        season={'kind':'series','episodes':[episode]}
        calls=[]
        def call(route,query=None):
            calls.append(route)
            return {'Id':'server1'} if route.startswith('/System') else {'TotalRecordCount':2,'Items':[{'Id':'wrong','Path':'/other/same-name.mkv'},{'Id':'episode1','SeriesId':'series1','Path':'/data/library/series/e.mkv'}]}
        worker=SimpleNamespace(season_by_id=lambda _:season,dest=lambda s,e:Path('/data/library/series/e.mkv'),j=SimpleNamespace(http=SimpleNamespace(call=call)))
        self.assertEqual(server.jellyfin_link('abc',4,worker)['itemId'],'episode1')
        self.assertEqual(server.jellyfin_link('abc',None,worker)['itemId'],'series1')
        episode['published']=False
        self.assertFalse(server.jellyfin_link('abc',4,worker)['available'])
        self.assertEqual(len(calls),4)

if __name__=='__main__':unittest.main()
