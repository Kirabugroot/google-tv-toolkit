#!/usr/bin/env python3
import asyncio
import html
import json
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from pypresence import Presence
from pypresence.types import ActivityType, StatusDisplayType

CONFIG_DIR = Path.home() / ".config" / "tvpresence"


def setting(name, default=""):
    f = CONFIG_DIR / name
    return f.read_text().strip() if f.exists() else default


TV_IP = setting("tv_ip")
CLIENT_ID_FILE = CONFIG_DIR / "client_id"
WEBHOOK_FILE = CONFIG_DIR / "webhook_url"
HISTORY_FILE = Path(__file__).with_name("history.jsonl")
POLL_SECONDS = 15
FAST_POLL_SECONDS = 1
KEY_BURST_SECONDS = 8
REPOST_AFTER = 3 * 3600
UA = {"User-Agent": "Mozilla/5.0 (tvpresence)"}

APPS = {
    "org.smarttube.stable": ("YouTube", "youtube.com"),
    "com.google.android.youtube.tv": ("YouTube", "youtube.com"),
    "com.netflix.ninja": ("Netflix", "netflix.com"),
    "com.disney.disneyplus": ("Disney+", "disneyplus.com"),
    "com.amazon.amazonvideo.livingroom": ("Prime Video", "primevideo.com"),
    "com.apple.atve.androidtv.appletv": ("Apple TV+", "tv.apple.com"),
    "com.canal.android.canal": ("Canal+", "canalplus.com"),
    "com.plexapp.android": ("Plex", "plex.tv"),
    "fr.tf1.mytf1": ("TF1+", "tf1.fr"),
    "fr.m6.m6replay": ("M6+", "m6.fr"),
    "fr.francetv.pluzz": ("france.tv", "france.tv"),
    "tv.arte.plus7": ("Arte", "arte.tv"),
    "tv.molotov.app": ("Molotov", "molotov.tv"),
    "com.orange.owtv.tv": ("TV d'Orange", "orange.fr"),
    "com.stremio.one": ("Stremio", "stremio.com"),
    "org.videolan.vlc": ("VLC", "videolan.org"),
    "com.smarterspro.smartersprotv": ("Smarters Pro", "iptvsmarters.com"),
    "com.xornetstudio.isp": ("ISP Player", None),
    "com.ottplay.ottplay": ("OTT Play", None),
    "com.google.android.apps.mediashell": ("Chromecast", "google.com"),
}
YOUTUBE_APPS = {"org.smarttube.stable", "com.google.android.youtube.tv"}

IDLE = {
    "com.google.android.apps.tv.launcherx",
    "com.google.android.tvlauncher",
    "com.spocky.projengmenu",
    "com.android.tv.settings",
    "com.google.android.apps.tv.dreamx",
    "com.google.android.backdrop",
    "com.google.android.katniss",
    "com.android.systemui",
    "android",
}

LISTENERS = []
STATE = {"act": None, "info": None, "since": None, "serial": None}


def emit(event, **kw):
    for fn in LISTENERS:
        try:
            fn(event, **kw)
        except Exception as e:
            print(f"… listener {event}: {e}")


def _connected_serial():
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "device" and ((TV_IP and TV_IP in parts[0]) or "adb-tls-connect" in parts[0]):
            return parts[0]
    return None


def adb_serial():
    serial = _connected_serial()
    if serial:
        return serial
    out = subprocess.run(["adb", "mdns", "services"], capture_output=True, text=True, timeout=15).stdout
    m = re.search(r"_adb-tls-connect\._tcp\.?\s+([\d.]+:\d+)", out)
    if m:
        subprocess.run(["adb", "connect", m.group(1)], capture_output=True, timeout=15)
    return _connected_serial()


def adb(serial, *cmd):
    return subprocess.run(["adb", "-s", serial, "shell", *cmd],
                          capture_output=True, text=True, timeout=15).stdout


