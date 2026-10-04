import array, asyncio, json, re, time
from urllib.parse import quote
from pathlib import Path
from aiohttp import web, ClientSession, ClientTimeout
from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus

A2DP_SOURCE = "0000110a-0000-1000-8000-00805f9b34fb"
DEVICE = re.compile(r"(/org/bluez/hci\d+/dev_[0-9A-F]{2}(?:_[0-9A-F]{2}){5})")
ITUNES = "https://itunes.apple.com/search"
# /art may fetch pictures from these hosts only: Apple, Bandcamp, Cover Art Archive
ART_HOST = re.compile(r"^https://(?:(?:[\w-]+\.)*(?:mzstatic|bcbits)\.com|coverartarchive\.org)/")
ART_MAX = 5_000_000  # bytes
BANDCAMP = "https://bandcamp.com/api/fuzzysearch/1/app_autocomplete"  # not an official API
MUSICBRAINZ = "https://musicbrainz.org/ws/2/recording"
COVER_ART = "https://coverartarchive.org/release-group/{}/front-{}"
# MusicBrainz asks for a contact in the User-Agent. Put your own e-mail address or web page here.
USER_AGENT = "NowPlayingPi/1.0 (https://example.com/your-contact)"
MB_DELAY = 1.1  # seconds between MusicBrainz searches. The limit is one each second.
UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
STOP = {"and", "the", "his", "her", "with", "feat", "ft", "featuring"}
# Audio levels for the page background. parec records what the Pi sends to the amp.
CAPTURE = ["parec", "--device=@DEFAULT_MONITOR@", "--format=s16le",
           "--rate=22050", "--channels=1", "--latency-msec=40"]
CHUNK = 2048  # bytes: 1024 samples, 46 ms
MAX_MS = 36_000_000  # 10 hours. A bigger value means "unknown" (AVRCP uses 0xFFFFFFFF)
IDLE = dict(name="", connected=False, device="", status="stopped",
            title="", artist="", album="", duration=0, position=0,
            art="", art_small="", art_done=True)
state = dict(IDLE)
clients = set()
adapter = ""    # name of the Bluetooth adapter
devs = {}       # device path -> name, for connected audio devices
players = {}    # player path -> track fields from player()
hist = {}       # player path -> duration history, for the stale check
active = None   # player path that the TV shows
art_cache = {}  # image URL -> (content type, bytes)
lookups = {}    # (artist, title) -> data from iTunes. {} means "not found"
pending = set() # lookups that run now
broken = {}     # source name -> (failures in a row, skip until). A failing source rests.
mb_lock = asyncio.Lock()
mb_last = 0.0   # time of the last MusicBrainz search


def push(name="state", data=None):
    msg = (name, json.dumps(state if data is None else data))
    for q in clients:
        if name == "state" or q.qsize() < 50:  # drop level data for a slow page
            q.put_nowait(msg)


def valid(ms):
    return ms if isinstance(ms, int) and 0 < ms < MAX_MS else 0


def player(props):
    p = {k: v.value for k, v in props.items()}
    out = {}
    if "Status" in p:
        out["status"] = p["Status"]
    if "Track" in p:
        t = {k: v.value for k, v in p["Track"].items()}
        out.update(title=t.get("Title", ""), artist=t.get("Artist", ""),
                   album=t.get("Album", ""),
                   duration=valid(t.get("Duration", 0)), position=0)
    if "Position" in p:
        out["position"] = p["Position"] if p["Position"] < MAX_MS else 0
    return out


# ---- iTunes lookup: song length, album name, album art ----

def words(text):
    t = text.lower().replace("&", " and ")
    t = re.sub(r"\s*-\s*topic\s*$", "", t)        # YouTube Music artist channels
    t = re.sub(r"[\(\[].*?[\)\]]", " ", t)        # (Remastered), [Official Video]
    return {w for w in re.findall(r"\w+", t) if w not in STOP}


def same_artist(a, b):
    wa, wb = words(a), words(b)
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.6


def same_title(a, b):
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return False
    small, big = sorted((wa, wb), key=len)
    return small <= big and len(small) >= 0.6 * len(big)


def proxied(url):
    """The page loads art from this server (/art). Then it can read the colors."""
    return "/art?u=" + quote(url, safe="") if url else ""


def meta_of(r):
    art = r.get("artworkUrl100", "").replace("http://", "https://")
    return dict(duration=valid(r.get("trackTimeMillis", 0)),
                album=r.get("collectionName", ""),
                art=proxied(re.sub(r"\d+x\d+(bb|-\d+)", "600x600bb", art)),
                art_small=proxied(art))


def best_match(results, artist, title):
    found = [r for r in results if same_artist(artist, r.get("artistName", ""))]
    exact = [r for r in found if words(r.get("trackName", "")) == words(title)]
    close = [r for r in found if same_title(title, r.get("trackName", ""))]
    pick = (exact + close)[:1]
    return meta_of(pick[0]) if pick else {}


async def itunes(term):
    async with ClientSession(timeout=ClientTimeout(total=8)) as http:
        params = dict(term=term, entity="song", limit=10)
        async with http.get(ITUNES, params=params) as r:
            return await r.json(content_type=None)


