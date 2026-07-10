(function () {
  const API = window.MC_CONTROL_API;

  const loginLink = document.getElementById("login-link");
  const loggedOut = document.getElementById("logged-out");
  const loggedIn = document.getElementById("logged-in");
  const usernameEl = document.getElementById("username");
  const statusEl = document.getElementById("status");
  const messageEl = document.getElementById("message");
  const btnStart = document.getElementById("btn-start");
  const btnStop = document.getElementById("btn-stop");
  const btnRefresh = document.getElementById("btn-refresh");
  const btnLogout = document.getElementById("logout");

  loginLink.href = `${API}/auth/discord/login`;

  function getToken() {
    return localStorage.getItem("mc_token");
  }

  function setToken(token) {
    localStorage.setItem("mc_token", token);
  }

  function clearToken() {
    localStorage.removeItem("mc_token");
  }

  function decodeToken(token) {
    const payload = token.split(".")[1];
    const normalized = payload.replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(normalized));
  }

  function showMessage(text, isError) {
    messageEl.textContent = text || "";
    messageEl.className = isError ? "message error" : "message";
  }

  async function apiFetch(path, options) {
    options = options || {};
    const token = getToken();
    const headers = Object.assign({}, options.headers, {
      Authorization: `Bearer ${token}`,
    });
    const resp = await fetch(`${API}${path}`, Object.assign({}, options, { headers }));
    if (resp.status === 401) {
      clearToken();
      render();
      throw new Error("Sesion expirada, inicia sesion de nuevo.");
    }
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || "Error inesperado.");
    }
    return data;
  }

  async function refreshStatus() {
    try {
      const data = await apiFetch("/api/status");
      const lines = [`VM: ${data.vm_status}`];
      if (data.minecraft_online) {
        lines.push(`Minecraft activo en ${data.address}:${data.port}`);
        lines.push(`Jugadores: ${data.players_online}/${data.players_max}`);
        if (data.version) lines.push(`Version: ${data.version}`);
      } else if (data.vm_status === "RUNNING") {
        lines.push("Minecraft aun no responde, espera unos minutos.");
      } else {
        lines.push("Minecraft esta apagado.");
      }
      statusEl.textContent = lines.join("\n");
    } catch (err) {
      statusEl.textContent = "";
      showMessage(err.message, true);
    }
  }

  async function handleAction(path, busyText) {
    showMessage(busyText, false);
    btnStart.disabled = true;
    btnStop.disabled = true;
    try {
      const data = await apiFetch(path, { method: "POST" });
      showMessage(data.message, false);
      await refreshStatus();
    } catch (err) {
      showMessage(err.message, true);
    } finally {
      btnStart.disabled = false;
      btnStop.disabled = false;
    }
  }

  btnStart.addEventListener("click", () => handleAction("/api/start", "Encendiendo..."));
  btnStop.addEventListener("click", () => handleAction("/api/stop", "Apagando..."));
  btnRefresh.addEventListener("click", () => {
    statusEl.textContent = "Cargando estado...";
    refreshStatus();
  });
  btnLogout.addEventListener("click", () => {
    clearToken();
    render();
  });

  function render() {
    const token = getToken();
    if (!token) {
      loggedOut.classList.remove("hidden");
      loggedIn.classList.add("hidden");
      return;
    }

    let claims;
    try {
      claims = decodeToken(token);
    } catch (err) {
      clearToken();
      loggedOut.classList.remove("hidden");
      loggedIn.classList.add("hidden");
      return;
    }

    loggedOut.classList.add("hidden");
    loggedIn.classList.remove("hidden");
    usernameEl.textContent = claims.username || claims.uid;
    btnStart.style.display = claims.can_control ? "" : "none";
    btnStop.style.display = claims.can_control ? "" : "none";
    statusEl.textContent = "Cargando estado...";
    showMessage("", false);
    refreshStatus();
  }

  function consumeTokenFromHash() {
    const match = location.hash.match(/token=([^&]+)/);
    if (match) {
      setToken(decodeURIComponent(match[1]));
      history.replaceState(null, "", location.pathname + location.search);
    }
  }

  consumeTokenFromHash();
  render();

  setInterval(() => {
    if (getToken()) refreshStatus();
  }, 20000);
})();