def screen_on(serial):
    return "mWakefulness=Awake" in adb(serial, "dumpsys", "power")


def foreground_window(serial):
    m = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([\w.]+)/([\w.$]+)", adb(serial, "dumpsys", "window"))
    return (m.group(1), m.group(2)) if m else (None, None)


class KeyWatcher(threading.Thread):

    def __init__(self):
        super().__init__(daemon=True)
        self.last_key = 0.0
        self.serial = None

    def run(self):
        while True:
            serial = self.serial
            if not serial:
                time.sleep(5)
                continue
            try:
                proc = subprocess.Popen(["adb", "-s", serial, "shell", "getevent", "-lq"],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                for line in proc.stdout:
                    if "EV_KEY" in line and "DOWN" in line:
                        self.last_key = time.time()
                    if self.serial != serial:
                        proc.kill()
                        break
            except Exception:
                pass
            time.sleep(5)

    def recent(self):
        return time.time() - self.last_key < KEY_BURST_SECONDS


KEYS = KeyWatcher()


OVERLAY_TITLE_APPS = {"com.smarterspro.smartersprotv"}
_TIME = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})$")
_NOT_A_TITLE = re.compile(r"^(\d{1,2}:)?\d{1,2}:\d{2}$|B/s$|^Speed \(|^Aspect Ratio$|^\d+%$|^-?\d+$", re.I)
_PREFIX = re.compile(r"^(\|[^|]{1,10}\||[A-Z]{2,4}\s*[-:|])\s*")
_overlay = {"window": None, "title": "", "subtitle": "", "progress": None, "paused": False,
            "elapsed": None, "seen_at": 0.0}


def _seconds(t):
    h, m, s = _TIME.match(t).groups()
    return int(h or 0) * 3600 + int(m) * 60 + int(s)


def _scan_overlay(serial, window):
    adb(serial, "uiautomator", "dump", "/sdcard/tvpresence_ui.xml")
    texts = [html.unescape(t).strip()
             for t in re.findall(r' text="([^"]+)"', adb(serial, "cat", "/sdcard/tvpresence_ui.xml"))]
    titles = [t for t in texts if not _NOT_A_TITLE.search(t)]
    if not titles:
        return
    now = time.time()
    o = _overlay
    o["title"] = _PREFIX.sub("", titles[0]).strip()
    live = "live" in window.lower()
    o["subtitle"] = _PREFIX.sub("", titles[1]).strip() if live and len(titles) > 1 else ""
    times = sorted(_seconds(t) for t in texts if _TIME.match(t))
    if not live and "vod" in window.lower() and len(times) >= 2 and times[-1] >= 600:
        elapsed, total = times[0], times[-1]
        if o["elapsed"] is not None and now - o["seen_at"] >= 2:
            o["paused"] = abs(elapsed - o["elapsed"]) < 1
        o["elapsed"], o["seen_at"] = elapsed, now
        start = int(now) - elapsed
        o["progress"] = (start, start + total)


def read_overlay(serial, window):
    if _overlay["window"] != window:
        _overlay.update(window=window, title="", subtitle="", progress=None, paused=False,
                        elapsed=None, seen_at=0.0)
    if not _overlay["title"] or KEYS.recent():
        _scan_overlay(serial, window)
    return {k: _overlay[k] for k in ("title", "subtitle", "progress", "paused")}


def scanning_overlay():
    return bool(_overlay["window"]) and (not _overlay["title"] or KEYS.recent())


def playing_media(serial):
    playing = {}
    for block in re.split(r"\n(?=\s{4}\S.*\n\s+ownerPid=)", adb(serial, "dumpsys", "media_session")):
        pkg = re.search(r"package=([\w.]+)", block)
        if not pkg or "state=PLAYING(3)" not in block:
            continue
        desc = re.search(r"description=([^\n]*)", block)
        parts = [p.strip() for p in desc.group(1).split(", ")] if desc else []
        parts = ["" if p == "null" else p for p in parts] + ["", ""]
        playing[pkg.group(1)] = (parts[0], parts[1])
    return playing


