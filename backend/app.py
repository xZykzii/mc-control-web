import json
import os
import re
import secrets
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import jwt
from flask import Flask, jsonify, redirect, request
from googleapiclient import discovery
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey


PROJECT_ID = os.environ.get("PROJECT_ID", "project-e8d49f4b-11ae-4521-b23")
ZONE = os.environ.get("ZONE", "us-central1-c")
INSTANCE = os.environ.get("INSTANCE", "minecraft-modpack-server")
MINECRAFT_PORT = int(os.environ.get("MINECRAFT_PORT", "25565"))
VM_AGENT_PORT = int(os.environ.get("VM_AGENT_PORT", "8090"))
CUSTOM_DOMAIN = os.environ.get("CUSTOM_DOMAIN", "").strip()
DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "").strip()
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
DISCORD_NOTIFY_CHANNEL_ID = os.environ.get("DISCORD_NOTIFY_CHANNEL_ID", "").strip()
NOTIFY_SECRET = os.environ.get("NOTIFY_SECRET", "").strip()
ALLOWED_ROLE_IDS = {
    role.strip() for role in os.environ.get("ALLOWED_ROLE_IDS", "").split(",") if role.strip()
}
ALLOWED_USER_IDS = {
    user.strip() for user in os.environ.get("ALLOWED_USER_IDS", "").split(",") if user.strip()
}

# --- Web login (Discord OAuth2) ---
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "").strip()
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "").strip()
SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip()
WEB_ORIGIN = os.environ.get("WEB_ORIGIN", "").strip().rstrip("/")
# Full URL of the frontend page (may include a path, e.g. GitHub Project Pages
# like https://user.github.io/repo). Falls back to WEB_ORIGIN when unset.
WEB_APP_URL = os.environ.get("WEB_APP_URL", "").strip().rstrip("/") or WEB_ORIGIN
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", str(12 * 3600)))

PING = 1
APPLICATION_COMMAND = 2
MESSAGE_COMPONENT = 3
PONG = 1
CHANNEL_MESSAGE = 4
DEFERRED_CHANNEL_MESSAGE = 5
DEFERRED_UPDATE_MESSAGE = 6
UPDATE_MESSAGE = 7
EPHEMERAL = 64
ADMINISTRATOR = 0x8
BUTTON = 2
ACTION_ROW = 1
BUTTON_SECONDARY = 2
BUTTON_SUCCESS = 3
BUTTON_DANGER = 4
BUTTON_PRIMARY = 1

app = Flask(__name__)
compute = discovery.build("compute", "v1", cache_discovery=False)


def mc_buttons():
    return [
        {
            "type": ACTION_ROW,
            "components": [
                {
                    "type": BUTTON,
                    "style": BUTTON_SUCCESS,
                    "label": "Encender",
                    "custom_id": "mc_start",
                    "emoji": {"name": "▶️"},
                },
                {
                    "type": BUTTON,
                    "style": BUTTON_DANGER,
                    "label": "Apagar",
                    "custom_id": "mc_stop",
                    "emoji": {"name": "⏹️"},
                },
                {
                    "type": BUTTON,
                    "style": BUTTON_PRIMARY,
                    "label": "IP",
                    "custom_id": "mc_ip",
                    "emoji": {"name": "\U0001f310"},
                },
            ],
        }
    ]


def response(
    content: str | None = None,
    embed: dict | None = None,
    ephemeral: bool = False,
    with_buttons: bool = True,
    response_type: int = CHANNEL_MESSAGE,
    buttons=mc_buttons,
):
    data: dict[str, Any] = {}
    if content:
        data["content"] = content
    if embed:
        data["embeds"] = [embed]
    if ephemeral:
        data["flags"] = EPHEMERAL
    if with_buttons:
        data["components"] = buttons()
    return jsonify({"type": response_type, "data": data})


def send_channel_message(content: str | None = None, embed: dict | None = None, with_buttons: bool = False):
    if not DISCORD_BOT_TOKEN or not DISCORD_NOTIFY_CHANNEL_ID:
        raise RuntimeError("DISCORD_BOT_TOKEN or DISCORD_NOTIFY_CHANNEL_ID is not configured")

    body: dict[str, Any] = {}
    if content:
        body["content"] = content
    if embed:
        body["embeds"] = [embed]
    if with_buttons:
        body["components"] = mc_buttons()
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{DISCORD_NOTIFY_CHANNEL_ID}/messages",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "mc-discord-control",
        },
    )
    # POST creates a new message - not safe to retry on a dropped
    # connection (see _urlopen_with_rate_limit_retry's docstring).
    return _urlopen_with_rate_limit_retry(req, idempotent=False)


