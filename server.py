"""WatchNext standalone REST API server with optional Telegram bot support.

Exposes REST API endpoints for the Android client and manages the background
media queue worker, qBittorrent, Jellyfin, Jackett and TVmaze subscriptions.
"""
import asyncio
from contextlib import asynccontextmanager
import html
import json
import logging
import os
import shutil
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Header, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

from app import Store, Worker
from core import GB, episode_count, episode_label, finished, magnet_hash, slug
from net import APIError, Jellyfin, Qbit, Telegram, HTTP
from integrations import load_settings, save_settings, telegram_config, public_settings
import re
from search_client import Jackett, plain, quality, release_numbers, voices
from subscriptions import TVmaze
from release_search import SearchJobs
release_searches = SearchJobs()

LOG = logging.getLogger("watchnext-server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

worker_lock = threading.Lock()
integration_update_lock = threading.Lock()
stop_event = threading.Event()

API_TOKEN = os.environ.get("API_TOKEN", "").strip()


class DummyTelegram:
    """Safe fallback when Telegram credentials are not configured."""
    owner = 0
    bot_username = "WatchNextBot"

    def send(self, text, chat_id=None, reply_markup=None):
        return 0

    def edit(self, text, chat_id, message_id, reply_markup):
        return {}

    def answer(self, query_id, text="", alert=False):
        return True

    def updates(self, offset):
        return []


def create_worker(data_root: Optional[str] = None) -> Worker:
    state_file = os.environ.get("STATE_FILE", "/state/state.json")
    store = Store(state_file)
    qbit = Qbit()
    jellyfin = Jellyfin()
    token, owner, enabled = telegram_config()
    if enabled and token and owner:
        try:
            tg = Telegram(token=token, owner=owner)
        except Exception as exc:
            LOG.warning("Не удалось инициализировать Telegram (%s); запуск в режиме только REST API.", exc)
            tg = DummyTelegram()
    else:
        LOG.info("Telegram не настроен; запуск в режиме автономного REST API.")
        tg = DummyTelegram()

    root = data_root or os.environ.get("DATA_ROOT") or "/data"
    return Worker(store, qbit, jellyfin, tg, data_root=root)


global_worker: Optional[Worker] = None


def get_worker() -> Worker:
    global global_worker
    if global_worker is None:
        global_worker = create_worker()
    return global_worker


# -----------------------------------------------------------------------------
# Security & Authentication
# -----------------------------------------------------------------------------
def verify_api_token(authorization: Optional[str] = Header(None)) -> bool:
    if not API_TOKEN:
        return True
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется заголовок Authorization: Bearer <token>",
        )
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный токен авторизации",
        )
    return True


# -----------------------------------------------------------------------------
# Pydantic Models for Android Client Compatibility
# -----------------------------------------------------------------------------
class QueueStatusResponse(BaseModel):
    episodeCountLimit: Optional[int] = 5
    capGb: Optional[float] = None
    reserveGb: float = 5.0
    isPaused: bool = False
    diskTotalGb: float = 0.0
    diskFreeGb: float = 0.0
    diskUsedGb: float = 0.0
    activeDownloadsCount: int = 0
    totalPacksCount: int = 0
    serviceAlert: Optional[str] = None
    limitAlert: Optional[str] = None


class EpisodeItemResponse(BaseModel):
    index: int
    name: str
    season: int
    episode: int
    label: str
    size: int
    progress: float
    status: str  # COMPLETED, DOWNLOADING, WAITING_LIMIT, PLAYED, SKIPPED
    isPublished: bool = False


class PackItemResponse(BaseModel):
    hash: str
    title: str
    kind: str  # SERIES or MOVIE
    season: int = 1
    startEpisode: int = 1
    state: str  # METADATA, READY, DOWNLOADING, CHECKING, STOPPED, COMPLETE
    progress: float = 0.0
    totalSize: int = 0
    downloadedSize: int = 0
    downloadSpeed: int = 0
    readyEpisodes: int = 0
    totalEpisodes: int = 0
    noNextSeason: bool = False
    episodes: List[EpisodeItemResponse] = []


class TorrentSearchResultResponse(BaseModel):
    title: str
    tracker: str
    size: int
    seeders: int
    peers: Optional[int] = None
    quality: str
    voices: List[str]
    magnet: str
    link: str = ""
    detectedKind: str = "SERIES"
    detectedSeason: int = 1
    detectedStartEpisode: int = 1


class SubscriptionItemResponse(BaseModel):
    id: str
    showTitle: str
    preferredQuality: str = "1080p"
    preferredVoice: str = "Любая"
    status: str = "ACTIVE"  # ACTIVE, EPISODE_OUT, RELEASE_FOUND, UP_TO_DATE
    lastSeason: int = 1
    lastEpisode: int = 1
    nextEpisodeDate: Optional[str] = None
    nextEpisodeLabel: Optional[str] = None
    candidateReleaseTitle: Optional[str] = None
    candidateReleaseMagnet: Optional[str] = None