async def fetch_json(url, params=None, headers=None):
    async with ClientSession(timeout=ClientTimeout(total=5), headers=headers) as http:
        async with http.get(url, params=params) as r:
            r.raise_for_status()
            return await r.json(content_type=None)


async def head_status(url):
    async with ClientSession(timeout=ClientTimeout(total=5),
                             headers={"User-Agent": USER_AGENT}) as http:
        async with http.head(url, allow_redirects=False) as r:
            return r.status


def choose(items, artist, title, artist_key, title_key):
    """The first item with the same artist. A title that is the same is the best."""
    found = [i for i in items if same_artist(artist, i.get(artist_key) or "")]
    exact = [i for i in found if words(i.get(title_key) or "") == words(title)]
    close = [i for i in found if same_title(title, i.get(title_key) or "")]
    return (exact + close or [None])[0]


async def find_bandcamp(artist, title):
    data = await fetch_json(BANDCAMP, params=dict(q=f"{artist} {title}"))
    results = data.get("results") or data.get("auto", {}).get("results", [])
    tracks = [r for r in results if r.get("type") == "t"
              and (r.get("img") or "").startswith("http")]
    r = choose(tracks, artist, title, "band_name", "name")
    if not r:
        return {}
    img = r["img"].replace("http://", "https://")
    return dict(duration=0, album=r.get("album_name") or "",
                art=proxied(re.sub(r"_\d+(\.\w+)$", r"_16\1", img)),  # 700 pixels
                art_small=proxied(img))


async def find_caa(artist, title):
    """MusicBrainz finds the release groups. Cover Art Archive has the pictures."""
    global mb_last
    clean = lambda t: re.sub(r'["\\]', " ", t)
    query = f'recording:"{clean(title)}" AND artist:"{clean(artist)}"'
    async with mb_lock:
        wait = MB_DELAY - (time.monotonic() - mb_last)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            data = await fetch_json(MUSICBRAINZ, headers={"User-Agent": USER_AGENT},
                                    params=dict(query=query, fmt="json", limit=5))
        finally:
            mb_last = time.monotonic()
    seen = set()
    for rec in data.get("recordings", []):
        credit = " ".join(c.get("name", "") for c in rec.get("artist-credit", []))
        if not (same_artist(artist, credit) and same_title(title, rec.get("title", ""))):
            continue
        for rel in rec.get("releases", []):
            group = (rel.get("release-group") or {}).get("id", "")
            if not UUID.match(group) or group in seen:
                continue
            seen.add(group)
            if await head_status(COVER_ART.format(group, 500)) in (200, 301, 302, 307, 308):
                return dict(duration=0, album=rel.get("title", ""),
                            art=proxied(COVER_ART.format(group, 500)),
                            art_small=proxied(COVER_ART.format(group, 250)))
            if len(seen) >= 4:
                return {}
    return {}


FALLBACKS = (("bandcamp", find_bandcamp), ("coverartarchive", find_caa))


async def guarded(name, find, artist, title):
    """Run one source. After 3 failures in a row the source rests for 10 minutes."""
    fails, until = broken.get(name, (0, 0.0))
    if time.time() < until:
        return {}
    try:
        meta = await find(artist, title)
    except Exception as e:
        fails += 1
        print(f"{name} failed ({fails}): {e}", flush=True)
        broken[name] = (fails, time.time() + 600 if fails >= 3 else 0.0)
        return {}
    broken[name] = (0, 0.0)
    return meta


async def lookup(key):
    artist, title = key
    try:
        meta = best_match((await itunes(f"{artist} {title}")).get("results", []), artist, title)
    except Exception as e:  # no network: do not store, try again on the next track event
        print("lookup failed:", e, flush=True)
        pending.discard(key)
        return
    found = dict(duration=0, album="", art="", art_small="")
    found.update(meta)
    if not found["art"]:    # Apple has no picture: ask the other sources
        for name, find in FALLBACKS:
            extra = await guarded(name, find, artist, title)
            if extra.get("art"):
                found.update(art=extra["art"], art_small=extra["art_small"])
                found["album"] = found["album"] or extra.get("album", "")
                break
    if len(lookups) > 200:
        lookups.pop(next(iter(lookups)))
    lookups[key] = found if any(found.values()) else {}
    pending.discard(key)
    render()


def track_key(f):
    return (f.get("artist", "").strip().lower(), f.get("title", "").strip().lower())


def want_lookup(key):
    if all(key) and key not in lookups and key not in pending:
        pending.add(key)
        asyncio.create_task(lookup(key))


# ---- player and display state ----

def device_of(path):
    m = DEVICE.match(path or "")
    return m.group(1) if m else None


def render():
    new = dict(IDLE, name=adapter)
    if devs:
        dev = device_of(active)
        if dev not in devs:
            dev = next(iter(devs))
        new.update(connected=True, device=devs[dev])
        if active in players and device_of(active) == dev:
            f = players[active]
            new.update({k: v for k, v in f.items() if k != "stale"})
            key = track_key(f)
            meta = lookups.get(key, {})
            new["art_done"] = key not in pending  # False while the lookup runs
            if f.get("stale") or not f.get("duration"):
                new["duration"] = meta.get("duration", 0)
            if not f.get("album"):
                new["album"] = meta.get("album", "")
            new.update(art=meta.get("art", ""), art_small=meta.get("art_small", ""))
    state.clear()
    state.update(new)
    push()


