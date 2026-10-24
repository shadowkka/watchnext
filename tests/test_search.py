import copy
import hashlib
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

import test_queue as fixtures
from app import Store, Worker
from core import GB, magnet_hash
from net import APIError, Qbit
from search_client import Jackett, plain, quality, voices, release_numbers, resolution
from torrent_meta import torrent_hash, MAX_TORRENT


def bencode(value):
    if isinstance(value, int): return b'i' + str(value).encode() + b'e'
    if isinstance(value, bytes): return str(len(value)).encode() + b':' + value
    if isinstance(value, list): return b'l'+b''.join(bencode(v) for v in value)+b'e'
    return b'd'+b''.join(bencode(k)+bencode(v) for k,v in sorted(value.items()))+b'e'


def torrent():
    info = {b'length':10, b'name':b'Movie.mkv', b'piece length':16384, b'pieces':b'a'*20}
    return bencode({b'announce':b'http://127.0.0.1/announce', b'info':info}), hashlib.sha1(bencode(info)).hexdigest()


def row(n=0):
    return {'title':f'Movie {n} / Movie EN (1994) WEB-DL 1080p', 'size':GB, 'seeders':10,
            'peers':12, 'tracker':'RuTor', 'quality':'1080p · WEB-DL',
            'magnet':'magnet:?xt=urn:btih:'+format(n+1,'040x'), 'link':''}


class Client(unittest.TestCase):
    def setUp(self):
        self.indexer_patch=patch.object(Jackett,'indexers',return_value=['rutor'])
        self.indexer_patch.start()
        self.addCleanup(self.indexer_patch.stop)

    def test_normalization_sort_dedup_unknown_peers_and_categories(self):
        j = Jackett()
        source = {'Indexers':[{'Name':'RuTor','Status':2}], 'Results':[
            {'Title':'<b>Film</b> 1080p','MagnetUri':fixtures.MAGNET,'Seeders':None,'Peers':None,'Category':[8000]},
            {'Title':'Duplicate','MagnetUri':fixtures.MAGNET,'Seeders':12,'Category':[8000]},
            {'Title':'Other','MagnetUri':'magnet:?xt=urn:btih:'+'b'*40,'Seeders':15,'Peers':18,'Category':[2000]},
            {'Title':'Music','MagnetUri':'magnet:?xt=urn:btih:'+'c'*40,'Category':[3000]},
            {'Title':'Adult','MagnetUri':'magnet:?xt=urn:btih:'+'d'*40,'Category':[6000]}]}
        with patch.object(j,'request',return_value=json.dumps(source).encode()):
            result=j.search('Film','movie')
        self.assertEqual(len(result['results']),2)
        self.assertEqual(result['results'][0]['seeders'],15)
        self.assertEqual(result['results'][1]['title'],'Film 1080p')
        self.assertIsNone(result['results'][1]['peers'])

    def test_indexer_error_partial_success_and_no_indexers(self):
        j=Jackett()
        with patch.object(j,'request',return_value=b'{"Results":[],"Indexers":[]}'):
            with self.assertRaises(APIError):j.search('Film','movie')
        payload={'Results':[],'Indexers':[{'Name':'RuTracker','Status':1,'Error':'secret-value'}]}
        with patch.object(j,'request',return_value=json.dumps(payload).encode()):
            with self.assertRaises(APIError) as exc:j.search('Film','movie')
            self.assertNotIn('secret-value',str(exc.exception))
        payload['Indexers'].append({'Name':'RuTor','Status':2})
        with patch.object(j,'request',return_value=json.dumps(payload).encode()):
            self.assertEqual(j.search('Film','movie')['warnings'],['RuTracker'])

    def test_independent_tracker_failure_keeps_other_results(self):
        j=Jackett()
        def request(path, query=None):
            if '/rutracker/' in path:raise APIError('timeout')
            return json.dumps({'Results':[{'Title':'Film','Category':[8000],'MagnetUri':fixtures.MAGNET}],
                               'Indexers':[{'Name':'RuTor','Status':2}]}).encode()
        with patch.object(j,'indexers',return_value=['rutor','rutracker']),patch.object(j,'request',side_effect=request):
            result=j.search('Film','movie')
        self.assertEqual(len(result['results']),1)
        self.assertEqual(result['warnings'],['rutracker'])

    def test_quality_and_plain_text(self):
        self.assertIn('2160p',quality('Film 2160p WEB-DL HEVC'))
        self.assertIn('WEB-DL',quality('Film 2160p WEB-DL HEVC'))
        self.assertEqual(plain('<b>A</b> &amp; B\n\x00 C'),'A & B C')

    def test_resolve_magnet_without_network_and_torrent_through_proxy(self):
        j=Jackett();j.base='http://jackett:9117'
        with patch.object(j,'request') as request:
            self.assertEqual(j.resolve(row())[0],row()['magnet']);request.assert_not_called()
        data,h=torrent()
        with patch.object(j,'request',return_value=data):
            magnet,raw=j.resolve({'link':'http://jackett:9117/dl/rutracker/?path=opaque'})
            self.assertEqual(magnet_hash(magnet),h);self.assertEqual(raw,data)

    def test_resolve_rejects_arbitrary_external_or_local_targets(self):
        j=Jackett();j.base='http://jackett:9117'
        for link in ('http://127.0.0.1:8096/Users','file:///etc/passwd','http://example.com/file.torrent',
                     'http://jackett:9117/UI/Dashboard','http://jackett:9117/dl/../api/config'):
            with self.subTest(link=link),self.assertRaises(ValueError):j.resolve({'link':link})

    def test_malformed_payloads_are_sanitized(self):
        j=Jackett()
        for data in (b'<html>login secret</html>',b'{"Results":null}',b'[]'):
            with patch.object(j,'request',return_value=data),self.assertRaises(APIError):j.search('Film','movie')


