import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from app import Store, Worker
from core import GB, episodes, low_seasons, magnet_hash, plan, safe_path
from net import APIError

H = 'a' * 40
MAGNET = 'magnet:?xt=urn:btih:' + H

def file(i, size=10, progress=0):
    return {'index': i, 'name': f'Pack/Show.S01E{i + 1:02d}.mkv', 'size': size, 'progress': progress}

class FakeQ:
    def __init__(self):
        self.infos, self.files, self.calls = {}, {}, []
    def get(self, endpoint, **kw):
        if endpoint == 'info':
            return [copy.deepcopy(t) for h, t in self.infos.items() if 'hashes' not in kw or h == kw['hashes']]
        return copy.deepcopy(self.files.get(kw['hash'], []))
    def post(self, endpoint, **kw):
        self.calls.append((endpoint, kw))
        h = kw.get('hashes')
        if endpoint == 'stop' and h in self.infos and 'checking' not in self.infos[h]['state'].lower():
            self.infos[h]['state'] = 'stoppedDL'
        if endpoint == 'start' and h in self.infos:
            self.infos[h]['state'] = 'downloading'
        if endpoint == 'recheck':
            self.infos[h]['state'] = 'checkingDL'
        if endpoint == 'delete':
            self.infos.pop(h, None)
        return 'Ok.'
    def priorities(self, h, files, selected):
        self.calls.append(('priorities', {'hash': h, 'selected': selected}))
    def add(self, s):
        self.calls.append(('add', {'hash': s['hash']}))

class FakeJ:
    def __init__(self):
        self.played, self.active, self.fail, self.refreshed = set(), set(), False, 0
    def snapshot(self):
        if self.fail:
            raise APIError('offline')
        return set(self.played), set(self.active)
    def refresh(self):
        self.refreshed += 1

class FakeTG:
    owner = 123
    bot_username = 'test_bot'
    def __init__(self):
        self.messages = []
        self.sent = []
        self.markups = []
        self.edits = []
        self.answers = []
    def send(self, text, chat_id=None, reply_markup=None):
        chat_id = self.owner if chat_id is None else chat_id
        self.messages.append(text)
        self.markups.append(reply_markup)
        self.sent.append((chat_id, text))
        return sum(uid == chat_id for uid, _ in self.sent)

    def edit(self, text, chat_id, message_id, reply_markup):
        self.edits.append((chat_id, message_id, text, reply_markup))
        self.markups.append(reply_markup)
        self.messages.append(text)

    def answer(self, query_id, text='', alert=False):
        self.answers.append((query_id, text, alert))

