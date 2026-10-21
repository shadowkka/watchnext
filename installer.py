#!/usr/bin/env python3
"""Interactive, resumable installer. Only Python's standard library is needed."""
import argparse
import http.cookiejar
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen, build_opener, HTTPCookieProcessor

from net import APIError, HTTP

ROOT = Path(__file__).resolve().parent


class InstallError(RuntimeError):
    pass


def private_write(path, text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    temporary=Path(name)
    with os.fdopen(fd,'w') as f:
        f.write(text);f.flush();os.fsync(f.fileno())
    os.replace(temporary,path);path.chmod(0o600)


def read_env(path):
    values={}
    if not Path(path).exists():return values
    for line in Path(path).read_text().splitlines():
        if not line or line.lstrip().startswith('#') or '=' not in line:continue
        key,value=line.split('=',1);value=value.strip()
        if len(value)>=2 and value[0]==value[-1]=="'":value=value[1:-1].replace("\\'","'")
        elif len(value)>=2 and value[0]==value[-1]=='"':value=json.loads(value)
        values[key.strip()]=value
    return values


def save_env(path, values):
    lines=[]
    for key,value in values.items():
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*',key):raise InstallError('Неверное имя настройки.')
        value=str(value)
        if '\n' in value or '\r' in value or '\x00' in value:raise InstallError('Перенос строки в настройке недопустим.')
        lines.append(key+"='"+value.replace("'","\\'")+"'")
    private_write(path,'\n'.join(lines)+'\n')


def ask(prompt,default=''):
    answer=input(prompt+(f' [{default}]' if default else '')+': ').strip()
    return answer or str(default)


def password(prompt,generated):
    answer=getpass.getpass(prompt+' (Enter — сгенерировать и сохранить локально): ')
    if any(c in answer for c in '\n\r\x00'):raise InstallError('Пароль должен быть одной строкой.')
    return answer or generated


def guessed_ip():
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    try:s.connect(('192.0.2.1',9));return s.getsockname()[0]
    except OSError:return '127.0.0.1'
    finally:s.close()


def require_port(value):
    port=int(value)
    if not 1024<=port<=65535:raise InstallError('Порт должен быть от 1024 до 65535.')
    return str(port)


def check_ports(address,ports):
    if len(set(ports))!=len(ports):raise InstallError('Выбери разные внешние порты сервисов.')
    for port in ports:
        for kind in ([socket.SOCK_STREAM,socket.SOCK_DGRAM] if port==ports[-1] else [socket.SOCK_STREAM]):
            with socket.socket(socket.AF_INET,kind) as s:
                try:s.bind(('0.0.0.0' if port==ports[-1] else address,int(port)))
                except OSError:raise InstallError(f'Порт {port} занят или IP не принадлежит этому серверу. Выбери другой порт/IP.') from None


class Docker:
    def __init__(self,root=ROOT):
        self.root=Path(root);self.prefix=['docker']
        if not shutil.which('docker'):raise InstallError('Docker не установлен. Запусти bash scripts/install-docker-ubuntu.sh, затем повтори ./install.sh.')
        if subprocess.run(['docker','info'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
            if shutil.which('sudo') and subprocess.run(['sudo','docker','info'],stdout=subprocess.DEVNULL).returncode==0:self.prefix=['sudo','docker']
            else:raise InstallError('Docker недоступен. Запусти его и проверь: docker info (или sudo docker info).')
        self.run('compose','version',capture=True)
    def run(self,*args,capture=False):
        result=subprocess.run(self.prefix+list(args),cwd=self.root,text=True,stdout=subprocess.PIPE if capture else None,stderr=subprocess.PIPE if capture else None)
        if result.returncode:raise InstallError('Не удалось выполнить docker '+str(args[0])+'. Проверь Docker и доступ к образам; секреты из вывода скрыты.')
        return result.stdout if capture else ''
    def compose(self,*args,capture=False):return self.run('compose',*args,capture=capture)


def select_dns(docker):
    nets=[json.loads(line) for line in docker.run('network','ls','--format','json',capture=True).splitlines() if line.strip()]
    ids=[n['ID'] for n in nets]
    used=[]
    if ids:
        for network in json.loads(docker.run('network','inspect',*ids,capture=True)):
            for config in network.get('IPAM',{}).get('Config',[]) or []:
                if config.get('Subnet'):used.append(ipaddress.ip_network(config['Subnet'],strict=False))
    for n in range(53,254):
        net=ipaddress.ip_network(f'172.30.{n}.0/24')
        if not any(net.version==x.version and net.overlaps(x) for x in used):return str(net),str(net.network_address+2)
    raise InstallError('Не удалось подобрать свободную сеть DNS. Укажи SEARCH_DNS_SUBNET/IP в .env вручную.')


def endpoint(env,key,default):return 'http://'+env['BIND_IP']+':'+env.get(key,str(default))


def wait_http(base,path='/',timeout=120):
    deadline=time.monotonic()+timeout
    opener=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    while time.monotonic()<deadline:
        try:
            with opener.open(base+path,timeout=3) as r:
                if r.status<400:
                    r.read(1024*1024)
                    return
        except HTTPError as exc:
            exc.close()
            if exc.code in (401,403):return  # service is ready; its API will authenticate next
        except OSError:pass
        time.sleep(2)
    raise InstallError('Сервис не запустился вовремя: '+base+'. Исправь причину и повтори ./install.sh — настройки сохранятся.')


def setup_qbit(docker,env):
    base=endpoint(env,'QBIT_PORT',8080);wait_http(base)
    client=HTTP(base,{'Referer':base+'/'})
    def login(pw):
        try:
            client.call('/api/v2/auth/login',data={'username':env['QBIT_USERNAME'],'password':pw},raw=True)
            client.call('/api/v2/app/version',raw=True);return True
        except APIError:return False
    if not login(env['QBIT_PASSWORD']):
        logs=docker.compose('logs','--no-color','--tail=150','qbittorrent',capture=True)
        match=re.search(r'temporary password[^\n]*?:\s*(\S+)',logs,re.I)
        if not match or not login(match[1]):raise InstallError('Не удалось войти в qBittorrent. Чужие пароли не сбрасываются; проверь QBIT_PASSWORD в .env.')
        client.call('/api/v2/app/setPreferences',data={'json':json.dumps({'web_ui_password':env['QBIT_PASSWORD']})},raw=True)
        if not login(env['QBIT_PASSWORD']):raise InstallError('qBittorrent не принял сохранённый пароль.')
    print('✓ qBittorrent настроен.')


class JellyfinSetup:
    def __init__(self,base):
        self.base=base
        self.headers={'Accept':'application/json; profile=PascalCase'}
    def call(self,path,body=None,query=None):
        url=self.base+path+('?' + urlencode(query,doseq=True) if query else '')
        headers=dict(self.headers);data=None
        if body is not None:headers['Content-Type']='application/json';data=json.dumps(body).encode()
        for attempt in range(10):
            try:
                with urlopen(Request(url,data=data,headers=headers),timeout=30) as r:
                    raw=r.read();return json.loads(raw) if raw else None
            except HTTPError as exc:
                if body is None and exc.code in (502,503,504) and attempt<9:
                    exc.close();time.sleep(2);continue
                raise InstallError(f'Jellyfin: HTTP {exc.code} при настройке {path}. Проверь настройки и повтори установщик.') from None
            except (OSError,ValueError) as exc:
                if body is None and attempt<9:
                    time.sleep(2)
                    continue
                raise InstallError('Jellyfin: '+type(exc).__name__+' при настройке '+path+'. Повтори установщик; сохранённые пароли не изменятся.') from None



def setup_jellyfin(env):
    base=endpoint(env,'JELLYFIN_PORT',8096);wait_http(base,'/System/Info/Public')
    j=JellyfinSetup(base);info=j.call('/System/Info/Public')
    if not info['StartupWizardCompleted']:
        j.call('/Startup/Configuration',{'ServerName':'WatchNext','UICulture':'ru','MetadataCountryCode':'RU','PreferredMetadataLanguage':'ru'})
        j.call('/Startup/User')
        j.call('/Startup/User',{'Name':env['JELLYFIN_USERNAME'],'Password':env['JELLYFIN_SETUP_PASSWORD']})
        j.call('/Startup/RemoteAccess',{'EnableRemoteAccess':True})
        j.call('/Startup/Complete',{})
    j.headers['Authorization']='MediaBrowser Client="WatchNext", Device="Installer", DeviceId="watchnext-installer", Version="1.0"'
    auth=j.call('/Users/AuthenticateByName',{'Username':env['JELLYFIN_USERNAME'],'Pw':env['JELLYFIN_SETUP_PASSWORD']})
    j.headers={'Authorization':'MediaBrowser Token="'+auth['AccessToken']+'"','Accept':'application/json; profile=PascalCase'}
    libraries=j.call('/Library/VirtualFolders')
    for name,kind,path in [('Сериалы','tvshows','/data/library'),('Фильмы','movies','/data/movies')]:
        if not any(path in v.get('Locations',[]) for v in libraries):
            j.call('/Library/VirtualFolders',{'LibraryOptions':{'EnableRealtimeMonitor':True,'EnableChapterImageExtraction':False,
                    'EnableAutomaticSeriesGrouping':False,'EnableInternetProviders':True,'PreferredMetadataLanguage':'ru','MetadataCountryCode':'RU'}},
                   {'name':name,'collectionType':kind,'paths':path,'refreshLibrary':'true'})
    keys=j.call('/Auth/Keys')['Items'];ours=[k for k in keys if k.get('AppName')=='WatchNext']
    if not ours:j.call('/Auth/Keys',{}, {'app':'WatchNext'});ours=[k for k in j.call('/Auth/Keys')['Items'] if k.get('AppName')=='WatchNext']
    if len(ours)!=1:raise InstallError('Не найден единственный API-ключ WatchNext в Jellyfin.')
    env['JELLYFIN_API_KEY']=ours[0]['AccessToken']
    print('✓ Jellyfin, общий профиль и две библиотеки настроены.')


def setup_jackett(root,env):
    base=endpoint(env,'JACKETT_PORT',9117);wait_http(base,'/UI/Dashboard')
    config=Path(root)/'config/jackett/Jackett/ServerConfig.json'
    deadline=time.monotonic()+30
    while not config.exists() and time.monotonic()<deadline:time.sleep(1)
    if not config.exists():raise InstallError('Jackett не создал конфигурацию.')
    env['JACKETT_API_KEY']=json.loads(config.read_text())['APIKey']
    a=HTTP(base);a.call('/UI/Dashboard',raw=True)
    settings=a.call('/api/v2.0/server/config')
    settings.update(flaresolverrurl='http://flaresolverr:8191',flaresolverr_maxtimeout=55000)
    with a.opener.open(Request(base+'/api/v2.0/server/config',data=json.dumps(settings).encode(),headers={'Content-Type':'application/json'}),timeout=30):pass
    # The configuration endpoint restarts Jackett's web service briefly.
    wait_http(base,'/UI/Dashboard');a.call('/UI/Dashboard',raw=True)
    try:
        cfg=a.call('/api/v2.0/indexers/rutor/config',timeout=60)
        for item in cfg:
            if item['id']=='striprussianletters':item['value']=False
            if item['id']=='sortrequestedfromsite(appliesonlytosearchwithkeywords)':item['value']='2'
        with a.opener.open(Request(base+'/api/v2.0/indexers/rutor/config',data=json.dumps(cfg).encode(),headers={'Content-Type':'application/json'}),timeout=90):pass
        print('✓ Jackett, FlareSolverr и RuTor настроены.')
    except (APIError,OSError):print('Jackett подключён. RuTor не ответил — добавь/проверь его в панели Jackett позже.')


def setup_telegram(env):
    if env.get('TELEGRAM_BOT_TOKEN') not in (None,'','replace-me') and env.get('TELEGRAM_OWNER_ID') not in (None,'','123456789'):return
    token=getpass.getpass('Токен отдельного бота от @BotFather (ввод скрыт): ').strip()
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+',token):raise InstallError('Некорректный формат токена Telegram.')
    api=HTTP('https://api.telegram.org/bot'+token)
    info=api.call('/getMe')
    if not info.get('ok'):raise InstallError('Telegram отклонил токен.')
    webhook=api.call('/getWebhookInfo')
    if webhook.get('result',{}).get('url'):raise InstallError('У этого бота есть webhook. Создай отдельного бота в @BotFather.')
    env['TELEGRAM_BOT_TOKEN']=token
    print('Бот: @'+info['result']['username'])
    while True:
        input('Открой бота, отправь /start, затем нажми Enter здесь. ')
        result=api.call('/getUpdates',data={'timeout':10},timeout=20)
        if not result.get('ok'):raise InstallError('Не удалось получить сообщения. Этот токен не должен использоваться другим приложением.')
        users={}
        for update in result['result']:
            msg=update.get('message',{});u=msg.get('from',{})
            if msg.get('chat',{}).get('type')=='private' and not u.get('is_bot') and u.get('id',0)>0:
                users[u['id']]=u.get('username') or u.get('first_name') or str(u['id'])
        if users:break
        print('Сообщения пока не найдены. Повтори /start.')
    entries=list(users.items())
    for i,(_,name) in enumerate(entries,1):print(f'{i}. {name}')
    selected=int(ask('Выбери свой аккаунт по номеру',1))-1
    if not 0<=selected<len(entries):raise InstallError('Неверный номер аккаунта.')
    env['TELEGRAM_OWNER_ID']=str(entries[selected][0])


def folders(root,env):
    for name in ('config/jackett','config/qbittorrent','config/jellyfin','cache/jellyfin','state'):(Path(root)/name).mkdir(parents=True,exist_ok=True)
    for name in ('config','state'):(Path(root)/name).chmod(0o700)
    data=Path(env['DATA_DIR']).expanduser().resolve()
    for name in ('downloads','library','movies'):(data/name).mkdir(parents=True,exist_ok=True)
    env['DATA_DIR']=str(data)
    if len({(data/n).stat().st_dev for n in ('downloads','library','movies')})!=1:raise InstallError('downloads, library и movies должны быть на одной файловой системе.')


def initial_state(root,settings):
    from app import Store
    path=Path(root)/'state/state.json'
    if path.exists():return
    s=Store(path);s.data.update(count=settings['count'],cap=int(settings['gb']*1e9),reserve=int(settings['reserve']*1e9));s.save()


def main():
    parser=argparse.ArgumentParser(description='WatchNext: пошаговая установка на Ubuntu с Docker Compose')
    parser.add_argument('--check',action='store_true',help='только проверить Docker, без запуска сервисов')
    args=parser.parse_args()
    os.chdir(ROOT)
    if os.getuid()==0:raise InstallError('Запусти установщик обычным пользователем, без sudo. Docker при необходимости будет вызван через sudo.')
    if not shutil.which('docker') and not args.check:
        if ask('Docker не найден. Установить из официального apt-репозитория? y/N','N').casefold()=='y':
            subprocess.run(['bash',str(ROOT/'scripts/install-docker-ubuntu.sh'),'--yes'],check=True)
    docker=Docker(ROOT)
    if args.check:
        print('✓ Docker и Compose доступны. Сервисы не изменялись.');return
    path=ROOT/'.env';values=read_env(path)
    if (ROOT/'state/installation-complete.json').exists():
        print('WatchNext уже установлен. Очередь и настройки не изменялись.\nДля запуска: docker compose up -d. Обновление описано в docs/OPERATIONS.md.');return
    if values.get('COMPOSE_PROJECT_NAME') and values['COMPOSE_PROJECT_NAME']!='watchnext':
        raise InstallError('Этот каталог использует другой Docker-проект. Для новой установки распакуй WatchNext в отдельную папку.')
    if not values:
        print('WatchNext — установка домашнего кинотеатра и Telegram-бота.\nНужен отдельный бот от @BotFather. Настройка сохраняется, её можно продолжить после ошибки.')
        values=read_env(ROOT/'.env.example')
        address=ask('IP мини-ПК в домашней сети',guessed_ip());ipaddress.IPv4Address(address)
        if address=='0.0.0.0':raise InstallError('Укажи адрес домашнего интерфейса, не 0.0.0.0.')
        values.update(BIND_IP=address,PUID=str(os.getuid()),PGID=str(os.getgid()))
        values['DATA_DIR']=ask('Папка для видео',str(ROOT/'data'))
        for key,label,default in [('QBIT_PORT','qBittorrent',8080),('JELLYFIN_PORT','Jellyfin',8096),('JACKETT_PORT','Jackett',9117),('TORRENT_PORT','BitTorrent',6881)]:
            values[key]=require_port(ask('Порт '+label,default))
        check_ports(address,[values[k] for k in ('QBIT_PORT','JELLYFIN_PORT','JACKETT_PORT','TORRENT_PORT')])
        values['JELLYFIN_USERNAME']=ask('Общий профиль Jellyfin','viewer')
        values['JELLYFIN_SETUP_PASSWORD']=password('Пароль Jellyfin для телевизора',''.join(secrets.choice('0123456789') for _ in range(8)))
        values['QBIT_PASSWORD']=password('Пароль администратора qBittorrent',secrets.token_urlsafe(18))
        values['SEARCH_DNS_SUBNET'],values['SEARCH_DNS_IP']=select_dns(docker)
        save_env(path,values)
    else:print('Продолжаю установку. Существующие пароли, библиотеки и очередь сохраняются.')
    if not values.get('JELLYFIN_SETUP_PASSWORD'):raise InstallError('Для продолжения укажи JELLYFIN_SETUP_PASSWORD в .env. Чужая установка не изменена.')
    folders(ROOT,values)
    setup_telegram(values);save_env(path,values)
    # No polling worker may compete with the setup conversation.
    docker.compose('stop','queue')
    print('Запускаю сервисы. Первая загрузка образов может занять несколько минут.')
    docker.compose('up','-d','qbittorrent','jellyfin','dnsproxy','flaresolverr','jackett')
    setup_qbit(docker,values);setup_jellyfin(values);save_env(path,values)
    try:setup_jackett(ROOT,values)
    except (InstallError,APIError,OSError):print('Поиск пока не настроен. Проверь панель Jackett; добавление по magnet будет доступно.')
    save_env(path,values)
    try:
        old=dict(os.environ);os.environ.update(values)
        from setup_metadata import main as metadata
        metadata()
    except (OSError,KeyError,AssertionError,APIError):print('Настройку обложек можно повторить: python3 setup_metadata.py')
    finally:os.environ.clear();os.environ.update(old)
    settings={'count':int(ask('Сколько серий держать одновременно',5)),'gb':float(ask('Общий лимит серий и фильмов в ГБ',15).replace(',','.')),'reserve':float(ask('Оставлять свободными ГБ',5).replace(',','.'))}
    if settings['count']<1 or settings['gb']<=0 or settings['reserve']<0:raise InstallError('Лимиты должны быть положительными, резерв — неотрицательным.')
    initial_state(ROOT,settings)
    access='Jellyfin: '+endpoint(values,'JELLYFIN_PORT',8096)+'\nЛогин: '+values['JELLYFIN_USERNAME']+'\nПароль: '+values['JELLYFIN_SETUP_PASSWORD']+'\n\nqBittorrent: '+endpoint(values,'QBIT_PORT',8080)+'\nЛогин: '+values['QBIT_USERNAME']+'\nПароль: '+values['QBIT_PASSWORD']+'\n'
    private_write(ROOT/'access.txt',access)
    docker.compose('up','-d','--build','queue')
    from ui import COMMANDS
    t=HTTP('https://api.telegram.org/bot'+values['TELEGRAM_BOT_TOKEN'])
    t.call('/setMyCommands',data={'commands':json.dumps([{'command':c,'description':d} for c,d in COMMANDS],ensure_ascii=False)})
    private_write(ROOT/'state/installation-complete.json',json.dumps({'version':1,'completed':int(time.time())}))
    print('\nГотово! Отправь боту /menu.\nJellyfin: '+endpoint(values,'JELLYFIN_PORT',8096)+'\nJackett: '+endpoint(values,'JACKETT_PORT',9117)+'/UI/Dashboard\nПароли: '+str(ROOT/'access.txt')+' (доступ только владельцу).\nRuTracker добавляется в панели Jackett; пароль от трекера вводится только там.\nСемейный доступ: /invite. Подписки: /subscriptions.')


if __name__=='__main__':
    try:main()
    except (InstallError,APIError,ValueError,KeyboardInterrupt,EOFError) as e:
        print('\nУстановка остановлена: '+(str(e) if isinstance(e,(InstallError,APIError)) else 'проверь введённые значения и повтори ./install.sh.'),file=sys.stderr)
        sys.exit(1)