class ReleaseHints(unittest.TestCase):
    def test_studios_and_original_from_real_release_titles(self):
        self.assertEqual(voices('Show [S01-12] BDRip | Кураж-Бомбей'), 'Кураж-Бамбей')
        self.assertEqual(voices('Show VO (- Paramount Comedy) + Original + Sub (Rus, Eng)'), 'Paramount Comedy, Оригинал')
        self.assertEqual(voices('Show MVO (LostFilm, NewStudio) + VO (Custom Studio)'), 'LostFilm, NewStudio, Custom Studio')
        self.assertEqual(voices('Show VO (kurazh) + Original'), 'Кураж-Бамбей, Оригинал')
        self.assertEqual(voices('Movie MVO + Original'), 'MVO — студия не указана, Оригинал')
        self.assertIn('студия не указана', voices('Movie MVO'))
        self.assertEqual(voices('Movie 1080p'), 'не указаны в названии')

    def test_season_ranges_russian_and_sxe(self):
        self.assertEqual(release_numbers('Show [S01-12 + Special] (2007-2019) 1080p')['seasons'], list(range(1,13)))
        self.assertEqual(release_numbers('Show Сезоны: 2-4 [2020]')['seasons'], [2,3,4])
        self.assertEqual(release_numbers('Show [2-4 сезоны]')['seasons'], [2,3,4])
        self.assertEqual(release_numbers('Show / S12E1-24 of 24 [2018]'), {'seasons':[12], 'episodes':[1,24]})
        self.assertEqual(release_numbers('Show Сезон 3 Серии 05-22 из 22'), {'seasons':[3], 'episodes':[5,22]})
        self.assertEqual(release_numbers('Show 2020 1080p'), {'seasons':[], 'episodes':[]})

    def test_multiple_seasons_do_not_reuse_total_episode_count(self):
        self.assertEqual(release_numbers('Show S01-12 / E1-280 of 280')['episodes'], [])
        self.assertEqual(release_numbers('Show S12E23-E24')['episodes'], [23,24])
        self.assertEqual(release_numbers('Show S1-999')['seasons'], [])


