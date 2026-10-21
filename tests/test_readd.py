import copy
import unittest
import test_queue as fixtures
from core import low_seasons


class ReAdd(fixtures.Lifecycle):
    def test_stale_record_is_replaced_at_same_position(self):
        self.w.tick();self.q.infos.pop(fixtures.H)
        result=self.w.add('Show',1,fixtures.MAGNET,start_episode=3)
        self.assertIn('Восстановлен',result)
        self.assertEqual(len(self.store.data['seasons']),1)
        p=self.store.data['seasons'][0]
        self.assertEqual((p['phase'],p['start_episode']),('metadata',3))
        self.assertFalse(p['submitted'])
        self.assertEqual(p['episodes'],[])

    def test_existing_torrent_still_deduplicated(self):
        self.w.tick();old=copy.deepcopy(self.s)
        result=self.w.add('Show',1,fixtures.MAGNET)
        self.assertIn('уже добавлен',result)
        self.assertEqual(old,self.s)
        self.assertEqual(len(self.store.data['seasons']),1)

    def test_external_torrent_not_taken_over(self):
        self.store.data['seasons']=[]
        with self.assertRaises(ValueError):self.w.add('Show',1,fixtures.MAGNET)

    def test_deleted_torrent_is_suspended_without_readd(self):
        self.w.tick();self.q.infos.pop(fixtures.H);self.q.calls.clear()
        self.w.tick()
        self.assertEqual(self.s['phase'],'missing')
        self.assertFalse(any(e['selected'] for e in self.s['episodes']))
        self.assertFalse(any(op=='add' for op,_ in self.q.calls))
        before=len(self.tg.messages);self.w.tick()
        self.assertEqual(len(self.tg.messages),before)
        self.assertFalse(any(op=='add' for op,_ in self.q.calls))
        self.assertIn('удалена из qBittorrent',self.w.status())
        self.assertNotIn(self.s,low_seasons(self.store.data['seasons'],999))

    def test_deletion_before_metadata_also_respected(self):
        self.s['submitted']=True;self.q.infos.pop(fixtures.H)
        self.w.tick()
        self.assertEqual(self.s['phase'],'missing')
        self.assertFalse(any(op=='add' for op,_ in self.q.calls))

    def test_first_submission_still_adds_torrent_once(self):
        self.q.infos.pop(fixtures.H)
        self.w.tick()
        self.assertTrue(self.s['submitted'])
        self.assertEqual(sum(op=='add' for op,_ in self.q.calls),1)

    def test_missing_pack_does_not_block_other_downloads(self):
        self.w.tick();self.q.infos.pop(fixtures.H)
        magnet='magnet:?xt=urn:btih:'+'b'*40
        self.w.add('Other',1,magnet)
        other=self.store.data['seasons'][1];h=other['hash']
        self.q.infos[h]={'hash':h,'save_path':other['download_root'],'state':'stoppedDL'}
        self.q.files[h]=[fixtures.file(0)]
        self.q.calls.clear();self.w.tick()
        self.assertEqual(self.s['phase'],'missing')
        self.assertTrue(other['episodes'][0]['selected'])
        self.assertTrue(any(op=='start' and args['hashes']==h for op,args in self.q.calls))

    def test_movie_can_be_readded_after_completion(self):
        self.s.update(kind='movie',phase='complete');self.q.infos.pop(fixtures.H)
        result=self.w.add('Show',1,fixtures.MAGNET,kind='movie')
        self.assertIn('Восстановлен',result)
        self.assertNotIn('recovery',self.store.data['seasons'][0])
        self.assertEqual(self.store.data['seasons'][0]['kind'],'movie')

    def test_published_hardlink_reused_if_source_deleted_with_torrent(self):
        e=self.publish_first();dst=self.w.dest(self.s,e)
        self.w.source(self.s,e).unlink();self.q.infos.pop(fixtures.H)
        self.w.add('Changed Name',1,fixtures.MAGNET)
        p=self.store.data['seasons'][0]
        self.assertEqual(p['title'],'Show')
        self.assertTrue(self.w.source(p,e).exists())
        self.assertTrue(dst.exists())
        self.q.infos[fixtures.H]={'hash':fixtures.H,'save_path':p['download_root'],'state':'stoppedDL'}
        self.w.tick()
        self.assertTrue(p['episodes'][0]['published'])

    def test_readding_clears_stale_reply_links(self):
        self.store.data['replies']={'123:5':fixtures.H,'456:9':'b'*40}
        self.q.infos.pop(fixtures.H)
        self.w.add('Show',1,fixtures.MAGNET)
        self.assertEqual(self.store.data['replies'],{'456:9':'b'*40})

for name in dir(fixtures.Lifecycle):
    if name.startswith('test_') and name not in ReAdd.__dict__:setattr(ReAdd,name,None)

if __name__=='__main__':unittest.main()
