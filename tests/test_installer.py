import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

import installer as i
from net import APIError


class Installer(unittest.TestCase):
    def test_env_quotes_dollar_and_hash_roundtrip_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'.env'
            source={'PASSWORD':"test'$value#with spaces",'DATA_DIR':'/tmp/test data','PORT':'18080'}
            i.save_env(path,source)
            self.assertEqual(i.read_env(path),source)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            self.assertIn("PASSWORD='",path.read_text())

    def test_env_rejects_newline_or_invalid_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            for source in ({'TOKEN':'first\nsecond'},{'BAD KEY':'a'}):
                with self.assertRaises(i.InstallError):i.save_env(Path(tmp)/'.env',source)

    def test_private_write_does_not_follow_a_predictable_temporary_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);outside=root/'unrelated';outside.write_text('keep')
            (root/'.env.install.tmp').symlink_to(outside)
            i.private_write(root/'.env','synthetic')
            self.assertEqual(outside.read_text(),'keep')
            self.assertEqual((root/'.env').read_text(),'synthetic')

    def test_initial_state_does_not_overwrite_existing_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings={'count':3,'gb':12.5,'reserve':4}
            i.initial_state(tmp,settings);path=Path(tmp)/'state/state.json';before=path.read_text()
            i.initial_state(tmp,{'count':99,'gb':1,'reserve':0})
            self.assertEqual(path.read_text(),before)
            value=json.loads(before);self.assertEqual(value['count'],3);self.assertEqual(value['cap'],12_500_000_000)

    def test_folders_are_on_one_filesystem_and_private_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            env={'DATA_DIR':str(Path(tmp)/'video')};i.folders(tmp,env)
            for name in ('downloads','library','movies'):self.assertTrue((Path(env['DATA_DIR'])/name).is_dir())
            self.assertEqual(stat.S_IMODE((Path(tmp)/'config').stat().st_mode),0o700)

    def test_dns_subnet_avoids_existing_docker_network(self):
        class Docker:
            def run(self,*args,**kw):
                if args[:2]==('network','ls'):return '{"ID":"one"}\n'
                return json.dumps([{'IPAM':{'Config':[{'Subnet':'172.30.53.0/24'}]}}])
        self.assertEqual(i.select_dns(Docker()),('172.30.54.0/24','172.30.54.2'))

    def test_duplicate_ports_are_rejected(self):
        with self.assertRaises(i.InstallError):i.check_ports('127.0.0.1',['18080','18080','18096','16881'])
        for value in ('0','-1','65536'):
            with self.assertRaises(i.InstallError):i.require_port(value)

    def test_invalid_existing_qbit_password_is_not_reset(self):
        class Client:
            def call(self,*a,**kw):raise APIError('HTTP 403')
        class Docker:
            def compose(self,*a,**kw):return 'Existing service, no temporary password'
        env={'BIND_IP':'127.0.0.1','QBIT_PORT':'18080','QBIT_USERNAME':'admin','QBIT_PASSWORD':'synthetic'}
        with patch.object(i,'wait_http'),patch.object(i,'HTTP',return_value=Client()),self.assertRaises(i.InstallError):
            i.setup_qbit(Docker(),env)

    def test_existing_qbit_success_does_not_change_password(self):
        class Client:
            def __init__(self):self.paths=[]
            def call(self,path,**kw):self.paths.append(path);return 'v5.2.4'
        client=Client();env={'BIND_IP':'127.0.0.1','QBIT_USERNAME':'admin','QBIT_PASSWORD':'synthetic'}
        with patch.object(i,'wait_http'),patch.object(i,'HTTP',return_value=client):i.setup_qbit(None,env)
        self.assertNotIn('/api/v2/app/setPreferences',client.paths)

    def test_auth_required_http_response_counts_as_ready(self):
        with patch.object(i,'build_opener') as build:
            build.return_value.open.side_effect=HTTPError('http://localhost',401,'auth',{},None)
            i.wait_http('http://localhost',timeout=1)

    def test_readiness_preserves_cookie_on_redirect(self):
        import threading
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path=='/':
                    self.send_response(302);self.send_header('Set-Cookie','session=synthetic; Path=/');self.send_header('Location','/dashboard');self.end_headers()
                else:
                    self.send_response(200 if self.headers.get('Cookie')=='session=synthetic' else 400);self.end_headers()
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:i.wait_http('http://127.0.0.1:'+str(server.server_port),timeout=2)
        finally:server.shutdown();server.server_close();thread.join()

    def test_jellyfin_retries_read_only_startup_503(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self):return b'{"StartupWizardCompleted":true}'
        with patch.object(i,'urlopen',side_effect=[HTTPError('http://localhost',503,'starting',{},None),Response()]),patch.object(i.time,'sleep'):
            result=i.JellyfinSetup('http://localhost').call('/System/Info/Public')
        self.assertTrue(result['StartupWizardCompleted'])

    def test_completed_install_is_information_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'state').mkdir();(root/'state/installation-complete.json').write_text('{}')
            i.save_env(root/'.env',{'COMPOSE_PROJECT_NAME':'watchnext'})
            old=Path.cwd()
            try:
                with patch.object(i,'ROOT',root),patch.object(i,'Docker') as docker,patch('os.getuid',return_value=1000),patch('sys.argv',['installer.py']):i.main()
                docker.return_value.compose.assert_not_called()
            finally:os.chdir(old)


if __name__=='__main__':unittest.main()
