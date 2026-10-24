"""Bounded progressive release searches. Threads never mutate the media worker."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import uuid


class SearchJobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='release-search')
        self.jobs = {}

    def start(self, query, kind, client):
        key = (' '.join(query.casefold().split()), kind)
        with self.lock:
            now = time.monotonic()
            self.jobs = {i:j for i,j in self.jobs.items() if not j['done'] or now-j['updated'] < 120}
            for identifier, job in self.jobs.items():
                if job['key'] == key and not job['done']:
                    return identifier
            if sum(not j['done'] for j in self.jobs.values()) >= 4:
                raise ValueError('Уже выполняется несколько поисков. Повторите через несколько секунд.')
            if len(self.jobs) >= 64:
                oldest = min((i for i,j in self.jobs.items() if j['done']), key=lambda i:self.jobs[i]['updated'])
                del self.jobs[oldest]
            identifier = uuid.uuid4().hex
            self.jobs[identifier] = {'key':key,'kind':kind,'done':False,'revision':0,'results':[],
                                     'warnings':[],'completed':0,'total':0,'error':'','updated':now}
            self.pool.submit(self.run, identifier, query, kind, client)
            return identifier

    def run(self, identifier, query, kind, client):
        def update(result, completed, total):
            with self.lock:
                self.jobs[identifier].update(results=result['results'],warnings=result.get('warnings',[]),
                    completed=completed,total=total,updated=time.monotonic())
                self.jobs[identifier]['revision'] += 1
        try:
            result = client.search(query, kind, on_update=update)
            with self.lock:
                self.jobs[identifier].update(results=result['results'],warnings=result.get('warnings',[]))
        except Exception:
            with self.lock:
                self.jobs[identifier]['error'] = 'Трекеры не ответили. Проверьте их доступность в Jackett.'
        finally:
            with self.lock:
                self.jobs[identifier].update(done=True,updated=time.monotonic())
                self.jobs[identifier]['revision'] += 1

    def snapshot(self, identifier, after=-1):
        with self.lock:
            if identifier not in self.jobs:
                raise KeyError(identifier)
            job = self.jobs[identifier]
            result = {k:job[k] for k in ['kind','done','revision','completed','total','error']}
            result['id'] = identifier
            result['changed'] = after != job['revision']
            if result['changed']:
                result.update(results=list(job['results']),warnings=list(job['warnings']))
            return result
