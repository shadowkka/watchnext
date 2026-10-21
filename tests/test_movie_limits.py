import unittest
import test_queue as fixtures
from core import GB, episode_count


class MovieLimits(fixtures.Lifecycle):
    def movie(self, size=2 * GB):
        self.w.tick()
        self.w.add('Movie', 1, 'magnet:?xt=urn:btih:' + 'b' * 40, kind='movie')
        p = self.store.data['seasons'][1]
        self.q.infos[p['hash']] = {'hash': p['hash'], 'save_path': p['download_root'], 'state': 'stoppedDL'}
        self.q.files[p['hash']] = [{'index': 0, 'name': 'Movie.mkv', 'size': size, 'progress': 0}]
        return p

    def test_movie_does_not_take_episode_slot_but_bytes_are_shared(self):
        self.store.data['cap'] = 15 * GB
        p = self.movie()
        self.w.tick()
        self.assertEqual(sum(episode_count(e) for e in self.s['episodes'] if e['selected']), 5)
        self.assertTrue(p['episodes'][0]['selected'])
        self.assertEqual(self.w.waiting_reason(p), '')
        self.assertIn('лимите серий', self.w.waiting_reason(self.s))
        self.assertIn(('priorities', {'hash': p['hash'], 'selected': {0}}), self.q.calls)
        self.assertIn(('start', {'hashes': p['hash']}), self.q.calls)

    def test_movie_waits_if_shared_gb_limit_full(self):
        self.store.data['cap'] = 2 * GB
        p = self.movie(size=2 * GB)
        self.w.tick()
        self.assertFalse(p['episodes'][0]['selected'])
        self.assertIn('общего лимита объёма', self.w.waiting_reason(p))
        self.assertEqual(sum(e['selected'] for e in self.s['episodes']), 5)
        self.assertIn(('priorities', {'hash': p['hash'], 'selected': set()}), self.q.calls)

    def test_movie_still_obeys_free_disk_reserve(self):
        self.store.data['reserve'] = 10**18
        p = self.movie()
        self.w.tick()
        self.assertFalse(p['episodes'][0]['selected'])

    def test_movie_does_not_block_episode_refill_when_count_full(self):
        p = self.movie(size=20)
        self.w.tick()
        self.s['episodes'][0].update(watched=True, removed=True, selected=False)
        self.w.tick()
        self.assertTrue(p['episodes'][0]['selected'])
        self.assertTrue(self.s['episodes'][5]['selected'])
        self.assertEqual(sum(e['selected'] for e in self.s['episodes']), 5)

    def test_byte_only_mode_still_limits_both_types(self):
        self.store.data.update(count=None, cap=GB)
        p = self.movie(size=2 * GB)
        self.w.tick()
        self.assertFalse(p['episodes'][0]['selected'])
        self.assertLessEqual(sum(e['size'] for e in self.s['episodes'] if e['selected']), GB)

    def test_lowered_limits_preserve_selected_movie_and_series(self):
        p = self.movie(size=100)
        self.w.tick()
        self.store.data.update(count=1, cap=1)
        self.w.tick()
        self.assertTrue(p['episodes'][0]['selected'])
        self.assertEqual(sum(e['selected'] for e in self.s['episodes']), 5)


for name in dir(fixtures.Lifecycle):
    if name.startswith('test_') and name not in MovieLimits.__dict__: setattr(MovieLimits, name, None)

if __name__ == '__main__': unittest.main()