class TorrentMetadata(unittest.TestCase):
    def test_hash_original_info_and_duplicate_key_rejected(self):
        data,h=torrent();self.assertEqual(torrent_hash(data),h)
        with self.assertRaises(ValueError):torrent_hash(b'd4:info'+bencode({b'pieces':b'a'*20})+b'4:infoi1ee')

    def test_bad_nested_oversized_html_and_v2_rejected(self):
        for data in (b'<html>error</html>',b'd4:infodee',b'l'*60+b'e'*60,b'0:'*(MAX_TORRENT//2+1),b'd3:foo1:xe'):
            with self.subTest(prefix=data[:10]),self.assertRaises(ValueError):torrent_hash(data)


class SearchMenus(fixtures.Lifecycle):
    def setUp(self):
        super().setUp()
        self.futures=[]
        def submit(uid,sid,task,fn,*args):
            future=Future();self.w.search_jobs()[(uid,sid,task)]=future
            self.futures.append(future)
        self.submit_patch=patch.object(self.w,'search_submit',side_effect=submit)
        self.submit_patch.start()
    def tearDown(self):
        self.submit_patch.stop();super().tearDown()
    def buttons(self):return [b for rows in self.tg.markups[-1]['inline_keyboard'] for b in rows]
    def click(self,label=None,data=None,uid=123):
        if data is None:
            matches=[b for b in self.buttons() if b['text']==label] or [b for b in self.buttons() if label in b['text']]
            self.assertEqual(len(matches),1,(label,self.buttons()))
            data=matches[0]['callback_data']
        self.w.callback({'id':'q','from':{'id':uid},'data':data,'message':{'message_id':42,'chat':{'id':uid,'type':'private'}}})
        return data
    def search(self,kind='movie',query='Movie',uid=123,count=7):
        self.command('/search '+query,sender=uid);self.click('Фильм' if kind=='movie' else 'Сериал',uid=uid)
        self.futures[-1].set_result({'results':[row(n) for n in range(count)],'total':count,'warnings':[]})
        self.w.search_poll()
        return self.store.data['searches'][str(uid)]

    def test_query_prompt_and_background_results_paginated(self):
        self.command('/search');self.click('Фильм');self.command('Movie')
        self.assertEqual(self.store.data['searches']['123']['phase'],'working')
        self.assertEqual(len(self.store.data['seasons']),1)
        self.futures[-1].set_result({'results':[row(n) for n in range(8)],'total':8,'warnings':[]})
        self.w.search_poll();self.assertIn('Сиды: 10',self.tg.messages[-1]);self.assertIn('Пиры: 12',self.tg.messages[-1])
        self.click('Дальше');self.assertIn('6. Movie 5',self.tg.messages[-1]);self.assertLess(len(self.tg.messages[-1]),4000)

    def test_selected_movie_preview_does_not_add_until_confirmed(self):
        s=self.search();self.click('1 ·')
        self.assertEqual(len(self.store.data['seasons']),1)
        self.assertNotIn('Выбрать сезон',str(self.tg.markups[-1]))
        self.assertEqual(s['title'],'Movie 0 (1994)')
        self.click('Добавить в очередь');self.assertEqual(s['phase'],'importing')
        self.futures[-1].set_result((row()['magnet'],None));self.w.search_poll()
        self.assertEqual(self.store.data['seasons'][1]['kind'],'movie');self.assertEqual(s['phase'],'added')

    def test_series_custom_start_and_title(self):
        s=self.search(kind='series');self.click('1 ·');self.click('Выбрать сезон');self.click('Сезон 2');self.click('5')
        self.click('Изменить название');self.command('Canonical Show')
        self.assertEqual((s['title'],s['season'],s['start_episode'],s['auto_season']),('Canonical Show',2,5,False))
        self.click('Добавить в очередь');self.futures[-1].set_result((row()['magnet'],None));self.w.search_poll()
        p=self.store.data['seasons'][1];self.assertEqual((p['title'],p['season'],p['start_episode']),('Canonical Show',2,5))

    def test_known_season_and_episode_buttons_apply_start(self):
        s=self.search(kind='series');s['results'][0]['title']='Show / S12E1-24 of 24 | Кураж-Бамбей'
        self.click('1 ·');self.assertIn('Озвучка:</b> Кураж-Бамбей',self.tg.messages[-1])
        self.click('Выбрать сезон');self.assertIn('распознаны',self.tg.messages[-1])
        self.assertNotIn('Сезон 11',str(self.tg.markups[-1]))
        self.click('Сезон 12');self.assertIn('серии 1–24',self.tg.messages[-1])
        self.assertNotIn('25', [b['text'] for b in self.buttons()])
        self.click('18');self.assertEqual((s['season'],s['start_episode'],s['auto_season']),(12,18,False))
        self.click('Выбрать сезон');self.click('Авто');self.assertTrue(s['auto_season'])

    def test_multiseason_buttons_and_unknown_episode_pagination(self):
        s=self.search(kind='series');s['results'][0]['title']='Show [S01-12]'
        self.click('1 ·');self.click('Выбрать сезон');self.click('Сезон 9')
        self.assertIn('не указан',self.tg.messages[-1]);self.click('Серии →');self.click('45')
        self.assertEqual((s['season'],s['start_episode']),(9,45))

    def test_unknown_season_pagination_and_back_preserve_title(self):
        s=self.search(kind='series');self.click('1 ·');self.click('Выбрать сезон')
        self.assertIn('не указаны',self.tg.messages[-1]);self.click('Сезоны →');self.click('Сезон 21')
        self.click('← К раздаче');self.assertEqual(s['title'],'Movie 0')
        self.assertEqual(s['phase'],'details')

    def test_movie_has_no_start_picker_and_old_input_buttons_use_picker(self):
        s=self.search();self.click('1 ·')
        with self.assertRaises(ValueError):self.w.search_seasons(123,s['id'])
        s=self.search(kind='series');self.click('1 ·');self.w.search_input(123,s['id'],'start')
        self.assertNotIn('123',self.store.data.get('ui_input',{}))
        self.assertIn('Сезон 1',str(self.tg.markups[-1]))

    def test_long_studio_titles_fit_telegram_limit(self):
        s=self.search(count=5)
        for r in s['results']:r['title']='Show MVO ('+'Long Studio '*30+') | '+('Кураж-Бамбей '*20)
        self.w.search_page(123,s['id']);self.assertLessEqual(len(self.tg.messages[-1]),4096)
        self.command('/menu');self.assertIn('🧲 Magnet',str(self.tg.markups[-1]))

    def test_priority_quality_ranks_before_seeders_and_persists(self):
        s=self.search(count=3)
        s['results'][0].update(title='High seeds 720p',seeders=100)
        s['results'][1].update(title='Preferred 1080p',seeders=2)
        s['results'][2].update(title='Other 2160p',seeders=20)
        self.w.search_options(123,s['id']);self.click('Выше:');self.click('1080p')
        self.click('← Результаты');self.assertLess(self.tg.messages[-1].index('Preferred 1080p'),self.tg.messages[-1].index('High seeds'))
        self.click('1 ·');self.assertEqual(s['chosen'],1)
        self.store.save();fresh=Worker(Store(self.store.path),self.q,self.j,self.tg,self.root/'data')
        self.assertEqual(fresh.search_prefs(123)['prefer'],'1080p')
        self.assertEqual(resolution('Film 4K WEB-DL'),'2160p')
        self.assertEqual(resolution('Film 1080i'),'1080i')

    def test_quality_voice_seeds_size_filters_and_clear_keep_preference(self):
        s=self.search(count=4)
        s['results'][0].update(title='Show 1080p LostFilm',seeders=20,size=2*GB)
        s['results'][1].update(title='Show 1080p NewStudio',seeders=30,size=3*GB)
        s['results'][2].update(title='Show 720p LostFilm',seeders=50,size=GB)
        s['results'][3].update(title='Show 1080p LostFilm',seeders=None,size=None)
        p=self.w.search_prefs(123);p.update(prefer='1080p',quality='1080p',seeds=5,size=5);s['voice']='LostFilm'
        self.assertEqual([i for i,_ in self.w.search_visible(123,s)],[0])
        self.w.search_page(123,s['id']);self.click('Фильтры');self.click('Озвучка в этом поиске:');self.click('NewStudio')
        self.assertEqual(s['voice'],'NewStudio')
        self.click('Сбросить фильтры');self.assertEqual(p,{'prefer':'1080p'})
        self.assertEqual(len(self.w.search_visible(123,s)),4)

    def test_empty_filters_can_be_reset_without_network(self):
        s=self.search(count=2);self.w.search_prefs(123)['quality']='2160p'
        before=len(self.futures);self.w.search_page(123,s['id'])
        self.assertIn('По текущим фильтрам',self.tg.messages[-1])
        self.click('Фильтры');self.click('Сбросить фильтры');self.click('← Результаты')
        self.assertIn('Movie 0',self.tg.messages[-1]);self.assertEqual(len(self.futures),before)

    def test_search_preferences_are_per_user_available_before_search(self):
        self.command('/menu');self.click('Настройки');self.click('Поиск:')
        self.click('Выше:');self.click('1080p');self.assertEqual(self.w.search_prefs(123)['prefer'],'1080p')
        self.assertEqual(self.w.search_prefs(456),{})
        self.assertFalse(any('Озвучка' in b['text'] for b in self.buttons()))

    def test_voice_filter_paginates_custom_studios(self):
        s=self.search(count=15)
        for i,r in enumerate(s['results']):r['title']=f'Show VO (Studio {i:02d})'
        self.w.search_filter_menu(123,s['id'],'voice');self.click('Дальше →');self.click('Studio 14')
        self.assertEqual(s['voice'],'Studio 14')
        self.click('← Результаты');self.click('1 ·');self.assertEqual(s['chosen'],14)

    def test_mvo_rus_independent_tags_and_local_filter(self):
        from search_client import voice_tags
        title='Пример фильма / Example Film Dub (Rus, Ukr) + MVO + 2x AVO + Original (Eng) + Sub (Rus, Eng)'
        self.assertTrue({'MVO','DUB','AVO','Rus','Ukr'} <= voice_tags(title))
        self.assertNotIn('Rus',voice_tags('Movie Original Eng + Sub (Rus, Eng)'))
        s=self.search(count=2);s['results'][0]['title']=title
        self.w.search_options(123,s['id']);self.click('Озвучка в этом поиске:');self.click('MVO')
        self.assertEqual([i for i,_ in self.w.search_visible(123,s)],[0])
        self.assertNotIn('voice',self.w.search_prefs(123))
        fresh=self.search(query='Next Movie');self.assertNotIn('voice',fresh)
        self.w.search_prefs(123)['voice']='Rus'
        self.assertEqual(len(self.w.search_visible(123,fresh)),7)

    def test_voice_options_respect_other_filters(self):
        s=self.search(count=2)
        s['results'][0]['title']='Movie 2160p VO (Other Studio)'
        s['results'][1]['title']='Movie 1080p MVO (Rus)'
        self.w.search_prefs(123)['quality']='1080p'
        options=self.w.search_filter_choices(123,s['id'],'voice')
        self.assertNotIn('Other Studio',[v for _,v in options])
        self.assertIn(('MVO · 1','MVO'),options)

    def test_html_escapes_external_titles_and_queue_names(self):
        from net import RichText
        s=self.search(count=1);s['query']='A <tag> & B';s['results'][0]['title']='A <tag> & B / Film 1080p'
        self.w.search_page(123,s['id'])
        self.assertIsInstance(self.tg.messages[-1],RichText)
        self.assertIn('&lt;tag&gt; &amp;',self.tg.messages[-1])
        self.assertNotIn('A <tag>',self.tg.messages[-1])
        self.s['title']='A <tag> & B'
        self.command('/status')
        self.assertIsInstance(self.tg.messages[-1],RichText)
        self.assertIn('A &lt;tag&gt; &amp; B',self.tg.messages[-1])

    def test_default_series_start_uses_auto_season(self):
        s=self.search(kind='series');self.click('1 ·');self.assertTrue(s['auto_season'])

    def test_family_results_and_input_independent(self):
        self.store.data['members']={'456':{'name':'Family'}}
        a=self.search(query='His Movie');token=self.buttons()[0]['callback_data']
        b=self.search(query='Her Movie',uid=456)
        self.click(data=token,uid=456);self.assertTrue(self.tg.answers[-1][2])
        self.assertEqual((a['query'],b['query']),('His Movie','Her Movie'))
        self.click('1 ·',uid=456);self.assertEqual(a['phase'],'ready');self.assertEqual(b['phase'],'details')

    def test_new_search_invalidates_old_result_buttons(self):
        self.search();token=self.buttons()[0]['callback_data'];self.search(query='New')
        self.click(data=token);self.assertTrue(self.tg.answers[-1][2]);self.assertIn('устарела',self.tg.answers[-1][1]);self.assertEqual(len(self.store.data['seasons']),1)

    def test_cancel_late_result_and_late_import_have_no_side_effects(self):
        self.command('/search Movie');self.click('Фильм');future=self.futures[-1]
        self.command('/cancel');future.set_result({'results':[row()],'total':1,'warnings':[]});self.w.search_poll()
        self.assertNotIn('123',self.store.data['searches'])
        self.search();self.click('1 ·');self.click('Добавить в очередь');future=self.futures[-1]
        self.command('/cancel');future.set_result((row()['magnet'],None));self.w.search_poll()
        self.assertEqual(len(self.store.data['seasons']),1)

    def test_revocation_while_import_pending_does_not_add(self):
        self.store.data['members']={'456':{'name':'Family'}}
        self.search(uid=456);self.click('1 ·',uid=456);self.click('Добавить в очередь',uid=456)
        self.command('/revoke 456');self.futures[-1].set_result((row()['magnet'],None));self.w.search_poll()
        self.assertEqual(len(self.store.data['seasons']),1)

    def test_restart_keeps_results_and_reports_interrupted_requests(self):
        s=self.search();self.store.save();fresh=Worker(Store(self.store.path),self.q,self.j,self.tg,self.root/'data')
        fresh.search_page(123,s['id']);self.assertIn('Movie',self.tg.messages[-1])
        fresh.state['searches']['123']['phase']='working';fresh.search_poll()
        self.assertEqual(fresh.state['searches']['123']['phase'],'error')

    def test_errors_do_not_pause_media_queue(self):
        self.command('/search Movie');self.click('Фильм');self.futures[-1].set_exception(APIError('Indexer offline'))
        self.w.search_poll();self.assertFalse(self.store.data['paused']);self.assertIn('Indexer offline',self.tg.messages[-1])
        self.w.tick();self.assertTrue(any(e['selected'] for e in self.s['episodes']))

    def test_add_torrent_preserves_original_metadata_for_restart(self):
        data,h=torrent();s=self.search();s['results'][0]['magnet']='magnet:?xt=urn:btih:'+h
        self.click('1 ·');self.click('Добавить в очередь');self.futures[-1].set_result(('magnet:?xt=urn:btih:'+h,data));self.w.search_poll()
        p=self.store.data['seasons'][1];self.assertEqual(Path(p['torrent_file']).read_bytes(),data)
        self.assertEqual(p['hash'],h)
        self.assertEqual(Store(self.store.path).data['seasons'][1]['torrent_file'],p['torrent_file'])

    def test_confirm_button_cannot_start_two_imports(self):
        self.search();self.click('1 ·');token=next(b['callback_data'] for b in self.buttons() if 'Добавить в очередь' in b['text'])
        self.click(data=token);n=len(self.futures);self.click(data=token)
        self.assertEqual(len(self.futures),n)

    def test_rejected_torrent_does_not_stop_other_downloads_or_loop(self):
        self.w.add('Bad Movie',1,'magnet:?xt=urn:btih:'+'b'*40,kind='movie')
        bad=self.store.data['seasons'][1]
        with patch.object(self.q,'add',side_effect=ValueError('Bad torrent')) as add:
            self.w.tick();self.w.tick()
        self.assertEqual(add.call_count,1)
        self.assertEqual(bad['error'],'Bad torrent')
        self.assertTrue(any(e['selected'] for e in self.s['episodes']))
        self.assertFalse(self.store.data['paused'])

    def test_unfinished_request_limits_are_bounded(self):
        self.submit_patch.stop()
        f=Future();self.w.search_jobs()[(123,'old','query')]=f
        with self.assertRaises(ValueError):self.w.search_submit(123,'new','query',lambda:None)
        self.assertFalse(hasattr(self.w,'_search_pool'))


for name in dir(fixtures.Lifecycle):
    if name.startswith('test_') and name not in SearchMenus.__dict__:setattr(SearchMenus,name,None)


class Upload(unittest.TestCase):
    @patch.dict(os.environ, {'QBIT_PASSWORD':'test-only'})
    def test_qbit_file_upload_starts_stopped_and_checks_hash(self):
        data,h=torrent()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'release.torrent';path.write_bytes(data)
            q=Qbit()
            class Response:
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def read(self):return b'Ok.'
            with patch.object(q.http.opener,'open',return_value=Response()) as upload:
                q.add({'hash':h,'download_root':'/data/downloads/'+h,'torrent_file':str(path)})
                body=upload.call_args.args[0].data
                self.assertIn(b'name="stopped"\r\n\r\ntrue\r\n',body)
                self.assertIn(b'name="autoTMM"\r\n\r\nfalse\r\n',body)
                self.assertIn(data,body)
                self.assertIn(b'name="torrents"',body)
            for report in ({'added_torrent_ids':[h],'failure_count':0,'pending_count':0,'success_count':1},
                           {'added_torrent_ids':[],'failure_count':0,'pending_count':1,'success_count':0}):
                with patch.object(Response, 'read', return_value=json.dumps(report).encode()), patch.object(q.http.opener,'open',return_value=Response()):
                    q.add({'hash':h,'download_root':'/data/downloads/'+h,'torrent_file':str(path)})
            for bad in (b'Fails.', b'{"failure_count":1,"success_count":0}', b'{}'):
                with patch.object(Response,'read',return_value=bad), patch.object(q.http.opener,'open',return_value=Response()), self.assertRaises(ValueError):
                    q.add({'hash':h,'download_root':'/data/downloads/'+h,'torrent_file':str(path)})
            with self.assertRaises(APIError):q.add({'hash':'b'*40,'download_root':'/data/downloads/b','torrent_file':str(path)})

if __name__=='__main__':unittest.main()