class AddTorrentRequest(BaseModel):
    magnet: str
    title: Optional[str] = ""
    kind: str = "series"  # series or movie
    season: int = 1
    startEpisode: int = 1
    autoSeason: bool = False


class SetStartRequest(BaseModel):
    season: int
    startEpisode: int


class UpdateLimitsRequest(BaseModel):
    episodeCountLimit: Optional[int] = None
    capGb: Optional[float] = None
    reserveGb: Optional[float] = None


class AddSubscriptionRequest(BaseModel):
    title: str
    preferredQuality: str = "1080p"
    preferredVoice: str = "Любая"
    tvmazeId: Optional[int] = None


class ShowSearchResultDto(BaseModel):
    id: int
    name: str
    premiered: Optional[str] = ""
    status: Optional[str] = ""
    language: Optional[str] = ""


class ResolveTorrentRequest(BaseModel):
    link: str


# -----------------------------------------------------------------------------
# Background Synchronization Loop (Worker Tick + Telegram Polling)
# -----------------------------------------------------------------------------
def background_worker_loop(worker: Worker):
    LOG.info("Запущен фоновый поток очереди WatchNext.")
    next_tick = 0

    while not stop_event.is_set():
        try:
            worker.q.login()

            # Poll Telegram if configured
            telegram = worker.tg
            if isinstance(telegram, Telegram) and telegram.owner > 0:
                try:
                    for update in telegram.updates(worker.state["offset"], timeout=1 if worker.ui_jobs_pending() else 10):
                        if telegram is not worker.tg:
                            break
                        message = update.get("message", {})
                        try:
                            with worker_lock:
                                if telegram is not worker.tg:
                                    break
                                if "callback_query" in update:
                                    worker.callback(update["callback_query"])
                                else:
                                    worker.command(message)
                        except (ValueError, OverflowError) as exc:
                            worker.reply_error(message, exc)
                        with worker_lock:
                            if telegram is not worker.tg:
                                break
                            worker.state["offset"] = update["update_id"] + 1
                            worker.store.save()
                except Exception as exc:
                    LOG.debug("Telegram polling error: %s", exc)

            with worker_lock:
                worker.search_poll()
                try:
                    worker.follow_cycle()
                except Exception:
                    LOG.exception("Ошибка проверки подписок; медиаочередь продолжает работу.")

                if time.monotonic() >= next_tick:
                    worker.tick()
                    next_tick = time.monotonic() + 20

                if worker.state["alerts"].pop("service", None):
                    worker.store.save()
                    LOG.info("Связь с сервисами восстановлена.")

        except (APIError, OSError, KeyError, ValueError) as exc:
            worker.q.logged_in = False
            for s in worker.state.get("seasons", []):
                try:
                    worker.stop(s)
                except (APIError, OSError):
                    pass
            LOG.error("Цикл остановлен: %s%s", type(exc).__name__, ": " + str(exc) if isinstance(exc, APIError) else "")
            try:
                worker.alert(
                    "service",
                    "Очередь приостановлена: один из сервисов недоступен или настройки некорректны. Проверь Jellyfin, qBittorrent и .env. Повторю автоматически.",
                )
            except (APIError, OSError):
                pass
            time.sleep(10)

        time.sleep(1)


# -----------------------------------------------------------------------------
# FastAPI App Definition & Lifespan
# -----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    worker = get_worker()
    thread = threading.Thread(
        target=background_worker_loop, args=(worker,), name="WatchNextBackgroundLoop", daemon=True
    )
    thread.start()
    yield
    stop_event.set()
    thread.join(timeout=3)


app = FastAPI(
    title="WatchNext API Server",
    description="REST API для управления медиаочередью WatchNext с поддержкой Android приложения и Telegram бота.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "status": "ok",
        "app": "WatchNext Server",
        "version": "1.0.0",
        "docs": "/docs",
    }


@app.get("/api/health")
def health():
    return {"status": "ok", "time": time.time()}


@app.get("/api/status", response_model=QueueStatusResponse, dependencies=[Depends(verify_api_token)])
def get_status(worker: Worker = Depends(get_worker)):
    with worker_lock:
        state = worker.state
        try:
            usage = shutil.disk_usage(worker.root)
            disk_total = round(usage.total / GB, 2)
            disk_free = round(usage.free / GB, 2)
            disk_used = round(usage.used / GB, 2)
        except Exception:
            disk_total = disk_free = disk_used = 0.0

        active_downloads = sum(
            1 for s in state.get("seasons", [])
            if s.get("phase") == "ready" and not s.get("download_idle")
        )
        total_packs = sum(s.get("phase") != "complete" for s in state.get("seasons", []))

        cap_gb = (state.get("cap") / GB) if state.get("cap") is not None else None
        reserve_gb = (state.get("reserve", 5 * GB) / GB)

        service_alert = "Очередь приостановлена: проверь подключение к сервисам" if state.get("alerts", {}).get("service") else None
        limit_alert = "Достигнут лимит очереди или дискового пространства" if (state.get("alerts", {}).get("limit") or state.get("alerts", {}).get("disk")) else None

        return QueueStatusResponse(
            episodeCountLimit=state.get("count"),
            capGb=cap_gb,
            reserveGb=reserve_gb,
            isPaused=state.get("paused", False),
            diskTotalGb=disk_total,
            diskFreeGb=disk_free,
            diskUsedGb=disk_used,
            activeDownloadsCount=active_downloads,
            totalPacksCount=total_packs,
            serviceAlert=service_alert,
            limitAlert=limit_alert,
        )


