(function () {
  const API = window.MC_CONTROL_API;

  const loginLink = document.getElementById("login-link");
  const loggedOut = document.getElementById("logged-out");
  const loggedIn = document.getElementById("logged-in");
  const usernameEl = document.getElementById("username");
  const statusOff = document.getElementById("status-off");
  const statusOffTitle = document.getElementById("status-off-title");
  const steps = document.getElementById("steps");
  const stepPower = document.getElementById("step-power");
  const stepWorld = document.getElementById("step-world");
  const stepReady = document.getElementById("step-ready");
  const statusDetail = document.getElementById("status-detail");
  const messageEl = document.getElementById("message");
  const btnStart = document.getElementById("btn-start");
  const btnStop = document.getElementById("btn-stop");
  const btnRefresh = document.getElementById("btn-refresh");
  const btnLogout = document.getElementById("logout");

  const POLL_FAST_MS = 6000; // while the VM/world is booting
  const POLL_SLOW_MS = 25000; // steady state (on or off)
  const BOOT_TIMER_KEY = "mc_boot_started_at";

  let pollTimer = null;
  let tickTimer = null;

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

  function setStep(el, state) {
    el.className = state === "pending" ? "step" : `step step-${state}`;
  }

  function formatElapsed(ms) {
    const totalSeconds = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(totalSeconds / 60);
    const s = totalSeconds % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  function getBootStart() {
    const raw = localStorage.getItem(BOOT_TIMER_KEY);
    return raw ? parseInt(raw, 10) : null;
  }

  function setBootStart(ts) {
    localStorage.setItem(BOOT_TIMER_KEY, String(ts));
  }

  function clearBootStart() {
    localStorage.removeItem(BOOT_TIMER_KEY);
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

  function renderTick() {
    const start = getBootStart();
    if (start === null || !statusDetail.dataset.template) return;
    statusDetail.textContent = statusDetail.dataset.template.replace(
      "{elapsed}",
      formatElapsed(Date.now() - start)
    );
  }

  function schedulePoll(delayMs) {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(refreshStatus, delayMs);
  }

  async function refreshStatus() {
    if (!getToken()) return;
    try {
      const data = await apiFetch("/api/status");
      applyStatus(data);
    } catch (err) {
      statusDetail.textContent = "";
      statusDetail.dataset.template = "";
      showMessage(err.message, true);
      schedulePoll(POLL_SLOW_MS);
    }
  }

  function applyStatus(data) {
    if (tickTimer) clearInterval(tickTimer);

    if (data.vm_status !== "RUNNING") {
      statusOff.classList.remove("hidden");
      steps.classList.add("hidden");
      statusDetail.classList.add("hidden");
      statusOffTitle.textContent = "Apagado";
      clearBootStart();
      schedulePoll(POLL_SLOW_MS);
      return;
    }

    statusOff.classList.add("hidden");
    steps.classList.remove("hidden");
    statusDetail.classList.remove("hidden");
    setStep(stepPower, "done");

    if (!data.minecraft_online) {
      if (getBootStart() === null) setBootStart(Date.now());
      setStep(stepWorld, "active");
      setStep(stepReady, "pending");
      statusDetail.dataset.template =
        "Puede tardar 3-8 minutos en total. Llevas {elapsed}.";
      renderTick();
      tickTimer = setInterval(renderTick, 1000);
      schedulePoll(POLL_FAST_MS);
      return;
    }

    clearBootStart();
    setStep(stepWorld, "done");
    setStep(stepReady, "done");
    statusDetail.dataset.template = "";
    if (data.status_unknown) {
      statusDetail.textContent =
        `${data.address}:${data.port} · el servidor no informa jugadores/version (ping de estado deshabilitado), pero el puerto responde.`;
    } else {
      const version = data.version ? ` · v${data.version}` : "";
      statusDetail.textContent =
        `${data.address}:${data.port} · Jugadores: ${data.players_online}/${data.players_max}${version}`;
    }
    schedulePoll(POLL_SLOW_MS);
  }

  async function handleAction(path, busyText) {
    showMessage(busyText, false);
    btnStart.disabled = true;
    btnStop.disabled = true;
    if (path === "/api/start") setBootStart(Date.now());
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
  btnStop.addEventListener("click", () => {
    clearBootStart();
    handleAction("/api/stop", "Apagando...");
  });
  btnRefresh.addEventListener("click", () => {
    statusDetail.textContent = "Cargando estado...";
    refreshStatus();
  });
  btnLogout.addEventListener("click", () => {
    clearToken();
    clearBootStart();
    render();
  });

  function render() {
    if (pollTimer) clearTimeout(pollTimer);
    if (tickTimer) clearInterval(tickTimer);

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
    const linkLogs = document.getElementById("link-logs");
    if (linkLogs) linkLogs.style.display = claims.can_control ? "" : "none";
    setStep(stepPower, "pending");
    setStep(stepWorld, "pending");
    setStep(stepReady, "pending");
    statusDetail.textContent = "Cargando estado...";
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
})();