def current_activity(serial):
    if not screen_on(serial):
        return None
    pkg, act = foreground_window(serial)
    app = APPS.get(pkg, ("la télé", None))[0]
    if pkg in OVERLAY_TITLE_APPS and "player" in (act or "").lower():
        o = read_overlay(serial, f"{pkg}/{act}")
        return {"pkg": pkg, "app": app, "live": "live" in act.lower(), **o}
    _overlay["window"] = None
    if not pkg or pkg in IDLE:
        return None
    media = playing_media(serial)
    title, subtitle = media.get(pkg) or next((m for p, m in media.items() if p in APPS and m[0]), ("", ""))
    return {"pkg": pkg, "app": app, "title": title, "subtitle": subtitle,
            "progress": None, "paused": False, "live": False}


_info_cache = {}
_EPISODE = re.compile(r"\s*[-–|]?\s*S(\d{1,2})\s*[-.]?\s*E(\d{1,3})\b.*$", re.I)


def _get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=8) as r:
        return json.load(r)


_YEAR = re.compile(r"\(?\b((?:19|20)\d{2})\b\)?")
_TAGS = re.compile(r"\b(?:F?HD|UHD|SD|4K|HDR(?:10)?|MULTI|VOSTFR|VFF?|VFQ|VO|10bit|HEVC)\b|\(H\.?26[45]\)|\[[^\]]*\]", re.I)


def find_year(title):
    m = _YEAR.search(_EPISODE.sub("", title))
    return m.group(1) if m else None


def clean_title(title):
    t = _TAGS.sub("", _YEAR.sub("", _EPISODE.sub("", title)))
    return re.sub(r"\s{2,}", " ", t).strip(" -|")


def _lookup_movie(query, year):
    results = _get_json("https://v3.sg.media-imdb.com/suggestion/x/"
                        + urllib.parse.quote(query.lower()) + ".json").get("d", [])
    for res in results:
        if res.get("qid") in {"movie", "tvMovie", "tvSeries", "tvMiniSeries"} and str(res.get("y")) == year:
            poster = (res.get("i") or {}).get("imageUrl")
            return {"kind": "movie",
                    "poster": poster.replace("._V1_.jpg", "._V1_SX500.jpg") if poster else None,
                    "url": f"https://www.imdb.com/title/{res['id']}/", "episode": None}
    return None


def _lookup_episode(query, season, number):
    show = _get_json("https://api.tvmaze.com/singlesearch/shows?q=" + urllib.parse.quote(query))
    imdb = (show.get("externals") or {}).get("imdb")
    info = {"kind": "episode", "poster": (show.get("image") or {}).get("original"),
            "url": f"https://www.imdb.com/title/{imdb}/" if imdb else show.get("url"),
            "episode": f"S{season}E{number}"}
    try:
        ep = _get_json(f"https://api.tvmaze.com/shows/{show['id']}/episodebynumber"
                       f"?season={season}&number={number}")
        info["episode"] += f" · {ep['name']}"
    except Exception:
        pass
    return info


def _lookup_youtube(title):
    req = urllib.request.Request("https://www.youtube.com/results?search_query="
                                 + urllib.parse.quote_plus(title), headers=UA)
    with urllib.request.urlopen(req, timeout=8) as r:
        m = re.search(r'"videoId":"([\w-]{11})"', r.read().decode("utf-8", "ignore"))
    if not m:
        return None
    return {"kind": "video", "poster": f"https://i.ytimg.com/vi/{m.group(1)}/hqdefault.jpg",
            "url": f"https://youtu.be/{m.group(1)}", "episode": None}


