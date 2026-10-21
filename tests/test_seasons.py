import unittest
import test_queue as fixtures
from app import library_relative
from core import episode_count, episode_label, episodes, finished, last_season, low_seasons, parse_start, plan


def files(*names):
    return [{'index':i,'name':name,'size':100,'progress':0} for i,name in enumerate(names)]


class Parsing(unittest.TestCase):
    def test_real_user_release_final_double_episode(self):
        eps=episodes(files('The.Big.Bang.Theory_s12_KB_720p/The.Big.Bang.Theory.s12e22.WEB-DL.720p_EniaHD.mkv',
                           'The.Big.Bang.Theory_s12_KB_720p/The.Big.Bang.Theory.s12e23-24.WEB-DL.720p_EniaHD.mkv',
                           'The.Big.Bang.Theory_s12_KB_720p/The.Big.Bang.Theory.s12e25.Special.WEB-DL.720p_EniaHD.mkv'),12)
        self.assertEqual([e['number'] for e in eps],[22,23,25])
        self.assertEqual(episode_count(eps[1]),2)
        self.assertEqual(episode_label(eps[1]),'S12E23-E24')
    def test_combined_formats(self):
        for name in ('S01E01E02','S01E01-E02','S01E01-02','1x01-02','S01E01+E02'):
            with self.subTest(name=name):
                e=episodes(files(name+'.mkv'),1)[0]
                self.assertEqual((e['number'],e['end_number']),(1,2))
    def test_triple_consecutive(self):
        e=episodes(files('Show.S01E01E02E03.mkv'),1)[0]
        self.assertEqual(episode_count(e),3)
    def test_nonconsecutive_rejected(self):
        with self.assertRaises(ValueError):episodes(files('S01E01E03.mkv'),1)
    def test_multiple_seasons_sorted(self):
        es=episodes(files('Show.S03E01.mkv','Show.S01E02.mkv','Show.S02E01.mkv'),1)
        self.assertEqual([(e['season'],e['number']) for e in es],[(1,2),(2,1),(3,1)])
    def test_season_from_folders(self):
        for folder in ('Season 02','Сезон 2','2 сезон','Show_s02_720p'):
            es=episodes(files(folder+'/01 - Начало.mkv',folder+'/Серия 02.mkv'),1)
            self.assertEqual([(e['season'],e['number']) for e in es],[(2,1),(2,2)])
    def test_explicit_file_season_overrides_collection_folder(self):
        self.assertEqual(episodes(files('Show.S01-S03/Show.S03E02.mkv'),1)[0]['season'],3)
    def test_start_excludes_earlier_seasons_and_episodes(self):
        es=episodes(files('S01E01.mkv','S02E03.mkv','S02E04.mkv','S03E01.mkv'),2,4)
        self.assertEqual([e['skipped_start'] for e in es],[True,True,False,False])
        self.assertFalse(any(e['watched'] for e in es))
        self.assertEqual(plan([(str(i),e) for i,e in enumerate(es)],5,None,1000),{'2','3'})
    def test_start_inside_double_file_keeps_whole_file(self):
        e=episodes(files('S12E23-24.mkv'),12,24)[0]
        self.assertFalse(e['skipped_start']);self.assertEqual(episode_count(e),2)
    def test_double_counts_as_two_slots(self):
        e=episodes(files('S01E01-E02.mkv'),1)[0]
        self.assertEqual(plan([('a',e)],1,None,1000),set())
        self.assertEqual(plan([('a',e)],2,None,1000),{'a'})
    def test_overlapping_number_ranges_rejected(self):
        with self.assertRaises(ValueError):episodes(files('S01E01-E02.mkv','S01E02.mkv'),1)
    def test_resolution_is_not_an_episode_range(self):
        e=episodes(files('S01E01-720p.WEB-DL.mkv'),1)[0]
        self.assertEqual(episode_count(e),1)
    def test_start_past_end_rejected(self):
        with self.assertRaises(ValueError):episodes(files('S01E01.mkv'),2)
    def test_start_input_formats(self):
        for text in ('S12E05','12 5','12x5','12:5'):
            self.assertEqual(parse_start(text),(12,5))
        self.assertEqual(parse_start('12'),(12,1))
    def test_jellyfin_paths_use_actual_season_and_range(self):
        e=episodes(files('S03E01-E02.mkv'),1)[0]
        self.assertEqual(library_relative({'title':'Show','season':1},e),'Show/Season 03/Show - S03E01-E02.mkv')
    def test_notification_uses_latest_season_in_pack(self):
        a={'title':'Show','season':1,'episodes':episodes(files('S01E01.mkv','S03E01.mkv'),1)}
        b={'title':'Show','season':2,'episodes':episodes(files('S02E01.mkv'),2)}
        self.assertEqual(last_season(a),3)
        self.assertEqual(low_seasons([a,b],2),[a])


class Commands(fixtures.Lifecycle):
    def test_add_with_startpoint(self):
        self.command('/add New Show | S02E05 | magnet:?xt=urn:btih:'+'b'*40)
        p=self.store.data['seasons'][1]
        self.assertEqual((p['season'],p['start_episode']),(2,5))
    def test_add_with_separate_season_episode(self):
        self.command('/add New Show | 2 | 5 | magnet:?xt=urn:btih:'+'b'*40)
        p=self.store.data['seasons'][1]
        self.assertEqual((p['season'],p['start_episode']),(2,5))
    def test_from_before_metadata_retries_error_and_starts(self):
        self.s['error']='old parser error'
        self.command('/from '+fixtures.H[:8]+' S01E03')
        self.assertNotIn('error',self.s)
        self.assertFalse(self.store.data['paused'])
        self.w.tick()
        self.assertEqual([e['skipped_start'] for e in self.s['episodes']],[True,True,False,False,False,False])
        self.assertTrue(self.s['episodes'][2]['selected'])
        self.assertFalse(self.s['episodes'][0]['selected'])
    def test_start_can_move_back_without_resetting_watched(self):
        self.w.tick();self.command('/from '+fixtures.H[:8]+' 1 4')
        self.s['episodes'][3]['watched']=True
        self.command('/from '+fixtures.H[:8]+' 1 2')
        self.assertFalse(self.s['episodes'][1]['skipped_start'])
        self.assertFalse(self.s['episodes'][1]['removed'])
        self.assertTrue(self.s['episodes'][3]['watched'])
    def test_multiseason_single_handle_and_correct_destinations(self):
        self.q.files[fixtures.H]=files('Show.S01E01.mkv','Show.S02E01-E02.mkv','Show.S02E03.mkv')
        self.w.tick()
        self.assertEqual(len(self.store.data['seasons']),1)
        self.assertIn('/Season 02/',str(self.w.dest(self.s,self.s['episodes'][1])))
        self.assertEqual(sum(episode_count(e) for e in self.s['episodes']),4)

for name in dir(fixtures.Lifecycle):
    if name.startswith('test_') and name not in Commands.__dict__:setattr(Commands,name,None)

if __name__=='__main__':unittest.main()
