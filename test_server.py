import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    from fastapi.testclient import TestClient
    import server
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

from app import Store, Worker
from core import GB
try:
    import test_queue as fixtures
except ImportError:
    from tests import test_queue as fixtures

H1 = 'a' * 40
H2 = 'b' * 40
MAGNET1 = 'magnet:?xt=urn:btih:' + H1
MAGNET2 = 'magnet:?xt=urn:btih:' + H2


@unittest.skipUnless(HAS_FASTAPI, "fastapi and dependencies are required for REST API server tests")
class ServerApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / 'data'
        self.state_file = Path(self.tmp.name) / 'state' / 'state.json'
        self.store = Store(str(self.state_file))

        self.fake_q = fixtures.FakeQ()
        self.fake_j = fixtures.FakeJ()
        self.fake_tg = fixtures.FakeTG()

        self.worker = Worker(self.store, self.fake_q, self.fake_j, self.fake_tg, data_root=str(self.data_dir))
        server.global_worker = self.worker
        server.API_TOKEN = ""

        # Dummy jackett mock
        self.worker.jackett = MagicMock()
        self.worker.jackett.search.return_value = {
            "results": [
                {
                    "title": "House of the Dragon S02E01 1080p LostFilm",
                    "tracker": "RuTor",
                    "size": 6000000000,
                    "seeders": 120,
                    "peers": 15,
                    "quality": "1080p · WEB-DL",
                    "magnet": MAGNET1,
                    "link": "",
                }
            ]
        }

        self.client = TestClient(server.app)

    def test_jellyfin_status_checks_authenticated_service_and_handles_outage(self):
        self.worker.j.http = MagicMock()
        self.worker.j.http.call.return_value = {'Id': 'synthetic-server'}
        self.assertEqual(self.client.get('/api/jellyfin/status').json(), {'connected': True})
        self.worker.j.http.call.assert_called_with('/System/Info', timeout=3)
        self.worker.j.http.call.side_effect = RuntimeError('offline')
        self.assertEqual(self.client.get('/api/jellyfin/status').json(), {'connected': False})
        server.API_TOKEN = 'test-token'
        self.assertEqual(self.client.get('/api/jellyfin/status').status_code, 401)

    def tearDown(self):
        self.tmp.cleanup()
        server.global_worker = None

    def test_root_and_health(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get("status"), "ok")

        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get("status"), "ok")

    def test_get_status(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["episodeCountLimit"], 5)
        self.assertFalse(data["isPaused"])
        self.assertEqual(data["reserveGb"], 5.0)

    def test_pause_resume_toggle(self):
        r = self.client.post("/api/queue/pause")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["isPaused"])
        self.assertTrue(self.worker.state["paused"])

        r = self.client.post("/api/queue/resume")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["isPaused"])
        self.assertFalse(self.worker.state["paused"])

        r = self.client.post("/api/queue/toggle-pause")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["isPaused"])

    def test_update_limits(self):
        r = self.client.post("/api/limits", json={"episodeCountLimit": 8, "capGb": 50.0, "reserveGb": 10.0})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.worker.state["count"], 8)
        self.assertEqual(self.worker.state["cap"], 50 * GB)
        self.assertEqual(self.worker.state["reserve"], 10 * GB)

        r = self.client.get("/api/status")
        data = r.json()
        self.assertEqual(data["episodeCountLimit"], 8)
        self.assertEqual(data["capGb"], 50.0)
        self.assertEqual(data["reserveGb"], 10.0)

    def test_clear_cap_and_persist_across_worker_restart(self):
        self.client.post("/api/limits", json={"episodeCountLimit": 12, "capGb": 80, "reserveGb": 18})
        r = self.client.post("/api/limits", json={"capGb": None})
        self.assertEqual(r.status_code, 200)
        # Restart uses the same durable state, as an image update does.
        restarted = Store(str(self.state_file))
        self.assertEqual(restarted.data["count"], 12)
        self.assertEqual(restarted.data["reserve"], 18 * GB)
        self.assertIsNone(restarted.data["cap"])
        self.client.post("/api/limits", json={"reserveGb": 20})
        self.assertIsNone(self.worker.state["cap"])

    def test_zero_cap_disables_only_size_limit_and_persists(self):
        self.client.post("/api/limits", json={"episodeCountLimit": 12, "capGb": 80, "reserveGb": 18})
        response = self.client.post("/api/limits", json={"capGb": 0})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.client.get("/api/status").json()["capGb"])
        restored = Store(str(self.state_file))
        self.assertIsNone(restored.data["cap"])
        self.assertEqual(restored.data["count"], 12)
        self.assertEqual(restored.data["reserve"], 18 * GB)

    def test_invalid_limit_patch_is_atomic(self):
        before = copy.deepcopy(self.worker.state)
        disk = self.state_file.read_bytes() if self.state_file.exists() else None
        r = self.client.post("/api/limits", json={"episodeCountLimit": 12, "reserveGb": -1})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.worker.state, before)
        self.assertEqual(self.state_file.read_bytes() if self.state_file.exists() else None, disk)

    def test_jellyfin_metadata_matches_file_and_uses_series_parent(self):
        for kind in ["movie", "series"]:
            with self.subTest(kind=kind):
                season = {"kind": kind, "episodes": [{"index": 0, "published": True}]}
                self.fake_j.http = MagicMock()
                item = {"Id": "item123", "SeriesId": "series123", "Name": "Игра", "OriginalTitle": "The Game", "ProductionYear": 1997,
                        "Path": "/managed/file.mkv", "ProviderIds": {"Tmdb": "2649", "Imdb": "tt0119174"}}
                responses = [{"Items": [{"Id": "wrong", "Name": "Игра", "Path": "/other/file.mkv"}, item], "TotalRecordCount": 2}]
                if kind == "series":
                    responses.append({"Items": [{"Id": "series123", "ProviderIds": {"Tmdb": "42", "Imdb": "tt1234567"}}]})
                responses.append({"Id": "server123"})
                self.fake_j.http.call.side_effect = responses
                with patch.object(self.worker, "season_by_id", return_value=season), patch.object(self.worker, "dest", return_value=Path("/managed/file.mkv")):
                    response = self.client.get("/api/jellyfin/link", params={"pack": H1})
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(data["tmdbId"], "2649" if kind == "movie" else "42")
                self.assertEqual(data["imdbId"], "tt0119174" if kind == "movie" else "tt1234567")
                self.assertEqual(data["itemId"], "item123" if kind == "movie" else "series123")
                self.assertNotIn("Path", data)

    def test_pack_crud(self):
        # 1. Add pack
        add_payload = {
            "title": "House of the Dragon",
            "magnet": MAGNET1,
            "kind": "series",
            "season": 2,
            "startEpisode": 1,
        }
        r = self.client.post("/api/packs/add", json=add_payload)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.worker.state["seasons"]), 1)

        # 2. List packs
        r = self.client.get("/api/packs")
        self.assertEqual(r.status_code, 200)
        packs = r.json()
        self.assertEqual(len(packs), 1)
        self.assertEqual(packs[0]["hash"], H1)
        self.assertEqual(packs[0]["kind"], "SERIES")
        self.assertEqual(packs[0]["season"], 2)

        # 3. Get pack
        r = self.client.get(f"/api/packs/{H1}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["hash"], H1)

        # 4. Finish pack
        r = self.client.post(f"/api/packs/{H1}/finish")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.worker.state["seasons"][0]["no_next"])

        # 5. Retry pack
        r = self.client.post(f"/api/packs/{H1}/retry")
        self.assertEqual(r.status_code, 200)

        # 6. Delete pack
        r = self.client.delete(f"/api/packs/{H1}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.worker.state["seasons"]), 0)

    def test_set_pack_start_and_delete_episode(self):
        # 1. Add pack
        add_payload = {
            "title": "House of the Dragon",
            "magnet": MAGNET1,
            "kind": "series",
            "season": 2,
            "startEpisode": 1,
        }
        r = self.client.post("/api/packs/add", json=add_payload)
        self.assertEqual(r.status_code, 200)
        pack = self.worker.state["seasons"][0]

        # Populate dummy episodes
        pack["episodes"] = [
            {"index": 0, "season": 2, "number": 1, "size": 100, "name": "Show.S02E01.mkv", "watched": False, "progress": 1.0, "published": True, "selected": True, "removed": False},
            {"index": 1, "season": 2, "number": 2, "size": 100, "name": "Show.S02E02.mkv", "watched": False, "progress": 1.0, "published": True, "selected": True, "removed": False},
            {"index": 2, "season": 2, "number": 3, "size": 100, "name": "Show.S02E03.mkv", "watched": False, "progress": 0.0, "published": False, "selected": True, "removed": False},
        ]
        self.fake_q.infos[H1] = {"state": "downloading", "progress": 0.66}

        # Change start episode to 2 (skips episode index 0)
        r = self.client.post(f"/api/packs/{H1}/from", json={"season": 2, "startEpisode": 2})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(pack["episodes"][0].get("discarded"))

        # Test deleting an episode (index 1)
        r = self.client.delete(f"/api/packs/{H1}/episodes/1")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(pack["episodes"][1].get("removed"))
        self.assertFalse(pack["episodes"][1].get("selected"))
        self.assertFalse(pack["episodes"][1].get("published"))

        # Test deleting non-existent episode index
        r = self.client.delete(f"/api/packs/{H1}/episodes/99")
        self.assertEqual(r.status_code, 404)

    def test_progressive_search_serializes_partial_results_and_enforces_auth(self):
        identifier = 'a' * 32
        snapshot = {'id':identifier,'kind':'series','done':False,'changed':True,'revision':1,
                    'completed':1,'total':2,'error':'','warnings':[],
                    'results':[{'title':'Show S02E01 1080p','tracker':'Fast','size':100,'seeders':5,
                                'magnet':MAGNET1,'link':'','quality':'1080p'}]}
        with patch.object(server.release_searches,'start',return_value=identifier), patch.object(server.release_searches,'snapshot',return_value=snapshot):
            response = self.client.post('/api/search/start',json={'query':'Show','kind':'series'})
            self.assertEqual(response.status_code,200)
            self.assertFalse(response.json()['done'])
            self.assertEqual(response.json()['results'][0]['detectedSeason'],2)
            self.assertEqual(response.json()['results'][0]['detectedKind'],'SERIES')
        with patch.object(server.release_searches,'snapshot',return_value={'id':identifier,'kind':'series','done':False,'changed':False,'revision':1,'completed':1,'total':2,'error':''}) as get:
            response = self.client.get('/api/search/jobs/'+identifier+'?after=1')
            self.assertNotIn('results',response.json())
            get.assert_called_once_with(identifier,1)
        with patch.object(server,'API_TOKEN','fixture-token'):
            self.assertEqual(self.client.post('/api/search/start',json={'query':'Show'}).status_code,401)
            self.assertEqual(self.client.get('/api/search/jobs/'+identifier).status_code,401)

    def test_search(self):
        r = self.client.get("/api/search?query=Dragon&kind=series&minSeeders=50")
        self.assertEqual(r.status_code, 200)
        results = r.json()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["tracker"], "RuTor")
        self.assertEqual(results[0]["seeders"], 120)
        self.assertIn("LostFilm", results[0]["voices"])

    def test_subscriptions_crud(self):
        # Add subscription
        r = self.client.post("/api/subscriptions", json={
            "title": "Severance",
            "preferredQuality": "1080p",
            "preferredVoice": "LostFilm"
        })
        self.assertEqual(r.status_code, 200)
        sub_id = r.json()["id"]

        # List subscriptions
        r = self.client.get("/api/subscriptions")
        self.assertEqual(r.status_code, 200)
        subs = r.json()
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["id"], sub_id)
        self.assertEqual(subs[0]["preferredQuality"], "1080p")
        self.assertEqual(subs[0]["preferredVoice"], "LostFilm")

        # Delete subscription
        r = self.client.delete(f"/api/subscriptions/{sub_id}")
        self.assertEqual(r.status_code, 200)

        r = self.client.get("/api/subscriptions")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()), 0)

    def test_auth_token(self):
        server.API_TOKEN = "secret123"

        # Missing token -> 401
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 401)

        # Wrong token -> 401
        r = self.client.get("/api/status", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

        # Correct token -> 200
        r = self.client.get("/api/status", headers={"Authorization": "Bearer secret123"})
        self.assertEqual(r.status_code, 200)


if __name__ == '__main__':
    unittest.main()
