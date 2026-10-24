import io
import json
import os
import time
import unittest
from concurrent.futures import Future
from unittest.mock import patch, MagicMock
import test_queue as fixtures
from test_search import row
from catalog_client import CatalogClient, parse_history
from net import Telegram, APIError


class ManualPool:
    def __init__(self):self.calls=[]
    def submit(self,fn,*args):
        future=Future();self.calls.append((future,fn,args));return future
    def finish(self,data=None,run=False):
        future,fn,args=next(c for c in self.calls if not c[0].done())
        try:future.set_result(fn(*args) if run else data)
        except Exception as exc:future.set_exception(exc)


class BotCatalog(unittest.TestCase):
    command=fixtures.Lifecycle.command
    def setUp(self):
        fixtures.Lifecycle.setUp(self)
        self.pool=ManualPool();self.w._catalog_pool=self.pool
        self.search_pool=ManualPool();self.w._search_pool=self.search_pool
    def tearDown(self):fixtures.Lifecycle.tearDown(self)
    def buttons(self):return [b for r in self.tg.markups[-1]['inline_keyboard'] for b in r]
    def click(self,label,uid=123):
        matches=[b for b in self.buttons() if label==b['text']] or [b for b in self.buttons() if label in b['text']]
        self.assertEqual(len(matches),1,(label,self.buttons()))
        self.w.callback({'id':'q','from':{'id':uid},'data':matches[0]['callback_data'], 'message':{'message_id':42,'chat':{'id':uid,'type':'private'}}})
    def session(self,uid=123):return self.w.state['catalogs'][str(uid)]
    def complete(self,data):self.pool.finish(data);self.w.catalog_poll()

    def test_home_commands_partial_person_and_shared_filmography_to_release_search(self):
        self.command('/menu');self.assertTrue(any('Что посмотреть' in b['text'] for b in self.buttons()))
        self.command('/person Том Х')
        self.assertIn('query=%D0%A2%D0%BE%D0%BC+%D0%A5',self.pool.calls[-1][2][0])
        self.complete({'people':[{'id':'Q2263','name':'Том Хэнкс','birthYear':'1956'}]})
        self.click('Том Хэнкс')
        self.assertEqual(self.pool.calls[-1][2][0],'/api/explore/person/Q2263')
        self.complete({'person':{'name':'Том Хэнкс'},'source':'Wikidata','movies':[{'imdb_id':'tt0109830','title':'Форрест Гамп','year':1994,'kind':'movie'}]})
        self.click('Форрест Гамп')
        self.complete({'imdb_id':'tt0109830','title':'Форрест Гамп','year':1994,'kind':'movie','description':'Русское описание'})
        self.assertIn('Русское описание',str(self.tg.messages[-1]))
        self.click('Найти раздачи')
        s=self.w.state['searches']['123']
        self.assertEqual(s['query'],'Форрест Гамп 1994');self.assertEqual(s['kind'],'movie')
        self.assertTrue(s['running']);self.assertEqual(len(self.search_pool.calls),1)
        self.assertEqual(len(self.w.state['seasons']),1)

    def test_random_filters_skip_seen_and_several_options(self):
        self.command('/random');s=self.session();sid=s['id']
        self.w.catalog_prompt(123,sid,'years');self.command('2000 2020')
        self.w.catalog_prompt(123,sid,'rating');self.command('8,5')
        for field,value in [('genre','Drama'),('country','France'),('excludeGenre','Horror'),('excludeCountry','United States'),('count',5)]:
            self.w.catalog_action(123,'catalog_set',sid,field,value)
        self.click('Подобрать фильмы')
        payload=self.pool.calls[-1][2][1]
        self.assertEqual((payload['yearFrom'],payload['yearTo'],payload['rating'],payload['count']),(2000,2020,8.5,5))
        self.assertEqual(payload['genre'],'Drama');self.assertEqual(payload['excludeCountry'],'United States')
        movies=[{'imdb_id':'tt000000'+str(i),'title':'Фильм '+str(i),'year':2010} for i in range(3)]
        self.complete({'movies':movies,'source':'TMDB'})
        self.assertIn('Найдено: 3',self.tg.messages[-1])
        self.click('Ещё варианты');self.assertEqual(self.pool.calls[-1][2][1]['skip'],[m['imdb_id'] for m in movies])

    def test_movie_photo_html_escape_and_seen_use_shared_history(self):
        self.command('/discover');s=self.session();sid=s['id']
        s.update(movies=[{'imdb_id':'tt0109830','title':'A < B','year':1994}],chosen=0,phase='ready')
        photos=[];self.tg.photo=lambda *args:photos.append(args)
        self.w.catalog_card(123,sid,0)
        self.complete({'imdb_id':'tt0109830','title':'A < B','year':1994,'description':'<script>bad</script>','_photo':b'jpeg','tmdbRating':8.3})
        self.assertEqual(len(photos),1);self.assertIn('&lt;script&gt;',str(photos[0][1]));self.assertNotIn('<script>',str(photos[0][1]))
        self.assertNotIn('_photo',json.dumps(self.w.state))
        self.w.catalog_action(123,'catalog_seen',sid,0)
        self.assertEqual(self.pool.calls[-1][2][0],'/api/discover/seen')
        self.complete({'watchedCount':1});self.assertTrue(s['movies'][0]['watched'])
        self.assertEqual(self.w.catalog_visible(s),[])

    def test_stale_session_and_revoked_member_cannot_apply_background_result(self):
        self.w.state.setdefault('members',{})['456']={'name':'Family'}
        self.command('/person Test',sender=456)
        s=self.session(456);s['movies']=[{'title':'Old'}]
        self.w.state['members'].pop('456')
        before=len(self.tg.messages)
        self.complete({'people':[{'id':'Q1','name':'Wrong'}]})
        self.assertEqual(len(self.tg.messages),before)
        self.command('/person New');old=self.session()['id']
        self.command('/discover');new=self.session()['id'];self.assertNotEqual(new,old)
        before=len(self.tg.messages);self.complete({'people':[{'id':'Q1','name':'Wrong'}]})
        self.assertEqual(len(self.tg.messages),before)
        with self.assertRaises(ValueError):self.w.catalog_session(123,old)

    def test_owner_file_import_requires_confirmation_and_member_is_denied(self):
        self.command('/watched');self.complete({'watchedCount':2})
        self.click('Загрузить CSV/JSON')
        self.tg.download_document=lambda d:b'imdb_id,title,year\ntt0109830,Forrest Gump,1994\n'
        self.w.command({'from':{'id':123},'chat':{'id':123,'type':'private'},'document':{'file_id':'fixture','file_name':'watched.csv','file_size':80}})
        self.pool.finish(run=True);self.w.catalog_poll()
        self.assertIn('считаться просмотренными',self.tg.messages[-1]);self.assertEqual(len(self.pool.calls),2)
        self.assertEqual(self.session()['import_rows'][0]['imdb_id'],'tt0109830')
        self.click('Импортировать');self.assertEqual(self.pool.calls[-1][2][0],'/api/discover/watched')
        self.complete({'accepted':1,'added':1,'watchedCount':3})
        self.assertNotIn('import_rows',self.session())
        self.w.state.setdefault('members',{})['456']={'name':'Family'}
        with self.assertRaises(ValueError):self.w.catalog_document(456,{'file_id':'fixture','file_name':'watched.csv'})

    def test_collection_filters_role_type_year_order_and_watched(self):
        self.command('/discover');s=self.session()
        s['movies']=[{'title':'Old','year':1994,'kind':'movie','roles':['actor']}, {'title':'New','year':2020,'kind':'series','roles':['director']}, {'title':'Seen','year':2010,'kind':'movie','roles':['actor'],'watched':True}]
        s.update(role='actor',kind='movie',list_years=[1990,2000],order='new')
        self.assertEqual([m['title'] for _,m in self.w.catalog_visible(s)],['Old'])
        s.update(role='',kind='',list_years=[1990,2026]);self.assertEqual([m['title'] for _,m in self.w.catalog_visible(s)],['New','Old'])
        self.w.catalog_action(123,'catalog_toggle_seen',s['id'])
        self.assertEqual([m['title'] for _,m in self.w.catalog_visible(s)],['New','Seen','Old'])

    def test_jellyfin_url_uses_public_address_and_exact_episode_without_token(self):
        with patch.dict(os.environ,{'BIND_IP':'192.0.2.10','JELLYFIN_PORT':'8096'}):
            self.w.catalog_jellyfin(123,fixtures.H,4)
            with patch.object(CatalogClient,'call',side_effect=[{'available':True,'itemId':'episode4','serverId':'server1'},{'jellyfinUrl':''}]):self.pool.finish(run=True)
            self.w.catalog_poll()
        button=next(b for b in self.buttons() if 'url' in b)
        self.assertEqual(button['url'],'http://192.0.2.10:8096/web/index.html#!/details?id=episode4&serverId=server1')
        self.assertNotIn('token',button['url'])

    def test_settings_public_link_and_unknown_user_denied(self):
        with patch.dict(os.environ,{'BIND_IP':'192.0.2.10','WEB_PORT':'8787'}):
            self.w.ui_settings(123)
            self.assertTrue(any(b.get('url')=='http://192.0.2.10:8787/#settings' for b in self.buttons()))
        before=len(self.tg.messages);self.command('/random',sender=999)
        self.assertEqual(len(self.tg.messages),before)

    def test_partial_release_indices_stay_stable_while_selecting_and_importing(self):
        self.w.search_start(123,'Show');s=self.w.state['searches']['123'];sid=s['id'];s['kind']='series'
        self.w.search_run(123,sid)
        first=dict(row(0),title='Show S01-12',size=20*10**9,seeders=5)
        late=dict(row(1),title='Show S06',size=1*10**9,seeders=100)
        self.w._search_events.put((123,sid,s['generation'],{'results':[first],'warnings':[]}));self.w.search_poll()
        self.assertEqual(s['phase'],'ready');self.assertTrue(s['running'])
        self.w.search_details(123,sid,0);s['displayed']=False;before=len(self.tg.messages)
        self.w._search_events.put((123,sid,s['generation'],{'results':[late,first],'warnings':[]}));self.w.search_poll()
        self.assertEqual(s['results'][s['chosen']]['magnet'],first['magnet']);self.assertEqual(len(self.tg.messages),before)
        self.w.search_action(123,'search_add',sid,0)
        self.assertEqual(s['phase'],'importing');self.assertEqual(len(self.search_pool.calls),2)
        self.search_pool.calls[0][0].set_result({'results':[late,first],'warnings':[]});self.w.search_poll()
        self.assertEqual(s['phase'],'importing');self.assertFalse(s['running'])

    def test_release_season_ranges_and_size_sort_keep_unknown_sizes_last(self):
        self.w.search_start(123,'Show');s=self.w.state['searches']['123'];s.update(kind='series',phase='ready',season_filter=6)
        s['results']=[dict(row(0),title='Show S01-12',size=None),dict(row(1),title='Show S06',size=30),dict(row(2),title='Show S06',size=10),dict(row(3),title='Show S07',size=1)]
        self.w.search_prefs(123)['sort']='size-asc';self.assertEqual([i for i,_ in self.w.search_visible(123,s)],[2,1,0])
        self.w.search_prefs(123)['sort']='size-desc';self.assertEqual([i for i,_ in self.w.search_visible(123,s)],[1,2,0])
    def test_restarted_catalog_session_is_recoverable_and_finished_jobs_use_fast_poll(self):
        self.command('/discover');s=self.session();s.update(phase='working',nonce='old')
        self.w.catalog_poll();self.assertEqual(s['phase'],'error')
        self.w.catalog_action(123,'catalog_random',s['id'])
        future=Future();future.set_result({'results':[]})
        self.w.search_jobs()[(123,'fixture','query')]=future
        self.assertTrue(self.w.ui_jobs_pending())

    def test_catalog_capabilities_are_scoped_to_user_and_reject_changed_session(self):
        self.command('/discover')
        button=next(b for b in self.buttons() if 'Актёры' in b['text'])
        self.w.state.setdefault('members',{})['456']={'name':'Family'}
        before=len(self.tg.messages)
        self.w.callback({'id':'q','from':{'id':456},'data':button['callback_data'],'message':{'message_id':42,'chat':{'id':456,'type':'private'}}})
        self.assertEqual(len(self.tg.messages),before);self.assertTrue(self.tg.answers[-1][2])
        self.command('/discover')
        self.w.callback({'id':'q','from':{'id':123},'data':button['callback_data'],'message':{'message_id':42,'chat':{'id':123,'type':'private'}}})
        self.assertTrue(self.tg.answers[-1][2])

    def test_new_query_generation_rejects_old_buttons_and_old_partial_events(self):
        self.w.search_start(123,'Old');s=self.w.state['searches']['123'];sid=s['id'];s['kind']='series'
        self.w.search_run(123,sid);old=s['generation']
        self.search_pool.calls[0][0].set_result({'results':[row(0)],'warnings':[]});self.w.search_poll()
        button=next(b for b in self.buttons() if self.w.state['buttons'].get(b.get('callback_data','')[3:],{}).get('action',[None])[0]=='search_pick')
        self.w.state['buttons'][button['callback_data'][3:]].pop('search',None)  # Old stored capabilities predate generation binding.
        s['query']='New';self.w.search_run(123,sid)
        self.w._search_events.put((123,sid,old,{'results':[row(1)],'warnings':[]}));self.w.search_poll()
        self.assertEqual(s['results'],[])
        self.w.callback({'id':'q','from':{'id':123},'data':button['callback_data'],'message':{'message_id':42,'chat':{'id':123,'type':'private'}}})
        self.assertTrue(self.tg.answers[-1][2]);self.assertNotEqual(s['generation'],old)

    def test_new_random_page_invalidates_previous_movie_buttons(self):
        self.command('/random');sid=self.session()['id'];self.click('Подобрать фильмы')
        self.complete({'movies':[{'imdb_id':'tt0109830','title':'First','year':1994}],'source':'TMDB'})
        button=next(b for b in self.buttons() if b['text'].startswith('First'))
        self.click('Ещё варианты');self.complete({'movies':[{'imdb_id':'tt0111161','title':'Second','year':1994}],'source':'TMDB'})
        before=len(self.pool.calls)
        self.w.callback({'id':'q','from':{'id':123},'data':button['callback_data'],'message':{'message_id':42,'chat':{'id':123,'type':'private'}}})
        self.assertTrue(self.tg.answers[-1][2]);self.assertEqual(len(self.pool.calls),before)

    def test_late_search_error_does_not_replace_home_menu(self):
        self.w.search_start(123,'Show');s=self.w.state['searches']['123'];s['kind']='series';self.w.search_run(123,s['id'])
        self.command('/menu');before=len(self.tg.messages)
        self.search_pool.calls[0][0].set_exception(APIError('offline'));self.w.search_poll()
        self.assertEqual(len(self.tg.messages),before)

    def test_already_completed_search_sends_only_one_result_update(self):
        self.w.search_start(123,'Show');s=self.w.state['searches']['123'];s['kind']='series';self.w.search_run(123,s['id'])
        result={'results':[row(0)],'warnings':[]}
        self.w._search_events.put((123,s['id'],s['generation'],result))
        self.search_pool.calls[0][0].set_result(result)
        before=len(self.tg.messages);self.w.search_poll()
        self.assertEqual(len(self.tg.messages)-before,1);self.assertFalse(s['running'])



