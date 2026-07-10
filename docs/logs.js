(function () {
  const API = window.MC_CONTROL_API;
  const logBox = document.getElementById("log-box");
  const statusEl = document.getElementById("log-status");
  const btnPause = document.getElementById("btn-pause");

  const POLL_MS = 3000;
  let paused = false;
  let timer = null;

  function getToken() {
    return localStorage.getItem("mc_token");
  }

  function classify(line) {
    if (/\/(WARN|WARNING)\]/.test(line)) return "warn";
    if (/\/(ERROR|FATAL)\]/.test(line)) return "err";
    if (/\]: <|\[Server thread\/INFO\]: \* /.test(line)) return "chat";
    return "";
  }

  function render(lines) {
    const nearBottom =
      logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 60;
    logBox.textContent = "";
    for (const line of lines) {
      const span = document.createElement("span");
      const cls = classify(line);
      if (cls) span.className = cls;
      span.textContent = line + "\n";
      logBox.appendChild(span);
    }
    if (nearBottom) logBox.scrollTop = logBox.scrollHeight;
  }

  async function poll() {
    if (paused) return;
    const token = getToken();
    if (!token) {
      statusEl.textContent = "Sin sesion";
      logBox.textContent =
        "No has iniciado sesion. Anda al panel, inicia sesion con Discord y volve a esta pagina.";
      return;
    }
    try {
      const resp = await fetch(`${API}/api/logs?lines=300`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      const data = await resp.json();
      if (resp.status === 401) {
        statusEl.textContent = "Sesion expirada";
        logBox.textContent =
          "Sesion expirada. Anda al panel, inicia sesion de nuevo y volve a esta pagina.";
        return;
      }
      if (resp.status === 403) {
        statusEl.textContent = "Sin permiso";
        logBox.textContent =
          "Solo los administradores o el rol Minecraft pueden ver los logs.";
        return;
      }
      if (data.vm_status && data.vm_status !== "RUNNING") {
        statusEl.textContent = "VM apagada";
        logBox.textContent = "El servidor esta apagado. No hay logs en vivo.";
        return;
      }
      if (data.error) {
        statusEl.textContent = data.error;
        return;
      }
      statusEl.textContent = `En vivo - ${new Date().toLocaleTimeString()}`;
      render(data.lines || []);
    } catch (err) {
      statusEl.textContent = "Error de conexion, reintentando...";
    }
  }

  btnPause.addEventListener("click", () => {
    paused = !paused;
    btnPause.textContent = paused ? "Reanudar" : "Pausar";
    statusEl.textContent = paused ? "Pausado" : "Reanudando...";
    if (!paused) poll();
  });

  poll();
  timer = setInterval(poll, POLL_MS);
})();