def _urlopen_with_rate_limit_retry(req: urllib.request.Request, timeout: float = 20, max_retries: int = 3, idempotent: bool = True):
    """Discord returns 429 with a JSON body giving how many seconds to
    wait - editing the same status card message repeatedly (join/leave
    spam, or just heavy testing) can hit that limit, and silently
    swallowing the error would leave the card stuck showing stale data.
    That 429 retry is always safe: a 429 means Discord rejected the
    request outright without acting on it.

    A dropped connection (broken pipe, reset, timeout) is a different
    story - by the time we see the error, Discord may have already
    received and processed the request; we just didn't get the response
    back. Retrying is only safe when idempotent=True (PATCH/GET - editing
    or reading the same thing twice is harmless). For a POST that creates
    a new message, retrying on a connection error risks posting a second,
    duplicate copy of something Discord already accepted - seen in
    practice as the same join/leave line appearing several times in a
    row. So non-idempotent (POST) calls don't get that retry."""
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == max_retries:
                raise
            try:
                retry_after = json.loads(exc.read()).get("retry_after", 1)
            except Exception:
                retry_after = 1
            time.sleep(min(float(retry_after), 10) + 0.1)
        except (BrokenPipeError, ConnectionResetError, urllib.error.URLError, TimeoutError) as exc:
            if not idempotent or attempt == max_retries:
                raise
            print(f"discord request attempt {attempt + 1} failed: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(1)


def edit_channel_message(message_id: str, embed: dict):
    if not DISCORD_BOT_TOKEN or not DISCORD_NOTIFY_CHANNEL_ID:
        return
    body = {"embeds": [embed], "components": mc_buttons()}
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{DISCORD_NOTIFY_CHANNEL_ID}/messages/{message_id}",
        data=payload,
        method="PATCH",
        headers={
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "mc-discord-control",
        },
    )
    return _urlopen_with_rate_limit_retry(req)


def find_status_card_id() -> str | None:
    """Looks for this bot's status card (Apagado/Encendiendo/Listo) in the
    channel's recent history. Best-effort: returns None on any error."""
    if not DISCORD_BOT_TOKEN or not DISCORD_NOTIFY_CHANNEL_ID:
        return None
    try:
        # 100 is the max Discord allows per request. A small limit here was
        # the real bug behind the card getting "lost" and duplicated: any
        # burst of other channel activity (join/leave spam, or anything
        # else posting to the channel) pushes the card out of a short
        # lookback window, and a new one gets created instead of edited.
        messages = discord_get(f"/channels/{DISCORD_NOTIFY_CHANNEL_ID}/messages?limit=100")
        for m in messages:
            if not (m.get("author") or {}).get("bot"):
                continue
            embeds = m.get("embeds") or []
            if embeds and embeds[0].get("title") in STATUS_EMBED_TITLES:
                return m["id"]
    except Exception as exc:
        print(f"find_status_card_id failed: {type(exc).__name__}: {exc}", flush=True)
    return None


def sync_status_card(actor: str | None = None, action_label: str | None = None):
    """The status card is a single persistent message: this edits it in
    place if it already exists, or creates it if this is the first time.
    It is never deleted and reposted, so it can't "disappear" from the
    channel or lose its place in the scrollback. Best-effort: never raises."""
    try:
        embed = build_embed(actor, action_label)
        message_id = find_status_card_id()
        if message_id:
            edit_channel_message(message_id, embed)
        else:
            send_channel_message(embed=embed, with_buttons=True)
    except Exception as exc:
        print(f"sync_status_card failed: {type(exc).__name__}: {exc}", flush=True)


def notify_action(actor: str, action: str):
    """Best-effort announcement of who started/stopped the server, from
    the bot or the web page. Never raises: notification failures
    shouldn't break the start/stop action itself."""
    try:
        emoji = "\U0001f7e2" if action == "Encendido" else "\U0001f534"
        color = 0x6CC24A if action == "Encendido" else 0xD1573F
        send_channel_message(embed={
            "description": f"{emoji} **{action}** por **{actor}**",
            "color": color,
        })
    except Exception:
        pass


JOIN_LEAVE_RE = re.compile(r"^.+ (entro al mundo|salio del mundo)\. Jugadores online: \d+\.$")
START_STOP_RE = re.compile(r"\*\*(Encendido|Apagado)\*\* por \*\*.+\*\*")
IDLE_STATUS_RE = re.compile(
    r"^(Nadie jugo por un rato, asi que la VM se apago sola para ahorrar credito\."
    r"|Minecraft se esta cerrando y guardando el mundo\.)$"
)


STATUS_EMBED_TITLES = {"\U0001f534 Apagado", "\U0001f7e1 Encendiendo...", "\U0001f7e2 Listo para jugar"}