def lookup_info(act):
    title = act["title"]
    if not title or act.get("live"):
        return None
    year = find_year(title)
    episode = _EPISODE.search(title)
    key = (act["pkg"] in YOUTUBE_APPS, title.lower())
    if key not in _info_cache:
        _info_cache[key] = None
        try:
            if act["pkg"] in YOUTUBE_APPS:
                _info_cache[key] = _lookup_youtube(f"{title} {act['subtitle']}".strip())
            elif episode:
                _info_cache[key] = _lookup_episode(clean_title(title), int(episode.group(1)),
                                                   int(episode.group(2)))
            elif year:
                _info_cache[key] = _lookup_movie(clean_title(title), year)
        except Exception as e:
            print(f"… infos indisponibles pour {title!r} ({e})")
    return _info_cache[key]


def app_logo(pkg):
    domain = APPS.get(pkg, (None, None))[1]
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=256" if domain else None


def display(act, info):
    if info and info.get("episode"):
        return clean_title(act["title"]), info["episode"]
    if act["live"]:
        return act["title"] or act["app"], act["subtitle"] or "En direct"
    if info and info["kind"] == "movie":
        return f"{clean_title(act['title'])} ({find_year(act['title'])})", act["subtitle"]
    return act["title"] or act["app"], act["subtitle"]


