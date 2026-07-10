import json
import os
import secrets
import socket
import struct
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
                    "style": BUTTON_SECONDARY,
                    "label": "Estado",
                    "custom_id": "mc_status",
                    "emoji": {"name": "\U0001f504"},
                },
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
    content: str,
    ephemeral: bool = False,
    with_buttons: bool = True,
    response_type: int = CHANNEL_MESSAGE,
):
    data = {"content": content}
    if ephemeral:
        data["flags"] = EPHEMERAL
    if with_buttons:
        data["components"] = mc_buttons()
    return jsonify({"type": response_type, "data": data})


def send_channel_message(content: str):
    if not DISCORD_BOT_TOKEN or not DISCORD_NOTIFY_CHANNEL_ID:
        raise RuntimeError("DISCORD_BOT_TOKEN or DISCORD_NOTIFY_CHANNEL_ID is not configured")

    payload = json.dumps({"content": content}).encode("utf-8")
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
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


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
    except BadSignatureError as exc:
        raise PermissionError("bad Discord signature") from exc


def instance_get() -> dict[str, Any]:
    return (
        compute.instances()
        .get(project=PROJECT_ID, zone=ZONE, instance=INSTANCE)
        .execute()
    )


def instance_start():
    return (
        compute.instances()
        .start(project=PROJECT_ID, zone=ZONE, instance=INSTANCE)
        .execute()
    )


def instance_stop():
    return (
        compute.instances()
        .stop(project=PROJECT_ID, zone=ZONE, instance=INSTANCE)
        .execute()
    )


def external_ip(instance: dict[str, Any]) -> str:
    for nic in instance.get("networkInterfaces", []):
        for access in nic.get("accessConfigs", []):
            if access.get("natIP"):
                return access["natIP"]
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
    with socket.create_connection((host, MINECRAFT_PORT), timeout=4) as sock:
        sock.settimeout(4)
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
        with socket.create_connection((host, MINECRAFT_PORT), timeout=3):
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
    try:
        mc = minecraft_status(ip)
        players = mc.get("players", {})
        data["minecraft_online"] = True
        data["players_online"] = players.get("online", 0)
        data["players_max"] = players.get("max")
        data["version"] = (mc.get("version") or {}).get("name")
    except Exception:
        # The server list ping can be disabled or too slow to answer even
        # though the game port itself accepts connections just fine (e.g.
        # enable-status=false in server.properties). Fall back to a plain
        # TCP check so the UI doesn't get stuck showing "starting" forever.
        if minecraft_port_open(ip):
            data["minecraft_online"] = True
            data["status_unknown"] = True
        else:
            data["minecraft_online"] = False
    return data


def status_text() -> str:
    data = status_payload()
    address = data["address"] or "sin IP externa"

    if data["vm_status"] != "RUNNING":
        return f"VM: `{data['vm_status']}`. Minecraft esta apagado. Usa `/mc start` para encenderlo."

    if data["minecraft_online"] and data.get("status_unknown"):
        return (
            f"VM: `RUNNING`.\n"
            f"Minecraft: el puerto `{address}:{data['port']}` responde, pero el servidor "
            "no contesta el ping de estado (jugadores/version desconocidos)."
        )

    if data["minecraft_online"]:
        return (
            f"VM: `RUNNING`.\n"
            f"Minecraft: activo en `{address}:{data['port']}`.\n"
            f"Jugadores: `{data.get('players_online', 0)}/{data.get('players_max', '?')}`.\n"
            f"Version: `{data.get('version') or 'desconocida'}`."
        )
    return (
        f"VM: `RUNNING`, pero Minecraft aun no responde en `{address}:{data['port']}`. "
        "Si se acaba de encender, espera unos minutos por el modpack."
    )


@app.get("/")
def health():
    return "ok"


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

    messages = {
        "server_open": "Minecraft esta abierto y listo para entrar.",
        "server_closing": "Minecraft se esta cerrando y guardando el mundo.",
        "server_closed": "Minecraft quedo cerrado. La VM puede apagarse para ahorrar credito.",
    }

    if event == "player_join" and player:
        suffix = f" Jugadores online: {online}." if online is not None else ""
        content = f"{player} entro al mundo.{suffix}"
    elif event == "player_leave" and player:
        suffix = f" Jugadores online: {online}." if online is not None else ""
        content = f"{player} salio del mundo.{suffix}"
    else:
        content = messages.get(event) or str(payload.get("message") or "Evento de Minecraft.")

    try:
        send_channel_message(content)
    except urllib.error.HTTPError as exc:
        return f"discord error {exc.code}: {exc.read().decode('utf-8', 'ignore')}", 502
    return "ok"


def mc_action(command: str, payload: dict) -> tuple[str, bool]:
    """Runs an /mc subcommand or button click. Returns (content, ephemeral)."""
    if command == "status":
        return status_text(), False

    if command == "ip":
        instance = instance_get()
        ip = external_ip(instance)
        address = CUSTOM_DOMAIN or ip or "sin IP externa"
        return f"Direccion del servidor: `{address}:{MINECRAFT_PORT}`", False

    if command in {"start", "stop"} and not member_can_control(payload):
        return "No tienes permiso para controlar la VM.", True

    if command == "start":
        instance = instance_get()
        if instance.get("status") == "RUNNING":
            return "La VM ya esta encendida. Si Minecraft no aparece, espera a que termine de cargar.", False
        instance_start()
        return "Encendiendo la VM. El modpack puede tardar 3-8 minutos en quedar listo.", False

    if command == "stop":
        instance = instance_get()
        if instance.get("status") != "RUNNING":
            return f"La VM ya esta `{instance.get('status', 'apagada')}`.", False
        instance_stop()
        return "Apagando la VM. El servicio de la VM guarda Minecraft antes de cortar energia.", False

    return "Comando desconocido.", True


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
        content, ephemeral = mc_action(command_name(payload), payload)
        return response(content, ephemeral=ephemeral, with_buttons=not ephemeral)

    if itype == MESSAGE_COMPONENT:
        custom_id = (payload.get("data") or {}).get("custom_id", "")
        command = custom_id[3:] if custom_id.startswith("mc_") else ""
        content, ephemeral = mc_action(command, payload)
        # Permission-denied clicks get a private reply and leave the shared
        # panel message untouched; everything else updates it in place.
        response_type = CHANNEL_MESSAGE if ephemeral else UPDATE_MESSAGE
        return response(content, ephemeral=ephemeral, with_buttons=not ephemeral, response_type=response_type)

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
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


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
        current_claims(need_control=True)
    except AuthError as exc:
        return jsonify({"error": str(exc)}), exc.status
    instance = instance_get()
    if instance.get("status") == "RUNNING":
        return jsonify({"message": "La VM ya esta encendida. Si Minecraft no aparece, espera a que termine de cargar."})
    instance_start()
    return jsonify({"message": "Encendiendo la VM. El modpack puede tardar 3-8 minutos en quedar listo."})


@app.post("/api/stop")
def api_stop():
    try:
        current_claims(need_control=True)
    except AuthError as exc:
        return jsonify({"error": str(exc)}), exc.status
    instance = instance_get()
    if instance.get("status") != "RUNNING":
        return jsonify({"message": f"La VM ya esta {instance.get('status', 'apagada')}."})
    instance_stop()
    return jsonify({"message": "Apagando la VM. El servicio de la VM guarda Minecraft antes de cortar energia."})