def _is_session_noise(m: dict[str, Any]) -> bool:
    # Note: the status card itself (title in STATUS_EMBED_TITLES) is
    # deliberately NOT treated as noise - it's a persistent message that
    # gets edited in place (see sync_status_card), never deleted.
    if not (m.get("author") or {}).get("bot"):
        return False
    content = m.get("content") or ""
    if JOIN_LEAVE_RE.match(content) or IDLE_STATUS_RE.match(content):
        return True
    embeds = m.get("embeds") or []
    if embeds and START_STOP_RE.search(embeds[0].get("description") or ""):
        return True
    return False


def cleanup_join_leave_messages():
    """Deletes this session's player join/leave spam plus any earlier
    Encendido/Apagado announcement, so the channel never accumulates more
    than the current one. Call this BEFORE posting a new status message,
    not after, so the fresh one isn't swept up with the old ones.
    Best-effort: never raises."""
    if not DISCORD_BOT_TOKEN or not DISCORD_NOTIFY_CHANNEL_ID:
        return
    try:
        messages = discord_get(f"/channels/{DISCORD_NOTIFY_CHANNEL_ID}/messages?limit=100")
        to_delete = [m["id"] for m in messages if _is_session_noise(m)]
        if not to_delete:
            return
        if len(to_delete) == 1:
            req = urllib.request.Request(
                f"https://discord.com/api/v10/channels/{DISCORD_NOTIFY_CHANNEL_ID}/messages/{to_delete[0]}",
                method="DELETE",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "User-Agent": "mc-discord-control"},
            )
        else:
            req = urllib.request.Request(
                f"https://discord.com/api/v10/channels/{DISCORD_NOTIFY_CHANNEL_ID}/messages/bulk-delete",
                data=json.dumps({"messages": to_delete}).encode("utf-8"),
                method="POST",
                headers={
                    "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                    "Content-Type": "application/json",
                    "User-Agent": "mc-discord-control",
                },
            )
        with urllib.request.urlopen(req, timeout=20):
            pass
    except Exception:
        pass


def verify_notify_request():
    if not NOTIFY_SECRET:
        raise RuntimeError("NOTIFY_SECRET is not configured")
    provided = request.headers.get("X-Notify-Secret", "")
    if not secrets.compare_digest(provided, NOTIFY_SECRET):
        auth = request.headers.get("Authorization", "")
        if not secrets.compare_digest(auth, f"Bearer {NOTIFY_SECRET}"):
            raise PermissionError("bad notify secret")


def verify_discord_request(raw_body: bytes):
    if not DISCORD_PUBLIC_KEY:
        raise RuntimeError("DISCORD_PUBLIC_KEY is not configured")
    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    try:
        VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY)).verify(
            timestamp.encode("utf-8") + raw_body,
            bytes.fromhex(signature),
        )
    except (BadSignatureError, ValueError) as exc:
        raise PermissionError("bad Discord signature") from exc


_TRANSIENT_CONNECTION_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, ssl.SSLError, OSError)