@app.post("/api/queue/pause", dependencies=[Depends(verify_api_token)])
def pause_queue(worker: Worker = Depends(get_worker)):
    with worker_lock:
        worker.state["paused"] = True
        worker.state["pause_reason"] = "user"
        for s in worker.state.get("seasons", []):
            try:
                worker.stop(s)
            except Exception:
                pass
        worker.store.save()
    return {"status": "paused", "isPaused": True}


@app.post("/api/queue/resume", dependencies=[Depends(verify_api_token)])
def resume_queue(worker: Worker = Depends(get_worker)):
    with worker_lock:
        worker.state["paused"] = False
        worker.state.pop("pause_reason", None)
        worker.store.save()
        try:
            worker.tick()
        except Exception:
            pass
    return {"status": "resumed", "isPaused": False}


@app.post("/api/queue/toggle-pause", dependencies=[Depends(verify_api_token)])
def toggle_pause(worker: Worker = Depends(get_worker)):
    with worker_lock:
        will_pause = not worker.state.get("paused", False)
        worker.state["paused"] = will_pause
        if will_pause:
            worker.state["pause_reason"] = "user"
            for s in worker.state.get("seasons", []):
                try:
                    worker.stop(s)
                except Exception:
                    pass
        else:
            worker.state.pop("pause_reason", None)
            try:
                worker.tick()
            except Exception:
                pass
        worker.store.save()
    return {"isPaused": will_pause}


@app.post("/api/queue/tick", dependencies=[Depends(verify_api_token)])
def trigger_tick(worker: Worker = Depends(get_worker)):
    with worker_lock:
        worker.tick()
    return {"status": "ok"}


@app.post("/api/limits", dependencies=[Depends(verify_api_token)])
def update_limits(body: UpdateLimitsRequest, worker: Worker = Depends(get_worker)):
    # Validate the entire patch before changing memory or durable state.
    import math
    updates = {}
    fields = body.model_fields_set
    if "episodeCountLimit" in fields:
        if body.episodeCountLimit is not None and not 1 <= body.episodeCountLimit <= 1000:
            raise HTTPException(400, "Количество серий должно быть от 1 до 1000")
        updates["count"] = body.episodeCountLimit
    if "capGb" in fields:
        if body.capGb is not None and not math.isfinite(body.capGb):
            raise HTTPException(400, "Лимит должен быть конечным числом")
        updates["cap"] = int(body.capGb * GB) if body.capGb is not None and body.capGb > 0 else None
    if "reserveGb" in fields:
        if body.reserveGb is None or not math.isfinite(body.reserveGb) or body.reserveGb < 0:
            raise HTTPException(400, "Резерв должен быть неотрицательным числом")
        updates["reserve"] = int(body.reserveGb * GB)
    with worker_lock:
        previous = {key: worker.state.get(key) for key in updates}
        worker.state.update(updates)
        try:
            worker.store.save()
        except Exception:
            worker.state.update(previous)
            raise
    return {"status": "updated"}



class TelegramSettingsRequest(BaseModel):
    telegramToken: Optional[str] = None
    telegramOwnerId: str = ""
    telegramEnabled: bool = False


@app.get("/api/integrations", dependencies=[Depends(verify_api_token)])
def integration_status():
    return public_settings()


@app.post("/api/integrations", dependencies=[Depends(verify_api_token)])
def update_integrations(body: TelegramSettingsRequest, worker: Worker = Depends(get_worker)):
    with integration_update_lock:
        return _update_integrations(body, worker)


