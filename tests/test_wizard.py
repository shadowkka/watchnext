import unittest
import test_queue as fixtures
from core import low_seasons,movie_files
from app import Store,Worker
from test_seasons import files

class Wizard(fixtures.Lifecycle):
    def start_series(self):
        for message in ('/add','Сериал','New Show','2','5'):
            self.command(message)

    def test_bare_add_prompts_type(self):
        self.command('/add')
        self.assertIn('Что добавляем',self.tg.messages[-1])
        self.assertEqual(self.store.data['dialogs']['123']['step'],'kind')
        self.assertEqual(len(self.store.data['seasons']),1)

    def test_series_full_dialog(self):
        self.start_series()
        self.assertEqual(self.store.data['dialogs']['123']['step'],'magnet')
        self.command('magnet:?xt=urn:btih:'+'b'*40)
        p=self.store.data['seasons'][1]
        self.assertEqual((p['kind'],p['title'],p['season'],p['start_episode']),('series','New Show',2,5))
        self.assertNotIn('123',self.store.data['dialogs'])

    def test_movie_asks_only_title_and_magnet(self):
        self.command('/add');self.command('Фильм')
        self.command('Movie (2020)')
        self.assertEqual(self.store.data['dialogs']['123']['step'],'magnet')
        self.command('magnet:?xt=urn:btih:'+'b'*40)
        p=self.store.data['seasons'][1]
        self.assertEqual((p['kind'],p['title']),('movie','Movie (2020)'))
        self.assertNotIn('123',self.store.data['dialogs'])

    def test_invalid_magnet_keeps_dialog(self):
        self.start_series()
        with self.assertRaises(ValueError):self.command('bad magnet')
        self.assertEqual(self.store.data['dialogs']['123']['step'],'magnet')
        self.assertEqual(len(self.store.data['seasons']),1)

    def test_cancel_clears_only_own_dialog(self):
        self.store.data['members']={'456':{'name':'Family'}}
        self.command('/add');self.command('/add',sender=456)
        self.command('Отмена')
        self.assertNotIn('123',self.store.data['dialogs'])
        self.assertIn('456',self.store.data['dialogs'])

    def test_parallel_dialogs_do_not_mix(self):
        self.store.data['members']={'456':{'name':'Family'}}
        self.command('/add');self.command('Сериал')
        self.command('/add',sender=456);self.command('Фильм',sender=456)
        self.command('His Show');self.command('Her Movie',sender=456)
        self.assertEqual(self.store.data['dialogs']['123']['step'],'season')
        self.assertEqual(self.store.data['dialogs']['456']['step'],'magnet')
        self.assertEqual(self.store.data['dialogs']['123']['title'],'His Show')
        self.assertEqual(self.store.data['dialogs']['456']['title'],'Her Movie')

    def test_status_during_dialog_does_not_consume_input(self):
        self.start_series();self.command('/status')
        self.assertEqual(self.store.data['dialogs']['123']['step'],'magnet')

    def test_dialog_survives_restart(self):
        self.start_series()
        fresh=Store(self.store.path)
        worker=Worker(fresh,self.q,self.j,self.tg,self.root/'data')
        worker.command({'from':{'id':123},'chat':{'id':123,'type':'private'},'text':'magnet:?xt=urn:btih:'+'b'*40})
        self.assertEqual(fresh.data['seasons'][1]['start_episode'],5)

    def test_auto_season_uses_actual_file_season(self):
        for message in ('/add','Сериал','Auto Show','Авто','3','magnet:?xt=urn:btih:'+'b'*40):self.command(message)
        p=self.store.data['seasons'][1]
        self.q.infos[p['hash']]={'hash':p['hash'],'save_path':p['download_root'],'state':'stoppedDL'}
        self.q.files[p['hash']]=files('Auto.S12E01.mkv','Auto.S12E03.mkv')
        self.store.data['paused']=True;self.w.tick()
        self.assertEqual(p['season'],12)
        self.assertEqual([e['skipped_start'] for e in p['episodes']],[True,False])

    def test_auto_season_numbered_files_default_one(self):
        for message in ('/add','Сериал','Auto Show','Авто','1','magnet:?xt=urn:btih:'+'b'*40):self.command(message)
        p=self.store.data['seasons'][1]
        self.q.infos[p['hash']]={'hash':p['hash'],'save_path':p['download_root'],'state':'stoppedDL'}
        self.q.files[p['hash']]=files('01.mkv','02.mkv')
        self.store.data['paused']=True;self.w.tick()
        self.assertEqual(p['season'],1)

    def test_movie_uses_largest_video_and_movies_directory(self):
        for message in ('/add','Фильм','Film','magnet:?xt=urn:btih:'+'b'*40):self.command(message)
        p=self.store.data['seasons'][1]
        self.q.infos[p['hash']]={'hash':p['hash'],'save_path':p['download_root'],'state':'stoppedDL'}
        fs=files('Movie.Release.2160p.mkv','sample.mkv');fs[1]['size']=1
        self.q.files[p['hash']]=fs
        self.store.data['paused']=True;self.w.tick()
        self.assertEqual(len(p['episodes']),1)
        self.assertEqual(str(self.w.dest(p,p['episodes'][0])),str(self.w.movies/'Film/Film.mkv'))
        self.assertNotIn(p,low_seasons(self.store.data['seasons'],999))

    def test_from_is_not_offered_for_movie(self):
        for message in ('/add','Фильм','Film','magnet:?xt=urn:btih:'+'b'*40):self.command(message)
        with self.assertRaises(ValueError):self.command('/from bbbbbbbb S01E01')

    def test_nonmember_cannot_start_dialog(self):
        self.command('/add',sender=456)
        self.assertNotIn('456',self.store.data.get('dialogs',{}))

for name in dir(fixtures.Lifecycle):
    if name.startswith('test_') and name not in Wizard.__dict__:setattr(Wizard,name,None)

class MovieParsing(unittest.TestCase):
    def test_movie_does_not_require_season_number(self):
        e=movie_files(files('Some Film (2020) BluRay.mkv'))[0]
        self.assertEqual(e['number'],1)
    def test_largest_non_sample_video(self):
        fs=files('Feature.mkv','samples/Big Sample.mkv','Trailer.mp4','Featurette.mp4')
        fs[0]['size']=200;fs[1]['size']=500;fs[2]['size']=300;fs[3]['size']=50
        self.assertEqual(movie_files(fs)[0]['index'],0)

if __name__=='__main__':unittest.main()
