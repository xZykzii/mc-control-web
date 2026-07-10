# Control web del servidor de Minecraft

Pagina web (GitHub Pages) que prende/apaga la misma VM de Google Cloud que
controlaba el bot de Discord, pero con botones en el navegador en vez de
comandos `/mc`. El backend (Cloud Run) es el mismo Flask app extendido: ahora
tambien acepta login con Discord y expone una API JSON ademas del endpoint de
interacciones de Discord.

```
mc-control-web/
  backend/   -> Flask app en Cloud Run (VM + Discord + login + API)
  docs/      -> sitio estatico para GitHub Pages (nombre "docs" porque
               GitHub Pages solo permite servir desde la raiz o /docs)
```

## Por que hace falta un backend

GitHub Pages solo sirve HTML/JS estatico: no puede guardar las credenciales
de Google Cloud ni el secreto del bot de Discord. Por eso la pagina llama a
un backend en Cloud Run (`backend/`) que es quien de verdad prende/apaga la
VM. La pagina en si no tiene ningun secreto.

## Como funciona el login

1. El usuario hace click en "Iniciar sesion con Discord" -> lo manda a
   `backend` (`/auth/discord/login`), que redirige a Discord.
2. Discord confirma la identidad y vuelve a `backend`
   (`/auth/discord/callback`).
3. El backend revisa, usando el bot de Discord, si ese usuario **pertenece al
   servidor** y si tiene el rol o esta en la lista de `ALLOWED_USER_IDS` /
   `ALLOWED_ROLE_IDS` (o permiso de Administrador) para poder usar
   start/stop.
4. El backend genera un token firmado (JWT) y redirige de vuelta a la pagina
   con el token en la URL. La pagina lo guarda en `localStorage` y lo manda
   en cada request (`Authorization: Bearer ...`).
5. `status`/`ip` requieren estar logueado (ser miembro del server).
   `start`/`stop` ademas requieren el rol permitido.

El token expira solo (12 horas por defecto, `SESSION_TTL_SECONDS`); no hay
sesiones guardadas en el servidor.

## 1. Configurar la app de Discord

En el [Discord Developer Portal](https://discord.com/developers/applications),
en tu aplicacion existente (la del bot):

- **OAuth2 > General**: copia el `CLIENT ID` y genera/copia el
  `CLIENT SECRET`.
- **OAuth2 > Redirects**: agrega
  `https://TU-SERVICIO-xxxxx.a.run.app/auth/discord/callback` (la URL exacta
  de Cloud Run; se conoce despues del primer deploy, ver paso 3).

## 2. Variables de entorno nuevas (ademas de las que ya usaba el bot)

| Variable | Que es |
|---|---|
| `DISCORD_CLIENT_ID` | Client ID de la app de Discord |
| `DISCORD_CLIENT_SECRET` | Client secret de la app de Discord |
| `DISCORD_GUILD_ID` | ID del servidor de Discord (para validar membresia) |
| `DISCORD_REDIRECT_URI` | `https://TU-SERVICIO-xxxxx.a.run.app/auth/discord/callback` |
| `SESSION_SECRET` | Secreto aleatorio para firmar los tokens de sesion (`openssl rand -hex 32`) |
| `WEB_ORIGIN` | Origen de tu GitHub Pages para CORS, ej. `https://tu-usuario.github.io` (sin barra final, sin la ruta del repo) |
| `WEB_APP_URL` | URL completa de la pagina, incluyendo la ruta del repo si aplica, ej. `https://tu-usuario.github.io/mc-control-web`. Ahi es a donde se redirige despues del login. Si no la seteas, usa `WEB_ORIGIN`. |

## 3. Deploy del backend (Cloud Run)

Primer deploy (sin `DISCORD_REDIRECT_URI`/`WEB_ORIGIN`/`WEB_APP_URL` todavia,
porque no conoces la URL de Cloud Run hasta que despliegas):

```bash
cd backend
export DISCORD_PUBLIC_KEY="..."
export DISCORD_BOT_TOKEN="..."
export DISCORD_NOTIFY_CHANNEL_ID="123456789012345678"
export NOTIFY_SECRET="$(openssl rand -hex 32)"
export SESSION_SECRET="$(openssl rand -hex 32)"
export ALLOWED_ROLE_IDS="123456789012345678"
export DISCORD_CLIENT_ID="..."
export DISCORD_CLIENT_SECRET="..."
export DISCORD_GUILD_ID="123456789012345678"
./deploy-cloud-run.sh
```

El script imprime la URL de Cloud Run al final, por ejemplo
`https://mc-discord-control-xxxxx.a.run.app`. Con esa URL:

1. Vuelve al Developer Portal y agrega el redirect
   `https://mc-discord-control-xxxxx.a.run.app/auth/discord/callback`.
2. Actualiza el servicio con las variables que faltaban:

```bash
gcloud run services update mc-discord-control --region us-central1 \
  --update-env-vars "DISCORD_REDIRECT_URI=https://mc-discord-control-xxxxx.a.run.app/auth/discord/callback,WEB_ORIGIN=https://tu-usuario.github.io,WEB_APP_URL=https://tu-usuario.github.io/mc-control-web"
```

Registra los comandos de Discord como antes:

```bash
python3 -m pip install -r requirements-register.txt
export DISCORD_APPLICATION_ID="..."
export DISCORD_BOT_TOKEN="..."
export DISCORD_GUILD_ID="..."
python3 register_commands.py
```

## 4. Publicar la pagina en GitHub Pages

1. Sube este repo a GitHub.
2. Edita [`docs/config.js`](docs/config.js) y pon la URL real de tu servicio
   de Cloud Run.
3. En GitHub: **Settings > Pages > Source > Deploy from a branch**, elige la
   rama (ej. `master`) y la carpeta `/docs`.
4. Tu pagina va a quedar en `https://tu-usuario.github.io/<repo>/`. Si el
   repo se llama distinto, el `WEB_ORIGIN` del backend sigue siendo solo
   `https://tu-usuario.github.io` (el origen no incluye la ruta del repo).

## Notas de seguridad

- El backend valida la firma de cada request de Discord y el JWT de cada
  request de la web; la pagina en si no contiene secretos.
- El service account de Cloud Run solo deberia poder controlar la VM de
  Minecraft. Si en tu proyecto de GCP hay otras VMs, conviene reemplazar el
  rol amplio `roles/compute.instanceAdmin.v1` por un rol custom limitado a
  `compute.instances.start/stop/get` con una condicion IAM que lo restrinja a
  esa instancia.
- Los tokens de sesion duran `SESSION_TTL_SECONDS` (12h por defecto) y no se
  pueden revocar antes de tiempo salvo cambiando `SESSION_SECRET` (eso
  invalida todas las sesiones activas).