def _update_integrations(body, worker):
    # Validation and network calls happen before swapping the running poller.
    with worker_lock:
        data = load_settings()
        old_token, _, _ = telegram_config(data)
    token = (body.telegramToken or "").strip() or old_token
    owner = body.telegramOwnerId.strip()
    if owner and not re.fullmatch(r"[1-9]\d{0,19}", owner):
        raise HTTPException(400, "ID владельца должен быть положительным числом")
    tg = DummyTelegram()
    username = data.get("telegramUsername", "")
    if body.telegramEnabled or body.telegramToken:
        if not re.fullmatch(r"\d{5,20}:[A-Za-z0-9_-]{20,200}", token) or not owner:
            raise HTTPException(400, "Укажите токен бота и ID владельца")
        try:
            client = HTTP("https://api.telegram.org/bot" + token)
            reply = client.call('/getMe', timeout=8)
            if not reply.get('ok') or not reply.get('result', {}).get('is_bot'):
                raise ValueError()
            username = reply['result'].get('username', '')
        except Exception:
            raise HTTPException(400, "Telegram не подтвердил токен. Проверьте токен и соединение.") from None
    if body.telegramEnabled:
        tg = Telegram(token=token, owner=owner)
    with worker_lock:
        data.update(telegramToken=token, telegramOwnerId=owner,
                    telegramEnabled=body.telegramEnabled, telegramUsername=username)
        save_settings(data)
        if token != old_token:
            worker.state['offset'] = 0
            worker.store.save()
        worker.tg = tg
    return public_settings(data)


@app.get("/api/jellyfin/status", dependencies=[Depends(verify_api_token)])
def jellyfin_status(worker: Worker = Depends(get_worker)):
    try:
        info = worker.j.http.call('/System/Info', timeout=3)
        return {'connected': bool(info.get('Id'))}
    except Exception:
        return {'connected': False}


@app.get("/api/jellyfin/link", dependencies=[Depends(verify_api_token)])
def jellyfin_link(pack: str = Query(..., max_length=100), index: Optional[int] = None,
                  worker: Worker = Depends(get_worker)):
    with worker_lock:
        try:
            season = worker.season_by_id(pack)
        except ValueError:
            raise HTTPException(404, "Раздача не найдена") from None
        if not season:
            raise HTTPException(404, "Раздача не найдена")
        episodes = season.get('episodes', [])
        if index is not None:
            episodes = [e for e in episodes if e['index'] == index]
            if not episodes:
                raise HTTPException(404, "Эпизод не найден")
        paths = {str(worker.dest(season, e)) for e in episodes if e.get('published')}
    if not paths:
        return {'available': False, 'reason': 'Файл ещё не опубликован в Jellyfin'}
    try:
        items, start = [], 0
        while True:
            page = worker.j.http.call('/Items', query={'recursive': 'true', 'fields': 'Path,ProviderIds,OriginalTitle',
                'includeItemTypes': 'Episode,Movie,Video', 'startIndex': start, 'limit': 200})
            batch = page.get('Items', [])
            items.extend(batch)
            start += len(batch)
            if not batch or start >= page.get('TotalRecordCount', start):
                break
        found = next((item for item in items if item.get('Path') in paths), None)
        if not found:
            return {'available': False, 'reason': 'Jellyfin ещё не проиндексировал файл'}
        item_id = found.get('Id')
        # The pack opens its series; a specific episode opens its own details.
        if index is None and season.get('kind') != 'movie':
            item_id = found.get('SeriesId') or item_id
            if item_id != found.get('Id'):
                if not re.fullmatch(r'[A-Za-z0-9-]{1,100}', str(item_id)):
                    raise ValueError()
                series_items = worker.j.http.call('/Items', query={'ids': str(item_id), 'fields': 'ProviderIds,OriginalTitle'}).get('Items', [])
                if not series_items:
                    raise ValueError()
                found = series_items[0]
        server_id = worker.j.http.call('/System/Info/Public').get('Id', '')
        if not re.fullmatch(r'[A-Za-z0-9-]{1,100}', str(item_id)) or not re.fullmatch(r'[A-Za-z0-9-]{1,100}', str(server_id)):
            raise ValueError()
        providers = found.get('ProviderIds') or {}
        imdb_id, tmdb_id = str(providers.get('Imdb') or ''), str(providers.get('Tmdb') or '')
        return {'available': True, 'itemId': item_id, 'serverId': server_id,
                'imdbId': imdb_id if re.fullmatch(r'tt\d{5,12}', imdb_id) else '',
                'tmdbId': tmdb_id if re.fullmatch(r'[1-9]\d{0,9}', tmdb_id) else '',
                'title': str(found.get('Name') or '')[:300],
                'originalTitle': str(found.get('OriginalTitle') or '')[:300],
                'year': found.get('ProductionYear')}
    except Exception:
        raise HTTPException(502, "Не удалось проверить библиотеку Jellyfin") from None