def update_player(path, props):
    f = players.setdefault(path, {})
    f.update(player(props))
    if "Track" in props:
        key = track_key(f)
        h = hist.setdefault(path, dict(key=None, cur=0, prev=0))
        if key != h["key"]:                # a new song: the old duration moves to "prev"
            h["key"], h["prev"] = key, h["cur"]
        h["cur"] = f["duration"]
        # The same duration to the millisecond as the song before: BlueZ kept the old value
        f["stale"] = bool(f["duration"]) and f["duration"] == h["prev"]
        want_lookup(key)


def on_player(path, props):
    global active
    update_player(path, props)
    if players[path].get("status") == "playing" or active not in players:
        active = path


async def scan(bus):
    global adapter, active
    reply = await bus.call(Message(
        destination="org.bluez", path="/",
        interface="org.freedesktop.DBus.ObjectManager",
        member="GetManagedObjects"))
    name, found, found_players = "", {}, {}
    for path, ifaces in reply.body[0].items():
        if "org.bluez.Adapter1" in ifaces:
            name = ifaces["org.bluez.Adapter1"]["Alias"].value
        dev = ifaces.get("org.bluez.Device1")
        if dev and dev["Connected"].value:
            uuids = dev["UUIDs"].value if "UUIDs" in dev else []
            if A2DP_SOURCE in uuids:
                found[path] = dev["Alias"].value
        if "org.bluez.MediaPlayer1" in ifaces:
            found_players[path] = ifaces["org.bluez.MediaPlayer1"]
    adapter = name
    devs.clear()
    devs.update(found)
    players.clear()
    for path, props in found_players.items():
        update_player(path, props)
    for path in [p for p in hist if p not in players]:
        del hist[path]
    playing = [p for p, f in players.items() if f.get("status") == "playing"]
    if active not in players or (active not in playing and playing):
        active = playing[0] if playing else next(iter(players), None)
    render()


def levels(chunk, y):
    """Return (loudness, bass, filter state) for one chunk. Values are 0 to 1."""
    s = array.array("h")
    s.frombytes(chunk)
    total = bass = 0.0
    for x in s:
        y += 0.0625 * (x - y)  # low-pass filter, about 230 Hz
        total += x * x
        bass += y * y
    n = len(s)
    return (total / n) ** 0.5 / 32768, (bass / n) ** 0.5 / 32768, y


async def capture():
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                *CAPTURE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
        except FileNotFoundError:
            print(f"{CAPTURE[0]} not found: no audio levels", flush=True)
            return
        y = 0.0
        try:
            while True:
                chunk = await proc.stdout.readexactly(CHUNK)
                if clients:
                    loud, bass, y = levels(chunk, y)
                    push("level", dict(l=round(loud, 4), b=round(bass, 4)))
        except asyncio.IncompleteReadError:
            pass
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
        await asyncio.sleep(3)


async def art_proxy(request):
    url = request.query.get("u", "")
    if not ART_HOST.match(url):
        raise web.HTTPBadRequest()
    if url not in art_cache:
        async with ClientSession(timeout=ClientTimeout(total=8)) as http:
            async with http.get(url) as r:
                if r.status != 200 or (r.content_length or 0) > ART_MAX:
                    raise web.HTTPNotFound()
                body = await r.read()
                ctype = r.headers.get("Content-Type", "image/jpeg")
        if len(art_cache) >= 30:
            art_cache.pop(next(iter(art_cache)))
        art_cache[url] = (ctype, body)
    ctype, body = art_cache[url]
    return web.Response(body=body, headers={
        "Content-Type": ctype, "Cache-Control": "max-age=86400"})


async def index(request):
    return web.FileResponse(Path(__file__).parent / "index.html")


async def events(request):
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await resp.prepare(request)
    q = asyncio.Queue()
    clients.add(q)
    q.put_nowait(("state", json.dumps(state)))
    try:
        while True:
            name, data = await q.get()
            await resp.write(f"event: {name}\ndata: {data}\n\n".encode())
    finally:
        clients.discard(q)
    return resp


async def main():
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    await bus.call(Message(
        destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus", member="AddMatch", signature="s",
        body=["type='signal',sender='org.bluez'"]))

    def on_signal(msg):
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.member == "PropertiesChanged":
            iface, changed, _ = msg.body
            if iface == "org.bluez.MediaPlayer1":
                on_player(msg.path, changed)
                render()
            elif iface == "org.bluez.Device1" and "Connected" in changed:
                asyncio.create_task(scan(bus))
        elif msg.member in ("InterfacesAdded", "InterfacesRemoved"):
            asyncio.create_task(scan(bus))

    bus.add_message_handler(on_signal)

    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/events", events),
                    web.get("/art", art_proxy)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8080).start()
    await scan(bus)
    levels_task = asyncio.create_task(capture())  # keep a reference
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())