def fmt_duration(seconds):
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def _history():
    try:
        return [json.loads(line) for line in HISTORY_FILE.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def _history_append(entry):
    with HISTORY_FILE.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _webhook(method, suffix="", payload=None):
    if not WEBHOOK_FILE.exists():
        return None
    url = WEBHOOK_FILE.read_text().strip() + suffix
    req = urllib.request.Request(url, method=method, data=json.dumps(payload).encode() if payload else None,
                                 headers={"Content-Type": "application/json", **UA})
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read()
    return json.loads(body) if body else None


def _embed(act, info, watching, watched=0):
    main_line, second = display(act, info)
    if watching:
        desc = f"Sur **{act['app']}** · en cours 📺"
        color = 0xE50914
    else:
        desc = f"Regardé **{fmt_duration(watched)}** sur **{act['app']}**"
        color = 0x2ECC71
    embed = {
        "title": f"{'🎬' if watching else '✅'} {main_line}",
        "description": (f"*{second}*\n" if second else "") + desc,
        "color": color,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "footer": {"text": "En cours" if watching else "Terminé"},
    }
    if info:
        embed["url"] = info["url"]
        if info["poster"]:
            embed["thumbnail"] = {"url": info["poster"]}
    return embed


class Session:

    def __init__(self, act, info):
        self.act, self.info = act, info
        self.key = (act["app"], act["title"])
        self.started = time.time()
        self.msg_id, self.watched_before = None, 0
        hist = _history()
        for i in range(len(hist) - 1, -1, -1):
            h = hist[i]
            if h.get("type", "start") == "start" and h["title"] == act["title"]:
                if self.started - h["ts"] < REPOST_AFTER and h.get("msg_id"):
                    self.msg_id = h["msg_id"]
                    self.watched_before = sum(e.get("watched", 0) for e in hist[i:]
                                              if e.get("type") == "end" and e["title"] == act["title"])
                break
        try:
            embed = _embed(act, info, watching=True)
            if self.msg_id:
                _webhook("PATCH", f"/messages/{self.msg_id}", {"embeds": [embed]})
            else:
                msg = _webhook("POST", "?wait=true", {"username": "Télé", "embeds": [embed]})
                self.msg_id = msg and msg["id"]
            if self.msg_id:
                print("  ↳ salon : en cours")
        except Exception as e:
            print(f"… webhook en échec ({e})")
        _history_append({"type": "start", "ts": int(self.started), "app": act["app"], "title": act["title"],
                         "kind": info and info["kind"], "imdb": info and info["url"], "msg_id": self.msg_id})

    def end(self):
        watched = int(time.time() - self.started)
        _history_append({"type": "end", "ts": int(time.time()), "app": self.act["app"],
                         "title": self.act["title"], "kind": self.info and self.info["kind"],
                         "watched": watched})
        if self.msg_id:
            try:
                embed = _embed(self.act, self.info, watching=False, watched=self.watched_before + watched)
                _webhook("PATCH", f"/messages/{self.msg_id}", {"embeds": [embed]})
                print("  ↳ salon : terminé")
            except Exception as e:
                print(f"… webhook en échec ({e})")
        return watched


def presence_payload(act, info, since):
    app, pkg = act["app"], act["pkg"]
    main_line, second = display(act, info)
    payload = dict(
        activity_type=ActivityType.WATCHING,
        name=app,
        status_display_type=StatusDisplayType.DETAILS,
        details=main_line,
        large_image=(info and info["poster"]) or app_logo(pkg),
        large_text=act["title"] or app,
    )
    if act["paused"]:
        payload["state"] = f"⏸️ En pause · {second or app}"
    else:
        payload["state"] = (f"{second} · {app}" if second else
                            (f"{app} · sur la télé 📺" if act["title"] else "Sur la télé 📺"))
        start, end = act["progress"] or (since, None)
        payload.update(start=start, end=end)
    if info and info["poster"] and app_logo(pkg):
        payload.update(small_image=app_logo(pkg), small_text=app)
    buttons = []
    if info and info["kind"] == "video":
        buttons.append({"label": "▶️ Voir la vidéo", "url": info["url"]})
    elif act["title"] and not act["live"]:
        buttons.append({"label": "🎬 Bande-annonce",
                        "url": "https://www.youtube.com/results?search_query="
                               + urllib.parse.quote_plus(f"{clean_title(act['title'])} bande annonce")})
        if info:
            buttons.append({"label": "ℹ️ Fiche IMDb", "url": info["url"]})
    if buttons:
        payload["buttons"] = buttons
    return payload


def run():
    asyncio.set_event_loop(asyncio.new_event_loop())
    while not CLIENT_ID_FILE.exists():
        print(f"En attente de l'Application ID Discord dans {CLIENT_ID_FILE}", flush=True)
        time.sleep(60)
    rpc = Presence(CLIENT_ID_FILE.read_text().strip())
    KEYS.start()
    connected = False
    shown, since, since_key, session = None, None, None, None

    while True:
        try:
            if not connected:
                try:
                    rpc.connect()
                    connected, shown = True, None
                    print("✓ Connecté à Discord", flush=True)
                except Exception as e:
                    print(f"… Discord injoignable ({type(e).__name__}), statut en pause", flush=True)

            serial = adb_serial()
            KEYS.serial = serial
            act = current_activity(serial) if serial else None
            STATE["serial"] = serial

            if act != shown:
                key = act and (act["app"], act["title"])
                info = lookup_info(act) if act else None
                new_title = key != since_key
                if new_title:
                    since, since_key = int(time.time()), key
                if (session and session.key) != (key if act and act["title"] else None):
                    if session:
                        watched = session.end()
                        emit("end", act=session.act, info=session.info, watched=watched)
                        session = None
                    if act and act["title"]:
                        session = Session(act, info)
                        emit("start", act=act, info=info)
                STATE.update(act=act, info=info, since=since)
                if act:
                    if connected:
                        rpc.update(**presence_payload(act, info, since))
                    if new_title:
                        print(f"▶ Regarde {act['app']}" + (f" : {act['title']}" if act["title"] else ""),
                              flush=True)
                elif connected:
                    rpc.clear()
                    print("■ Rien à l'écran, statut effacé", flush=True)
                emit("update", act=act, info=info)
                shown = act
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"… {type(e).__name__}: {e} (nouvel essai dans {POLL_SECONDS}s)", flush=True)
            if connected:
                try:
                    rpc.close()
                except Exception:
                    pass
            connected, shown = False, None
        time.sleep(FAST_POLL_SECONDS if scanning_overlay() else POLL_SECONDS)

    if session:
        session.end()
    if connected:
        rpc.clear()
        rpc.close()


if __name__ == "__main__":
    run()
