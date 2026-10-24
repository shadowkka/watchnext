import json
import threading
import unittest
from unittest.mock import patch
from search_client import Jackett
from release_search import SearchJobs


class ProgressiveSearch(unittest.TestCase):
    def test_fast_tracker_published_before_slow_tracker_and_slow_result_not_lost(self):
        gate=threading.Event();first=threading.Event();updates=[];result=[]
        client=Jackett()
        def request(path, query=None):
            slow='/slow/' in path
            if slow:gate.wait(2)
            return json.dumps({'Results':[{'Title':'Slow' if slow else 'Fast','MagnetUri':'magnet:?xt=urn:btih:'+('b' if slow else 'a')*40,'Category':[5000]}], 'Indexers':[{'Name':'slow' if slow else 'fast','Status':2}]}).encode()
        def on_update(value,done,total):
            updates.append((value,done,total));first.set()
        with patch.object(client,'indexers',return_value=['slow','fast']),patch.object(client,'request',side_effect=request):
            thread=threading.Thread(target=lambda:result.append(client.search('Show','series',on_update)))
            thread.start()
            try:
                self.assertTrue(first.wait(1))
                self.assertEqual(updates[0][0]['results'][0]['title'],'Fast')
                self.assertTrue(thread.is_alive())
            finally:
                gate.set();thread.join(2)
        self.assertEqual({r['title'] for r in result[0]['results']},{'Fast','Slow'})
        self.assertEqual(updates[-1][1:],(2,2))

    def test_jobs_share_active_query_and_expose_partial_revisions(self):
        gate=threading.Event();first=threading.Event()
        class Client:
            def search(self,query,kind,on_update):
                result={'results':[{'title':'Fast'}],'warnings':[]}
                on_update(result,1,2);first.set();gate.wait(2)
                return {'results':[{'title':'Fast'},{'title':'Slow'}],'warnings':[]}
        jobs=SearchJobs()
        try:
            identifier=jobs.start('Show','series',Client())
            self.assertTrue(first.wait(1))
            self.assertEqual(jobs.start(' show ','series',Client()),identifier)
            snapshot=jobs.snapshot(identifier)
            self.assertFalse(snapshot['done']);self.assertEqual(snapshot['results'][0]['title'],'Fast')
            unchanged=jobs.snapshot(identifier,snapshot['revision'])
            self.assertFalse(unchanged['changed']);self.assertNotIn('results',unchanged)
        finally:
            gate.set();jobs.pool.shutdown(wait=True)
        self.assertTrue(jobs.snapshot(identifier)['done'])
        self.assertEqual(len(jobs.snapshot(identifier)['results']),2)

    def test_job_capacity_is_bounded_and_errors_do_not_expose_upstream_secrets(self):
        gate=threading.Event()
        class Client:
            def search(self,*args,**kwargs):gate.wait(2);raise ValueError('secret-api-key')
        jobs=SearchJobs()
        try:
            ids=[jobs.start(str(i),'movie',Client()) for i in range(4)]
            with self.assertRaises(ValueError):jobs.start('extra','movie',Client())
        finally:
            gate.set();jobs.pool.shutdown(wait=True)
        for identifier in ids:
            snapshot=jobs.snapshot(identifier)
            self.assertTrue(snapshot['done']);self.assertNotIn('secret-api-key',str(snapshot))