class Policy(unittest.TestCase):
    def test_magnet(self):
        self.assertEqual(magnet_hash(MAGNET), H)
        self.assertEqual(magnet_hash('magnet:?xt=urn:btih:' + 'A' * 32), '0' * 40)
        for bad in ('https://example.com', 'magnet:?xt=urn:btmh:1220bad', 'magnet:?xt=urn:btih:bad'):
            with self.assertRaises(ValueError): magnet_hash(bad)
    def test_order(self):
        self.assertEqual([e['number'] for e in episodes([file(9), file(0), file(1)], 1)], [1, 2, 10])
    def test_numbered(self):
        self.assertEqual(episodes([{'index': 0, 'name': '01.mkv', 'size': 1}], 5)[0]['number'], 1)
    def test_ambiguous(self):
        for names in (['show.mkv'], ['Show.S01E01.mkv', 'Other.S01E01.mp4'], ['Show.S01E03-E02.mkv']):
            with self.subTest(names=names), self.assertRaises(ValueError):
                episodes([{'index': i, 'name': n, 'size': 1} for i, n in enumerate(names)], 1)
    def test_sample(self):
        self.assertEqual(len(episodes([file(0), {'index': 8, 'name': 'sample.mkv', 'size': 1}], 1)), 1)
    def rows(self):
        return [(str(i), e) for i, e in enumerate(episodes([file(i, 4) for i in range(6)], 1))]
    def test_count(self):
        self.assertEqual(plan(self.rows(), 5, None, 100), {'0', '1', '2', '3', '4'})
    def test_bytes(self):
        self.assertEqual(plan(self.rows(), None, 10, 100), {'0', '1'})
    def test_both(self):
        self.assertEqual(plan(self.rows(), 1, 10, 100), {'0'})
    def test_disk(self):
        self.assertEqual(plan(self.rows(), None, 100, 9), {'0', '1'})
    def test_no_skipping(self):
        rows = self.rows(); rows[0][1]['size'] = 100
        self.assertEqual(plan(rows, 5, 10, 1000), set())
    def test_preserve_on_lower_limit(self):
        rows = self.rows()
        for _, e in rows[:3]: e['selected'] = True
        self.assertEqual(plan(rows, 1, 1, 0), {'0', '1', '2'})
    def test_slide(self):
        rows = self.rows()
        for _, e in rows[:5]: e['selected'] = True
        rows[0][1]['watched'] = True
        self.assertEqual(plan(rows, 5, None, 100), {'1', '2', '3', '4', '5'})
    def test_notify_latest_once(self):
        ss = [{'title': 'Show', 'season': n, 'episodes': [{'watched': False}], 'notified': False} for n in (1, 2)]
        self.assertEqual(low_seasons(ss, 2), [ss[1]])
        ss[1]['notified'] = True
        self.assertEqual(low_seasons(ss, 2), [])
    def test_paths(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(safe_path(d, 'a/video.mkv'), Path(d).resolve() / 'a/video.mkv')
            for rel in ('../out', '/tmp/out'):
                with self.assertRaises(ValueError): safe_path(d, rel)
            (Path(d) / 'link').symlink_to(Path(d), target_is_directory=True)
            with self.assertRaises(ValueError): safe_path(d, 'link/file')

class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state' / 'state.json')
        self.q, self.j, self.tg = FakeQ(), FakeJ(), FakeTG()
        self.w = Worker(self.store, self.q, self.j, self.tg, self.root / 'data')
        self.w.grace = 0; self.store.data['reserve'] = 0
        self.w.add('Show', 1, MAGNET); self.s = self.store.data['seasons'][0]
        self.q.infos[H] = {'hash': H, 'save_path': self.s['download_root'], 'state': 'stoppedDL'}
        self.q.files[H] = [file(i) for i in range(6)]
    def tearDown(self): self.temp.cleanup()
    def publish_first(self):
        self.w.tick(); e = self.s['episodes'][0]; p = self.w.source(self.s, e)
        p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'x' * e['size'])
        self.q.files[H][0]['progress'] = 1; self.w.tick()
        return e
    def command(self, text, sender=123, reply=None, chat_type='private'):
        self.w.command({'from': {'id': sender}, 'chat': {'id': sender, 'type': chat_type},
                        'text': text, 'reply_to_message': {'message_id': reply}})
    def test_metadata_selection(self):
        self.w.tick()
        self.assertEqual(sum(e['selected'] for e in self.s['episodes']), 5)
        self.assertFalse(self.s['episodes'][5]['selected'])
    def test_publish_delete_refill(self):
        e = self.publish_first()
        self.assertTrue(os.path.samefile(self.w.source(self.s, e), self.w.dest(self.s, e)))
        self.j.played.add(str(self.w.dest(self.s, e)))
        self.w.tick(); self.w.tick()
        self.assertFalse(self.w.source(self.s, e).exists()); self.assertFalse(self.w.dest(self.s, e).exists())
        self.assertEqual(self.s['phase'], 'checking')
        self.w.tick(); self.assertEqual(self.q.infos[H]['state'], 'checkingDL')
        self.q.infos[H]['state'] = 'stoppedDL'; self.q.files[H][0]['progress'] = 0
        self.w.tick(); self.w.tick()
        self.assertTrue(self.s['episodes'][5]['selected']); self.assertFalse(e['selected'])
    def download_window(self):
        self.w.tick()
        for e in self.s['episodes']:
            if not e['selected']: continue
            p=self.w.source(self.s,e);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x'*e['size'])
            self.q.files[H][e['index']]['progress']=1
        self.q.calls.clear();self.w.tick()

    def test_downloaded_window_stops_seeding_and_stays_stopped(self):
        self.download_window()
        self.assertEqual(self.q.infos[H]['state'],'stoppedDL')
        self.assertTrue(self.s['download_idle'])
        self.assertTrue(all(e['published'] for e in self.s['episodes'][:5]))
        self.q.calls.clear();self.w.tick()
        self.assertFalse(any(op=='start' for op,_ in self.q.calls))
        self.assertIn('раздача остановлена',self.w.status())

    def test_stopped_completed_window_resumes_next_after_watching(self):
        self.download_window();e=self.s['episodes'][0]
        self.j.played.add(str(self.w.dest(self.s,e)))
        self.w.tick();self.assertEqual(self.s['phase'],'checking')
        self.q.infos[H]['state']='stoppedDL';self.q.files[H][0]['progress']=0
        self.w.tick();self.q.calls.clear();self.w.tick()
        self.assertTrue(e['removed']);self.assertFalse(e['selected'])
        self.assertTrue(self.s['episodes'][5]['selected'])
        self.assertFalse(self.s['download_idle'])
        self.assertEqual(self.q.infos[H]['state'],'downloading')
        self.assertFalse(self.store.data['paused'])

    def test_one_incomplete_selected_file_keeps_download_active(self):
        self.download_window();self.q.files[H][4]['progress']=0.5
        self.w.tick();self.assertFalse(self.s['download_idle'])
        self.assertEqual(self.q.infos[H]['state'],'downloading')

    def test_playing_preserved(self):
        e = self.publish_first(); path = str(self.w.dest(self.s, e))
        self.j.played.add(path); self.j.active.add(path); self.w.tick()
        self.assertFalse(e['watched']); self.assertTrue(self.w.source(self.s, e).exists())
    def test_grace(self):
        e = self.publish_first(); self.w.grace = 120; self.j.played.add(str(self.w.dest(self.s, e)))
        self.w.tick(); self.assertIsNotNone(e['seen_at']); self.assertFalse(e['watched'])
    def test_jellyfin_outage_preserves_files(self):
        e = self.publish_first(); self.j.fail = True
        with self.assertRaises(APIError): self.w.tick()
        self.assertTrue(self.w.source(self.s, e).exists())
    def test_restart_deletion(self):
        e = self.publish_first(); e['watched'] = True; self.store.save()
        fresh = Store(self.store.path); w = Worker(fresh, self.q, self.j, self.tg, self.root / 'data')
        w.tick(); w.tick()
        self.assertTrue(fresh.data['seasons'][0]['episodes'][0]['removed'])
    def test_owner_and_limits(self):
        self.command('/limit episodes 1', sender=456); self.command('/limit episodes 1', chat_type='group')
        self.assertEqual(self.store.data['count'], 5)
        self.command('/limit both 3 7'); self.assertEqual((self.store.data['count'], self.store.data['cap']), (3, 7 * GB))
    def test_reply_next_season(self):
        self.w.tick()
        for e in self.s['episodes'][:4]: e['watched'], e['removed'], e['selected'] = True, True, False
        self.w.tick(); mid = next(int(x.split(':')[-1]) for x in self.store.data['replies'])
        self.command('magnet:?xt=urn:btih:' + 'b' * 40, reply=mid)
        self.assertEqual(self.store.data['seasons'][1]['season'], 2)
        self.assertEqual(self.store.data['seasons'][1]['title'], 'Show')
        before = len(self.tg.messages); self.w.tick(); self.assertEqual(len(self.tg.messages), before)
    def test_pause(self):
        e = self.publish_first(); self.j.played.add(str(self.w.dest(self.s, e))); self.command('/pause')
        self.q.calls.clear(); self.w.tick()
        self.assertFalse(e['watched']); self.assertFalse(any(op == 'start' for op, _ in self.q.calls))
    def test_changed_folder_refused(self):
        self.q.infos[H]['save_path'] = '/elsewhere'
        with self.assertRaises(APIError): self.w.tick()
        self.assertTrue(any(op == 'stop' for op, _ in self.q.calls))
    def test_disk_shortage(self):
        self.w.tick(); self.q.calls.clear(); self.store.data['reserve'] = 500
        with patch('app.shutil.disk_usage', return_value=SimpleNamespace(free=100)): self.w.tick()
        self.assertFalse(any(op == 'start' for op, _ in self.q.calls))
        self.assertTrue(any('Недостаточно' in text for text in self.tg.messages))
    def test_complete_cleanup(self):
        self.w.tick()
        for e in self.s['episodes']: e['watched'], e['removed'], e['selected'] = True, True, False
        folder = Path(self.s['download_root']); folder.mkdir(parents=True, exist_ok=True)
        (folder / '.parts').write_text('boundary pieces'); self.s['phase'] = 'checking'
        self.q.infos[H]['state'] = 'stoppedDL'; self.w.tick()
        self.assertEqual(self.s['phase'], 'retiring'); self.w.tick(); self.w.tick()
        self.assertEqual(self.s['phase'], 'complete'); self.assertFalse(folder.exists()); self.assertNotIn(H, self.q.infos)
        self.w.tick(); self.assertFalse(any(op == 'add' for op, _ in self.q.calls))
    def test_state_roundtrip(self):
        self.store.save(); self.assertEqual(Store(self.store.path).data, self.store.data)
    def test_invalid_magnet_no_mutation(self):
        before = copy.deepcopy(self.store.data)
        with self.assertRaises(ValueError): self.w.add('Other', 1, 'bad')
        self.assertEqual(before, self.store.data)
    def test_manual_jellyfin_delete_retires_source_without_watched_history(self):
        e = self.publish_first()
        self.w.dest(self.s, e).unlink()
        self.w.tick(); self.w.tick()
        self.assertTrue(e['discarded'])
        self.assertFalse(e['watched'])
        self.assertTrue(e['removed'])
        self.assertFalse(self.w.source(self.s, e).exists())
        self.q.infos[H]['state'] = 'stoppedDL'
        self.q.files[H][0]['progress'] = 0
        self.w.tick(); self.w.tick()
        self.assertTrue(self.s['episodes'][5]['selected'])
        self.assertFalse(e['selected'])

    def test_manual_delete_defers_source_cleanup_during_playback(self):
        e = self.publish_first()
        path = str(self.w.dest(self.s, e))
        self.j.active.add(path)
        self.w.dest(self.s, e).unlink()
        self.w.tick(); self.w.tick()
        self.assertTrue(e['discarded'])
        self.assertFalse(e['removed'])
        self.assertTrue(self.w.source(self.s, e).exists())

    def test_missing_library_and_source_fail_closed(self):
        e = self.publish_first()
        self.w.dest(self.s, e).unlink()
        self.w.source(self.s, e).unlink()
        with self.assertRaises(APIError): self.w.tick()
        self.assertFalse(e.get('discarded', False))
        self.assertFalse(e['watched'])

    def test_invalid_numeric_limits(self):
        for text in ('/limit gb nan', '/limit gb inf', '/reserve nan', '/notify nan'):
            with self.assertRaises((ValueError, OverflowError)): self.command(text)

if __name__ == '__main__': unittest.main()