# -----------------------------------------------------------------------------
# Pack Helpers & Endpoints
# -----------------------------------------------------------------------------
def serialize_pack(s: Dict[str, Any], torrent_info: Optional[Dict[str, Any]] = None, files_info: Optional[List[Dict[str, Any]]] = None) -> PackItemResponse:
    episodes_data: List[EpisodeItemResponse] = []
    by_index = {f["index"]: f for f in (files_info or [])}

    phase = s.get("phase", "ready")
    if phase == "metadata":
        state_str = "METADATA"
    elif phase in ("needcheck", "checking"):
        state_str = "CHECKING"
    elif phase in ("complete", "retiring"):
        state_str = "COMPLETE"
    elif phase == "missing":
        state_str = "STOPPED"
    elif s.get("download_idle"):
        state_str = "READY"
    else:
        state_str = "DOWNLOADING"

    kind_str = "MOVIE" if s.get("kind") == "movie" else "SERIES"
    total_size = sum(e.get("size", 0) for e in s.get("episodes", []))
    ready_count = 0

    for e in s.get("episodes", []):
        watched = e.get("watched", False)
        discarded = e.get("discarded", False)
        skipped = e.get("skipped_start", False)
        published = e.get("published", False)
        selected = e.get("selected", False)

        if watched:
            status_str = "PLAYED"
            prog = 1.0
            ready_count += 1
        elif discarded or skipped:
            status_str = "SKIPPED"
            prog = 0.0
        elif published:
            status_str = "COMPLETED"
            prog = 1.0
            ready_count += 1
        elif selected:
            status_str = "DOWNLOADING"
            f = by_index.get(e["index"])
            prog = f.get("progress", 0.0) if f else 0.0
        else:
            status_str = "WAITING_LIMIT"
            prog = 0.0

        label = "Фильм" if s.get("kind") == "movie" else episode_label(e, s.get("season", 1))

        episodes_data.append(
            EpisodeItemResponse(
                index=e["index"],
                name=e.get("name", ""),
                season=e.get("season", s.get("season", 1)),
                episode=e.get("number", 1),
                label=label,
                size=e.get("size", 0),
                progress=float(prog),
                status=status_str,
                isPublished=published,
            )
        )

    if torrent_info:
        progress = float(torrent_info.get("progress", 0.0))
        downloaded = int(torrent_info.get("completed", 0) or torrent_info.get("downloaded", 0))
        dlspeed = int(torrent_info.get("dlspeed", 0))
    else:
        completed_size = sum(e.size for e in episodes_data if e.status in ("COMPLETED", "PLAYED"))
        progress = (completed_size / total_size) if total_size > 0 else 0.0
        downloaded = completed_size
        dlspeed = 0

    return PackItemResponse(
        hash=s["hash"],
        title=s.get("title", ""),
        kind=kind_str,
        season=s.get("season", 1),
        startEpisode=s.get("start_episode", 1),
        state=state_str,
        progress=round(progress, 3),
        totalSize=total_size,
        downloadedSize=downloaded,
        downloadSpeed=dlspeed,
        readyEpisodes=ready_count,
        totalEpisodes=len(episodes_data),
        noNextSeason=s.get("notified", False) or s.get("no_next", False),
        episodes=episodes_data,
    )


@app.get("/api/packs", response_model=List[PackItemResponse], dependencies=[Depends(verify_api_token)])
def list_packs(worker: Worker = Depends(get_worker)):
    with worker_lock:
        seasons = [s for s in worker.ordered_seasons() if s.get("phase") != "complete"]
        try:
            infos = {t["hash"]: t for t in worker.q.get("info")}
        except Exception:
            infos = {}

        result = []
        for s in seasons:
            info = infos.get(s["hash"])
            files = []
            if info and s.get("phase") not in ("metadata", "complete", "missing"):
                try:
                    files = worker.q.files(s["hash"])
                except Exception:
                    files = []
            result.append(serialize_pack(s, info, files))
        return result


@app.get("/api/packs/{pack_hash}", response_model=PackItemResponse, dependencies=[Depends(verify_api_token)])
def get_pack(pack_hash: str, worker: Worker = Depends(get_worker)):
    with worker_lock:
        s = next((p for p in worker.state.get("seasons", []) if p["hash"].lower() == pack_hash.lower()), None)
        if not s:
            raise HTTPException(status_code=404, detail="Раздача не найдена")

        try:
            info = next((t for t in worker.q.get("info", hashes=s["hash"])), None)
        except Exception:
            info = None

        files = []
        if info:
            try:
                files = worker.q.files(s["hash"])
            except Exception:
                files = []

        return serialize_pack(s, info, files)


@app.post("/api/packs/add", dependencies=[Depends(verify_api_token)])
def add_pack(body: AddTorrentRequest, worker: Worker = Depends(get_worker)):
    with worker_lock:
        try:
            title = body.title.strip() if body.title else ""
            if not title:
                # Extract hash or fallback
                title = f"Раздача #{magnet_hash(body.magnet)[:8]}"

            res = worker.add(
                title=title,
                number=body.season,
                magnet=body.magnet,
                start_episode=body.startEpisode,
                kind=body.kind.lower(),
                auto_season=body.autoSeason,
            )
            return {"status": "ok", "message": res}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except APIError as exc:
            raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/packs/{pack_hash}/from", dependencies=[Depends(verify_api_token)])