def _with_connection_retry(fn, *, max_retries: int = 2):
    """The Compute Engine client (httplib2) has no retry of its own for a
    connection dropped mid-request (BrokenPipeError etc.) - it just raises.
    Seen in practice hitting instance_get() from a /mc start click, which
    surfaced as a generic "something went wrong" to the user for what was
    really just a one-off dropped socket worth a quick retry."""
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except _TRANSIENT_CONNECTION_ERRORS as exc:
            if attempt == max_retries:
                raise
            print(f"{fn.__name__ if hasattr(fn, '__name__') else 'gce call'} attempt {attempt + 1} failed: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(1)


def instance_get() -> dict[str, Any]:
    return _with_connection_retry(
        lambda: compute.instances().get(project=PROJECT_ID, zone=ZONE, instance=INSTANCE).execute()
    )


def instance_start():
    return _with_connection_retry(
        lambda: compute.instances().start(project=PROJECT_ID, zone=ZONE, instance=INSTANCE).execute()
    )


def instance_stop():
    return _with_connection_retry(
        lambda: compute.instances().stop(project=PROJECT_ID, zone=ZONE, instance=INSTANCE).execute()
    )


def external_ip(instance: dict[str, Any]) -> str:
    for nic in instance.get("networkInterfaces", []):
        for access in nic.get("accessConfigs", []):
            if access.get("natIP"):
                return access["natIP"]
    return ""


# --- VM control agent (docker start/stop for whichever game container) ---
# The agent runs on the VM itself with a self-signed cert; we skip
# verification but keep TLS so the shared secret isn't sent in cleartext.
_AGENT_SSL_CONTEXT = ssl.create_default_context()
_AGENT_SSL_CONTEXT.check_hostname = False
_AGENT_SSL_CONTEXT.verify_mode = ssl.CERT_NONE

CRAFTY_CONTAINER = "crafty_container"


def agent_get(ip: str, path: str, timeout: float = 8) -> Any:
    req = urllib.request.Request(
        f"https://{ip}:{VM_AGENT_PORT}{path}",
        headers={"X-Log-Secret": NOTIFY_SECRET, "User-Agent": "mc-discord-control"},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_AGENT_SSL_CONTEXT) as resp:
        return json.loads(resp.read())


def agent_post(ip: str, path: str, body: dict[str, Any], timeout: float = 40) -> Any:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"https://{ip}:{VM_AGENT_PORT}{path}",
        data=data,
        method="POST",
        headers={
            "X-Log-Secret": NOTIFY_SECRET,
            "Content-Type": "application/json",
            "User-Agent": "mc-discord-control",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_AGENT_SSL_CONTEXT) as resp:
        return json.loads(resp.read())


def container_status(ip: str, name: str) -> str:
    try:
        return agent_get(ip, f"/container/status?name={name}").get("status", "unknown")
    except Exception:
        return "unknown"


def container_start(ip: str, name: str) -> bool:
    try:
        return bool(agent_post(ip, "/container/start", {"container": name}).get("ok"))
    except Exception:
        return False


def container_stop(ip: str, name: str) -> bool:
    try:
        return bool(agent_post(ip, "/container/stop", {"container": name}).get("ok"))
    except Exception:
        return False


def wait_for_agent(timeout: int = 240) -> str:
    """Polls the VM until it's RUNNING and the control agent answers.
    Returns the external IP, or '' if it never came up in time."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        instance = instance_get()
        if instance.get("status") == "RUNNING":
            ip = external_ip(instance)
            if ip:
                try:
                    agent_get(ip, f"/container/status?name={CRAFTY_CONTAINER}", timeout=4)
                    return ip
                except Exception:
                    pass
        time.sleep(5)
    return ""


def pack_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        temp = value & 0x7F
        value >>= 7
        if value:
            temp |= 0x80
        out.append(temp)
        if not value:
            return bytes(out)


def read_varint(sock: socket.socket) -> int:
    value = 0
    for i in range(5):
        byte = sock.recv(1)
        if not byte:
            raise OSError("connection closed")
        value |= (byte[0] & 0x7F) << (7 * i)
        if not byte[0] & 0x80:
            return value
    raise OSError("varint too long")


def read_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise OSError("connection closed")
        data.extend(chunk)
    return bytes(data)


def minecraft_status(host: str) -> dict[str, Any]:
    host_bytes = host.encode("utf-8")
    handshake = (
        pack_varint(0)
        + pack_varint(760)
        + pack_varint(len(host_bytes))
        + host_bytes
        + struct.pack(">H", MINECRAFT_PORT)
        + pack_varint(1)
    )
    request_packet = pack_varint(0)
    # Discord interactions are deferred (see run_deferred()), so this is no
    # longer bounded by Discord's 3s ACK window - just generous enough to
    # let a loaded modded server answer without waiting forever.
    with socket.create_connection((host, MINECRAFT_PORT), timeout=5) as sock:
        sock.settimeout(5)
        sock.sendall(pack_varint(len(handshake)) + handshake)
        sock.sendall(pack_varint(len(request_packet)) + request_packet)
        read_varint(sock)
        packet_id = read_varint(sock)
        if packet_id != 0:
            raise OSError(f"unexpected packet id {packet_id}")
        length = read_varint(sock)
        return json.loads(read_exact(sock, length).decode("utf-8"))


def minecraft_port_open(host: str) -> bool:
    try:
        with socket.create_connection((host, MINECRAFT_PORT), timeout=2):
            return True
    except OSError:
        return False


def member_can_control(payload: dict[str, Any]) -> bool:
    member = payload.get("member") or {}
    user = member.get("user") or payload.get("user") or {}
    if user.get("id") in ALLOWED_USER_IDS:
        return True
    roles = set(member.get("roles") or [])
    if ALLOWED_ROLE_IDS and roles.intersection(ALLOWED_ROLE_IDS):
        return True
    try:
        permissions = int(member.get("permissions", "0"))
        if permissions & ADMINISTRATOR:
            return True
    except ValueError:
        pass
    return False


def payload_actor_name(payload: dict[str, Any]) -> str:
    member = payload.get("member") or {}
    user = member.get("user") or payload.get("user") or {}
    return member.get("nick") or user.get("global_name") or user.get("username") or "alguien"


def command_name(payload: dict[str, Any]) -> str:
    data = payload.get("data") or {}
    options = data.get("options") or []
    if options:
        return options[0].get("name", "")
    return ""


def status_payload() -> dict[str, Any]:
    instance = instance_get()
    vm_status = instance.get("status", "UNKNOWN")
    ip = external_ip(instance)
    address = CUSTOM_DOMAIN or ip or ""
    data: dict[str, Any] = {
        "vm_status": vm_status,
        "address": address,
        "port": MINECRAFT_PORT,
        "minecraft_online": False,
    }
    if vm_status != "RUNNING":
        return data
    mc = None
    for attempt in range(2):
        try:
            mc = minecraft_status(ip)
            break
        except Exception as exc:
            print(f"minecraft_status({ip!r}) attempt {attempt + 1} failed: {type(exc).__name__}: {exc}", flush=True)
    if mc is not None:
        players = mc.get("players", {})
        data["minecraft_online"] = True
        data["players_online"] = players.get("online", 0)
        data["players_max"] = players.get("max")
        data["version"] = (mc.get("version") or {}).get("name")
    else:
        # The server list ping can be disabled, or too slow/flaky to
        # answer twice in a row, even though the game port itself accepts
        # connections just fine (e.g. enable-status=false, or a heavily
        # modded server under load). Fall back to a plain TCP check so the
        # UI doesn't get stuck showing "starting" forever.
        if minecraft_port_open(ip):
            data["minecraft_online"] = True
            data["status_unknown"] = True
        else:
            data["minecraft_online"] = False
    return data


COLOR_OFF = 0xD1573F
COLOR_STARTING = 0xE0A940
COLOR_ON = 0x6CC24A


def build_embed(actor: str | None = None, action_label: str | None = None) -> dict[str, Any]:
    data = status_payload()
    address = data["address"] or "sin IP externa"
    fields: list[dict[str, Any]] = []

    if data["vm_status"] != "RUNNING":
        color = COLOR_OFF
        title = "\U0001f534 Apagado"
        description = 'Usa `/mc start` o el boton "Encender" para prenderlo.'
    elif not data["minecraft_online"]:
        color = COLOR_STARTING
        title = "\U0001f7e1 Encendiendo..."
        description = (
            f"La VM esta prendida. El mundo esta cargando en `{address}:{data['port']}` "
            "(puede tardar 3-8 minutos)."
        )
    else:
        color = COLOR_ON
        title = "\U0001f7e2 Listo para jugar"
        description = f"`{address}:{data['port']}`"
        if data.get("status_unknown"):
            fields.append({"name": "Jugadores", "value": "desconocido (ping deshabilitado)", "inline": True})
        else:
            players = f"{data.get('players_online', 0)}/{data.get('players_max', '?')}"
            fields.append({"name": "Jugadores", "value": players, "inline": True})
            if data.get("version"):
                fields.append({"name": "Version", "value": data["version"], "inline": True})

    embed: dict[str, Any] = {"title": title, "description": description, "color": color, "fields": fields}
    if actor and action_label:
        embed["footer"] = {"text": f"{action_label} por {actor}"}
    return embed


@app.get("/")
def health():
    return "ok"


_RECENT_NOTIFY_EVENTS: dict[tuple[str, str], float] = {}
_DUPLICATE_EVENT_WINDOW_SECONDS = 30  # observed duplicate bursts were ~20s apart


@app.post("/notify")
def notify():
    try:
        verify_notify_request()
    except PermissionError:
        return "forbidden", 403

    payload = request.get_json(force=True, silent=True) or {}
    event = str(payload.get("event", "")).strip()
    player = str(payload.get("player", "")).strip()
    online = payload.get("online")

    # Safety net against whatever upstream is re-sending the same event
    # (seen in practice: the same "player left" line reported by the VM's
    # watcher many times in a row, ~20s apart - root cause not fully
    # pinned down, but a burst of truly identical events this close
    # together is never legitimate, so drop the repeats here regardless
    # of why they're happening).
    dedup_key = (event, player)
    now = time.time()
    last_seen = _RECENT_NOTIFY_EVENTS.get(dedup_key)
    _RECENT_NOTIFY_EVENTS[dedup_key] = now
    if len(_RECENT_NOTIFY_EVENTS) > 50:
        stale_cutoff = now - 60
        for key, ts in list(_RECENT_NOTIFY_EVENTS.items()):
            if ts < stale_cutoff:
                del _RECENT_NOTIFY_EVENTS[key]
    if last_seen is not None and now - last_seen < _DUPLICATE_EVENT_WINDOW_SECONDS:
        print(f"notify: dropping duplicate {dedup_key} ({now - last_seen:.1f}s after last)", flush=True)
        return "ok"

    if event == "server_open":
        # The world finished loading after a start (bot, web, or someone
        # else's /mc start) - sync the persistent status card to the real
        # ready-to-play state so people don't have to poll /mc status
        # themselves to find out.
        sync_status_card()
        return "ok"

    messages = {
        "server_closing": "Minecraft se esta cerrando y guardando el mundo.",
        "server_closed": "Nadie jugo por un rato, asi que la VM se apago sola para ahorrar credito.",
    }

    if event == "player_join" and player:
        suffix = f" Jugadores online: {online}." if online is not None else ""
        content = f"{player} entro al mundo.{suffix}"
    elif event == "player_leave" and player:
        suffix = f" Jugadores online: {online}." if online is not None else ""
        content = f"{player} salio del mundo.{suffix}"
    else:
        content = messages.get(event) or str(payload.get("message") or "Evento de Minecraft.")

    if event == "server_closed":
        # The VM auto-stopped on its own (idle timeout), not via a /mc stop
        # or the web page, so nothing else triggers this cleanup. Runs
        # before posting so the fresh message below isn't swept up too.
        cleanup_join_leave_messages()

    try:
        send_channel_message(content)
    except urllib.error.HTTPError as exc:
        return f"discord error {exc.code}: {exc.read().decode('utf-8', 'ignore')}", 502

    if event in ("player_join", "player_leave"):
        # Keep the persistent status card's player count live instead of
        # frozen at whatever it was when it was first posted.
        sync_status_card()
    elif event == "server_closed":
        # Reflect the real "Apagado" state on the card too, instead of
        # leaving it showing stale "Listo para jugar" data.
        sync_status_card()

    return "ok"


def mc_action(command: str, payload: dict) -> tuple[dict[str, Any], bool]:
    """Runs an /mc subcommand or button click. Returns (message_kwargs, ephemeral).
    Also syncs the one shared status card as a side effect (see
    sync_status_card) so the visible state is correct no matter whether
    this came from a button click (which already edits that card in
    place as the interaction response) or a typed slash command (whose
    response is a separate, private message - see interactions())."""
    if command == "status":
        sync_status_card()
        return {"embed": build_embed()}, False

    if command == "ip":
        instance = instance_get()
        ip = external_ip(instance)
        address = CUSTOM_DOMAIN or ip or "sin IP externa"
        return {"content": f"Direccion del servidor: `{address}:{MINECRAFT_PORT}`"}, False

    if command in {"start", "stop"} and not member_can_control(payload):
        return {"content": "No tienes permiso para controlar la VM."}, True

    actor = payload_actor_name(payload)

    if command == "start":
        instance = instance_get()
        if instance.get("status") == "RUNNING":
            sync_status_card()
            return {"embed": build_embed()}, False
        instance_start()
        ip = wait_for_agent()
        if not ip:
            return {"content": "La VM esta tardando en arrancar. Proba `/mc status` en un minuto."}, False
        container_start(ip, CRAFTY_CONTAINER)
        cleanup_join_leave_messages()
        notify_action(actor, "Encendido")
        sync_status_card(actor, "Encendido")
        return {"embed": build_embed(actor, "Encendido")}, False

    if command == "stop":
        instance = instance_get()
        if instance.get("status") != "RUNNING":
            sync_status_card()
            return {"embed": build_embed()}, False
        cleanup_join_leave_messages()
        notify_action(actor, "Apagado")
        instance_stop()
        sync_status_card(actor, "Apagado")
        return {"embed": build_embed(actor, "Apagado")}, False

    return {"content": "Comando desconocido."}, True


def send_followup(application_id: str, token: str, msg: dict[str, Any]):
    # Discord's message-edit PATCH only touches fields present in the body,
    # so content/embeds must always be sent explicitly (even as empty) -
    # otherwise a content-only reply (e.g. an error) leaves a stale embed
    # from whatever this message showed before still attached to it.
    data: dict[str, Any] = {
        "content": msg.get("content") or "",
        "embeds": [msg["embed"]] if msg.get("embed") else [],
        "components": mc_buttons(),
    }
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        f"https://discord.com/api/v10/webhooks/{application_id}/{token}/messages/@original",
        data=body,
        method="PATCH",
        headers={"Content-Type": "application/json", "User-Agent": "mc-discord-control"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def run_deferred(command: str, payload: dict[str, Any], application_id: str, token: str):
    """Runs the slow part (VM/game check) after Discord already got its 3s
    ACK, then edits the deferred message with the real result."""
    try:
        msg, _ = mc_action(command, payload)
        send_followup(application_id, token, msg)
    except Exception:
        import traceback
        traceback.print_exc()
        try:
            send_followup(application_id, token, {"content": "Ocurrio un error al consultar el servidor."})
        except Exception:
            traceback.print_exc()


@app.post("/")
def interactions():
    raw_body = request.get_data()
    try:
        verify_discord_request(raw_body)
    except PermissionError:
        return "invalid request signature", 401

    payload = request.get_json(force=True)
    itype = payload.get("type")

    if itype == PING:
        return jsonify({"type": PONG})

    if itype == APPLICATION_COMMAND:
        command = command_name(payload)
        if command in {"start", "stop"} and not member_can_control(payload):
            return response(
                "No tienes permiso para controlar la VM.",
                ephemeral=True, with_buttons=False,
            )
        # The VM check can take longer than Discord's 3s ACK window,
        # especially under load, so ACK immediately and edit the message
        # once run_deferred() finishes the real work.
        threading.Thread(
            target=run_deferred,
            args=(command, payload, payload.get("application_id", ""), payload.get("token", "")),
            daemon=True,
        ).start()
        ack: dict[str, Any] = {"type": DEFERRED_CHANNEL_MESSAGE}
        if command in {"start", "stop", "status"}:
            # These would otherwise post their own separate embed as a
            # brand new message (Discord always creates a fresh one for a
            # typed slash command, unlike a button click which edits the
            # message it lives on) - making it private avoids a second,
            # un-synced "status card" appearing next to the real one.
            ack["data"] = {"flags": EPHEMERAL}
        return jsonify(ack)

    if itype == MESSAGE_COMPONENT:
        custom_id = (payload.get("data") or {}).get("custom_id", "")
        _, _, command = custom_id.partition("_")
        if not command:
            return response("Interaccion no soportada.", ephemeral=True, with_buttons=False)
        if command in {"start", "stop"} and not member_can_control(payload):
            # Private reply that leaves the shared panel message untouched.
            return response(
                "No tienes permiso para controlar la VM.",
                ephemeral=True, with_buttons=False,
            )
        threading.Thread(
            target=run_deferred,
            args=(command, payload, payload.get("application_id", ""), payload.get("token", "")),
            daemon=True,
        ).start()
        return jsonify({"type": DEFERRED_UPDATE_MESSAGE})

    return response("Interaccion no soportada.", ephemeral=True, with_buttons=False)


# --- Web login (Discord OAuth2) + JSON API for the static frontend ---


class AuthError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def discord_token_exchange(code: str) -> dict[str, Any]:
    payload = urllib.parse.urlencode(
        {
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_REDIRECT_URI,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://discord.com/api/v10/oauth2/token",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "mc-discord-control",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def discord_get(path: str, *, user_token: str | None = None) -> Any:
    headers = {"User-Agent": "mc-discord-control"}
    headers["Authorization"] = f"Bearer {user_token}" if user_token else f"Bot {DISCORD_BOT_TOKEN}"
    req = urllib.request.Request(f"https://discord.com/api/v10{path}", headers=headers)
    return json.loads(_urlopen_with_rate_limit_retry(req))


def member_permissions_from_roles(role_ids: set[str]) -> int:
    perms = 0
    for role in discord_get(f"/guilds/{DISCORD_GUILD_ID}/roles"):
        if role["id"] == DISCORD_GUILD_ID or role["id"] in role_ids:
            perms |= int(role["permissions"])
    return perms


def create_session_token(uid: str, username: str, can_control: bool) -> str:
    now = int(time.time())
    payload = {
        "uid": uid,
        "username": username,
        "member": True,
        "can_control": can_control,
        "iat": now,
        "exp": now + SESSION_TTL_SECONDS,
    }
    return jwt.encode(payload, SESSION_SECRET, algorithm="HS256")


def current_claims(need_control: bool = False) -> dict[str, Any]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise AuthError("missing bearer token", 401)
    try:
        claims = jwt.decode(auth[7:], SESSION_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise AuthError("invalid or expired token", 401) from exc
    if not claims.get("member"):
        raise AuthError("not a guild member", 403)
    if need_control and not claims.get("can_control"):
        raise AuthError("insufficient permissions", 403)
    return claims


@app.after_request
def apply_cors(resp):
    if WEB_ORIGIN and request.path.startswith("/api/"):
        resp.headers["Access-Control-Allow-Origin"] = WEB_ORIGIN
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Vary"] = "Origin"
    return resp


@app.route("/api/<path:_unused>", methods=["OPTIONS"])
def api_preflight(_unused):
    return "", 204


@app.get("/auth/discord/login")
def discord_login():
    if not (DISCORD_CLIENT_ID and DISCORD_REDIRECT_URI and WEB_ORIGIN and SESSION_SECRET):
        return "OAuth web login is not configured", 500

    state = secrets.token_urlsafe(24)
    params = urllib.parse.urlencode(
        {
            "client_id": DISCORD_CLIENT_ID,
            "redirect_uri": DISCORD_REDIRECT_URI,
            "response_type": "code",
            "scope": "identify",
            "state": state,
        }
    )
    resp = redirect(f"https://discord.com/oauth2/authorize?{params}")
    resp.set_cookie(
        "oauth_state", state, max_age=300, httponly=True, secure=True, samesite="Lax"
    )
    return resp


@app.get("/auth/discord/callback")
def discord_callback():
    error = request.args.get("error")
    if error:
        return f"Discord OAuth error: {error}", 400

    state = request.args.get("state", "")
    if not state or state != request.cookies.get("oauth_state"):
        return "invalid oauth state", 400

    code = request.args.get("code", "")
    if not code:
        return "missing code", 400

    try:
        token_data = discord_token_exchange(code)
        me = discord_get("/users/@me", user_token=token_data["access_token"])
        uid = me["id"]
        try:
            member = discord_get(f"/guilds/{DISCORD_GUILD_ID}/members/{uid}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return "No perteneces al servidor de Discord.", 403
            raise
        roles = set(member.get("roles") or [])
        can_control = bool(uid in ALLOWED_USER_IDS or (ALLOWED_ROLE_IDS & roles))
        if not can_control:
            perms = member_permissions_from_roles(roles)
            can_control = bool(perms & ADMINISTRATOR)
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError) as exc:
        return f"Error de autenticacion: {exc}", 502

    token = create_session_token(uid, me.get("username", uid), can_control)
    resp = redirect(f"{WEB_APP_URL}#token={token}")
    resp.delete_cookie("oauth_state")
    return resp


@app.get("/api/status")
def api_status():
    try:
        current_claims()
    except AuthError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify(status_payload())


@app.get("/api/logs")
def api_logs():
    # Control-level only: the raw server log includes player IPs on join.
    try:
        current_claims(need_control=True)
    except AuthError as exc:
        return jsonify({"error": str(exc)}), exc.status

    instance = instance_get()
    if instance.get("status") != "RUNNING":
        return jsonify({"lines": [], "vm_status": instance.get("status", "UNKNOWN")})

    ip = external_ip(instance)
    if not ip:
        return jsonify({"lines": [], "vm_status": "RUNNING", "error": "sin IP externa"})

    lines = min(max(int(request.args.get("lines", "200")), 1), 500)
    # The log endpoint on the VM uses a self-signed cert; skip verification
    # but keep TLS so the shared secret isn't sent in cleartext.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        f"https://{ip}:8090/tail?lines={lines}",
        headers={"X-Log-Secret": NOTIFY_SECRET, "User-Agent": "mc-discord-control"},
    )
    try:
        with urllib.request.urlopen(req, timeout=6, context=ctx) as resp:
            data = json.loads(resp.read())
    except Exception:
        return jsonify({"lines": [], "vm_status": "RUNNING", "error": "el servidor de logs no responde"})
    data["vm_status"] = "RUNNING"
    return jsonify(data)


@app.get("/api/ip")
def api_ip():
    try:
        current_claims()
    except AuthError as exc:
        return jsonify({"error": str(exc)}), exc.status
    instance = instance_get()
    address = CUSTOM_DOMAIN or external_ip(instance) or ""
    return jsonify({"address": address, "port": MINECRAFT_PORT})


@app.post("/api/start")
def api_start():
    try:
        claims = current_claims(need_control=True)
    except AuthError as exc:
        return jsonify({"error": str(exc)}), exc.status
    instance = instance_get()
    if instance.get("status") == "RUNNING":
        return jsonify({"message": "La VM ya esta encendida. Si Minecraft no aparece, espera a que termine de cargar."})
    instance_start()
    cleanup_join_leave_messages()
    actor = claims.get("username") or "alguien (web)"
    notify_action(actor, "Encendido")
    sync_status_card(actor, "Encendido")
    return jsonify({"message": "Encendiendo la VM. El modpack puede tardar 3-8 minutos en quedar listo."})


@app.post("/api/stop")
def api_stop():
    try:
        claims = current_claims(need_control=True)
    except AuthError as exc:
        return jsonify({"error": str(exc)}), exc.status
    instance = instance_get()
    if instance.get("status") != "RUNNING":
        return jsonify({"message": f"La VM ya esta {instance.get('status', 'apagada')}."})
    instance_stop()
    cleanup_join_leave_messages()
    actor = claims.get("username") or "alguien (web)"
    notify_action(actor, "Apagado")
    sync_status_card(actor, "Apagado")
    return jsonify({"message": "Apagando la VM. El servicio de la VM guarda Minecraft antes de cortar energia."})