class HistoryAndTransport(unittest.TestCase):
    def test_history_formats_and_bounds(self):
        self.assertEqual(parse_history(b'\xef\xbb\xbfimdb_id;title\ntt0109830;Forrest\n','watched.csv')[0]['imdb_id'],'tt0109830')
        self.assertEqual(parse_history(b'{"ratings":[{"kinopoisk_id":"123"}]}','watched.json')[0]['kinopoisk_id'],'123')
        for raw,name in [(b'{}','watched.json'),(b'x,y\n1,2','watched.csv'),(b'[]','x.xlsx'),(b'x'*1048577,'x.csv')]:
            with self.subTest(name=name),self.assertRaises(ValueError):parse_history(raw,name)
    def test_photo_upload_and_short_poll_transport(self):
        tg=Telegram(token='fixture-token',owner=123)
        response=MagicMock();response.__enter__.return_value=response;response.read.return_value=b'{"ok":true,"result":{"message_id":7}}'
        with patch.object(tg.http.opener,'open',return_value=response) as send:
            self.assertEqual(tg.photo(b'jpeg','<b>Title</b>',123,{'inline_keyboard':[]}),7)
            self.assertIn(b'name="photo"',send.call_args.args[0].data)
            self.assertIn(b'name="caption"',send.call_args.args[0].data)
        with patch.object(tg,'call',return_value=[]) as call:
            tg.updates(5,timeout=1);self.assertEqual(call.call_args.kwargs['timeout'],1)
    def test_download_rejects_path_traversal_and_never_exposes_token_in_errors(self):
        tg=Telegram(token='fixture-token',owner=123)
        with patch.object(tg,'call',return_value={'file_path':'../secrets'}):
            with self.assertRaises(APIError) as error:tg.download_document({'file_id':'fixture'})
        self.assertNotIn('fixture-token',str(error.exception))