def set_pack_start(pack_hash: str, body: SetStartRequest, worker: Worker = Depends(get_worker)):
    with worker_lock:
        pack = next((s for s in worker.state.get("seasons", []) if s["hash"].lower() == pack_hash.lower()), None)
        if not pack:
            raise HTTPException(status_code=404, detail="Раздача не найдена")
        try:
            worker.set_start(pack, body.season, body.startEpisode)
            worker.ui_autostart()
            try:
                worker.tick()
            except Exception:
                pass
            return {"status": "ok", "season": body.season, "startEpisode": body.startEpisode}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/packs/{pack_hash}/finish", dependencies=[Depends(verify_api_token)])
def finish_pack(pack_hash: str, worker: Worker = Depends(get_worker)):
    with worker_lock:
        pack = next((s for s in worker.state.get("seasons", []) if s["hash"].lower() == pack_hash.lower()), None)
        if not pack:
            raise HTTPException(status_code=404, detail="Раздача не найдена")
        pack["notified"] = True
        pack["no_next"] = True
        worker.store.save()
        return {"status": "ok"}


@app.post("/api/packs/{pack_hash}/retry", dependencies=[Depends(verify_api_token)])
def retry_pack(pack_hash: str, worker: Worker = Depends(get_worker)):
    with worker_lock:
        pack = next((s for s in worker.state.get("seasons", []) if s["hash"].lower() == pack_hash.lower()), None)
        if not pack:
            raise HTTPException(status_code=404, detail="Раздача не найдена")
        pack.pop("error", None)
        if not pack.get("episodes"):
            pack["phase"] = "metadata"
        else:
            try:
                worker.q.post("recheck", hashes=pack["hash"])
                pack["phase"] = "checking"
            except Exception:
                pass
        worker.state["alerts"].pop("pack:" + pack["hash"], None)
        worker.store.save()
        return {"status": "ok"}


@app.delete("/api/packs/{pack_hash}", dependencies=[Depends(verify_api_token)])
def delete_pack(pack_hash: str, worker: Worker = Depends(get_worker)):
    with worker_lock:
        pack = next((s for s in worker.state.get("seasons", []) if s["hash"].lower() == pack_hash.lower()), None)
        if not pack:
            raise HTTPException(status_code=404, detail="Раздача не найдена")

        try:
            worker.stop(pack)
        except Exception:
            pass

        for e in pack.get("episodes", []):
            try:
                worker.dest(pack, e).unlink(missing_ok=True)
                worker.source(pack, e).unlink(missing_ok=True)
            except Exception:
                pass

        try:
            folder = worker.root / "downloads" / pack["hash"]
            if folder.exists():
                shutil.rmtree(folder, ignore_errors=True)
        except Exception:
            pass

        try:
            worker.q.post("delete", hashes=pack["hash"], deleteFiles="true")
        except Exception:
            pass

        worker.state["seasons"] = [s for s in worker.state.get("seasons", []) if s["hash"] != pack["hash"]]
        worker.state["replies"] = {k: v for k, v in worker.state.get("replies", {}).items() if v != pack["hash"]}
        for prefix in ("pack:", "metadata:", "missing:"):
            worker.state["alerts"].pop(prefix + pack["hash"], None)

        worker.refresh_needed = True
        worker.store.save()
        return {"status": "deleted"}


@app.delete("/api/packs/{pack_hash}/episodes/{episode_index}", dependencies=[Depends(verify_api_token)])
def delete_episode(pack_hash: str, episode_index: int, worker: Worker = Depends(get_worker)):
    with worker_lock:
        pack = next((s for s in worker.state.get("seasons", []) if s["hash"].lower() == pack_hash.lower()), None)
        if not pack:
            raise HTTPException(status_code=404, detail="Раздача не найдена")
        ep = next((e for e in pack.get("episodes", []) if e["index"] == episode_index), None)
        if not ep:
            raise HTTPException(status_code=404, detail="Серия не найдена")

        try:
            _, active = worker.j.snapshot()
            if str(worker.dest(pack, ep)) in active:
                raise HTTPException(status_code=409, detail="Серия сейчас воспроизводится в Jellyfin")
        except HTTPException:
            raise
        except Exception:
            pass

        ep["discarded"] = True
        ep["seen_at"] = None

        was_active = ep.get("published") or ep.get("selected") or worker.source(pack, ep).exists()
        if was_active:
            try:
                worker.stop(pack)
            except Exception:
                pass
            try:
                worker.dest(pack, ep).unlink(missing_ok=True)
                worker.source(pack, ep).unlink(missing_ok=True)
            except Exception:
                pass
            ep["removed"] = True
            ep["published"] = False
            ep["selected"] = False
            worker.refresh_needed = True
            try:
                files = worker.q.files(pack["hash"])
                keep = {e["index"] for e in pack["episodes"] if e["selected"] and not finished(e)}
                worker.q.priorities(pack["hash"], files, keep)
                worker.q.post("recheck", hashes=pack["hash"])
                pack["phase"] = "checking"
            except Exception:
                pass
        else:
            ep["removed"] = True
            ep["published"] = False
            ep["selected"] = False

        worker.store.save()
        return {"status": "deleted", "index": episode_index}


# -----------------------------------------------------------------------------
# Jackett Search Endpoints
# -----------------------------------------------------------------------------
@app.get("/api/search", response_model=List[TorrentSearchResultResponse], dependencies=[Depends(verify_api_token)])
def search_torrents(
    query: str = Query(..., min_length=1, max_length=120),
    kind: str = Query("series"),
    minSeeders: int = Query(0, ge=0),
    voiceFilter: Optional[str] = Query(None),
    qualityFilter: Optional[str] = Query(None),
    worker: Worker = Depends(get_worker),
):
    clean_query = query.strip()
    pack_kind = "movie" if kind.lower() == "movie" else "series"

    try:
        jackett = getattr(worker, "jackett", None) or Jackett()
        raw_res = jackett.search(clean_query, pack_kind)
        items = raw_res.get("results", [])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка поиска Jackett: {exc}")

    return serialize_search(raw_res, pack_kind, minSeeders, voiceFilter, qualityFilter)


def serialize_search(raw_res, pack_kind, minSeeders=0, voiceFilter=None, qualityFilter=None):
    items = raw_res.get("results", [])
    results = []
    for item in items:
        title = item.get("title", "")
        seeders = item.get("seeders") or 0
        if seeders < minSeeders:
            continue

        raw_voices = voices(title)
        if raw_voices and raw_voices != "не указаны в названии":
            item_voices = [v.strip() for v in raw_voices.split(",") if v.strip()]
        else:
            item_voices = []

        if voiceFilter and not any(voiceFilter.lower() in v.lower() for v in item_voices):
            continue

        item_quality = item.get("quality") or quality(title)
        if qualityFilter and qualityFilter.lower() not in item_quality.lower():
            continue

        hints = release_numbers(title)
        det_season = hints["seasons"][0] if hints["seasons"] else 1
        det_episode = hints["episodes"][0] if hints["episodes"] else 1

        results.append(
            TorrentSearchResultResponse(
                title=title,
                tracker=item.get("tracker", ""),
                size=item.get("size") or 0,
                seeders=seeders,
                peers=item.get("peers"),
                quality=item_quality,
                voices=item_voices,
                magnet=item.get("magnet", ""),
                link=item.get("link", ""),
                detectedKind="MOVIE" if pack_kind == "movie" else "SERIES",
                detectedSeason=det_season,
                detectedStartEpisode=det_episode,
            )
        )

    return results


class StartSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=120)
    kind: str = "series"


def search_snapshot(identifier, after=-1):
    try:
        result = release_searches.snapshot(identifier, after)
    except KeyError:
        raise HTTPException(404, "Поиск истёк. Повторите запрос.") from None
    if 'results' in result:
        result['results'] = serialize_search({'results':result['results']}, result['kind'])
    return result


@app.post("/api/search/start", dependencies=[Depends(verify_api_token)])
def start_search(body: StartSearchRequest, worker: Worker = Depends(get_worker)):
    query = body.query.strip()
    if not query:
        raise HTTPException(400, "Введите название")
    kind = "movie" if body.kind.lower() == "movie" else "series"
    client = getattr(worker, "jackett", None) or Jackett()
    try:
        identifier = release_searches.start(query, kind, client)
    except ValueError as exc:
        raise HTTPException(429, str(exc)) from None
    return search_snapshot(identifier)


@app.get("/api/search/jobs/{identifier}", dependencies=[Depends(verify_api_token)])
def get_search(identifier: str, after: int = -1):
    return search_snapshot(identifier, after)


@app.post("/api/search/resolve", dependencies=[Depends(verify_api_token)])
def resolve_torrent(body: ResolveTorrentRequest, worker: Worker = Depends(get_worker)):
    try:
        jackett = getattr(worker, "jackett", None) or Jackett()
        magnet, torrent_bytes = jackett.resolve({"link": body.link})
        return {"magnet": magnet}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# -----------------------------------------------------------------------------
# Subscriptions Endpoints
# -----------------------------------------------------------------------------
@app.get("/api/subscriptions", response_model=List[SubscriptionItemResponse], dependencies=[Depends(verify_api_token)])
def list_subscriptions(worker: Worker = Depends(get_worker)):
    with worker_lock:
        subs = worker.follow_all()
        results = []
        for key, s in subs.items():
            candidate_title = None
            candidate_magnet = None
            sub_status = "ACTIVE"

            events = s.get("events", {})
            for ek, ev in sorted(events.items(), reverse=True):
                if ev.get("choices"):
                    sub_status = "RELEASE_FOUND"
                    candidate_title = ev["choices"][0].get("title")
                    candidate_magnet = ev["choices"][0].get("magnet")
                    break
                elif not ev.get("handled"):
                    sub_status = "EPISODE_OUT"

            nxt = s.get("next_episode")
            nxt_date = None
            nxt_label = None
            if nxt:
                nxt_label = f"Сезон {nxt.get('season')}, Серия {nxt.get('number')}"
                if nxt.get("air"):
                    nxt_date = time.strftime("%Y-%m-%d", time.gmtime(nxt["air"]))

            last_sn = s.get("last_season", 1)
            last_en = s.get("last_episode", 1)

            results.append(
                SubscriptionItemResponse(
                    id=key,
                    showTitle=s.get("title", ""),
                    preferredQuality=s.get("quality") or "1080p",
                    preferredVoice=s.get("voice") or "Любая",
                    status=sub_status,
                    lastSeason=last_sn,
                    lastEpisode=last_en,
                    nextEpisodeDate=nxt_date,
                    nextEpisodeLabel=nxt_label,
                    candidateReleaseTitle=candidate_title,
                    candidateReleaseMagnet=candidate_magnet,
                )
            )
        return results


@app.get("/api/shows/search", response_model=List[ShowSearchResultDto], dependencies=[Depends(verify_api_token)])
def search_shows(query: str = Query(..., min_length=1), worker: Worker = Depends(get_worker)):
    try:
        shows = TVmaze().find(query.strip(), jellyfin=worker.j)
        return [
            ShowSearchResultDto(
                id=s["id"],
                name=s["name"],
                premiered=s.get("premiered") or "",
                status=s.get("status") or "",
                language=s.get("language") or "",
            )
            for s in shows
        ]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка поиска TVmaze: {exc}")


@app.post("/api/subscriptions", dependencies=[Depends(verify_api_token)])
def add_subscription(body: AddSubscriptionRequest, worker: Worker = Depends(get_worker)):
    with worker_lock:
        show_obj = None
        if body.tvmazeId:
            try:
                raw_show = TVmaze().get(f"/shows/{body.tvmazeId}")
                if isinstance(raw_show, dict) and raw_show.get("id"):
                    show_obj = TVmaze.show(raw_show)
            except Exception:
                pass

        if not show_obj:
            try:
                shows = TVmaze().find(body.title, jellyfin=worker.j)
                if shows:
                    show_obj = shows[0]
            except Exception:
                pass

        if not show_obj:
            show_obj = {
                "id": int(time.time() * 1000) % 1000000,
                "name": body.title.strip(),
                "premiered": "",
                "status": "Running",
                "language": "English",
            }

        key = str(show_obj["id"])
        worker.follow_all()[key] = {
            "title": slug(body.title.strip()),
            "show": show_obj,
            "quality": "" if body.preferredQuality in ("Любое", "any", "") else body.preferredQuality,
            "voice": "" if body.preferredVoice in ("Любая", "any", "") else body.preferredVoice,
            "enabled": True,
            "since": time.time(),
            "dismissed": [],
            "events": {},
            "created": time.time(),
        }
        worker.store.save()
        return {"status": "ok", "id": key}


@app.delete("/api/subscriptions/{sub_id}", dependencies=[Depends(verify_api_token)])
def remove_subscription(sub_id: str, worker: Worker = Depends(get_worker)):
    with worker_lock:
        subs = worker.follow_all()
        if sub_id in subs:
            subs.pop(sub_id, None)
            worker.store.save()
            return {"status": "deleted"}
        raise HTTPException(status_code=404, detail="Подписка не найдена")


@app.post("/api/subscriptions/{sub_id}/import", dependencies=[Depends(verify_api_token)])
def import_subscription_release(sub_id: str, worker: Worker = Depends(get_worker)):
    with worker_lock:
        sub = worker.follow_all().get(sub_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Подписка не найдена")

        candidate_magnet = None
        candidate_title = None
        candidate_season = 1
        candidate_episode = 1

        events = sub.get("events", {})
        for ek, ev in events.items():
            if ev.get("choices"):
                candidate_magnet = ev["choices"][0].get("magnet")
                candidate_title = ev["choices"][0].get("title")
                candidate_season = ev.get("season", 1)
                candidate_episode = ev.get("number", 1)
                ev["handled"] = True
                break

        if not candidate_magnet:
            raise HTTPException(status_code=400, detail="Для этой подписки пока нет найденной раздачи")

        res = worker.add(
            title=sub["title"],
            number=candidate_season,
            magnet=candidate_magnet,
            start_episode=candidate_episode,
            kind="series",
        )
        worker.store.save()
        return {"status": "imported", "message": res}


def main():
    port = int(os.environ.get("SERVER_PORT", "8000"))
    host = os.environ.get("SERVER_HOST", "0.0.0.0")
    LOG.info("Запуск WatchNext сервера на %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
