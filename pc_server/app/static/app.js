(() => {
  const sessionKey = "session_id";

  function wsUrl(path) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}${path}`;
  }

  function setStatus(el, text, state) {
    if (!el) return;
    el.textContent = text;
    el.classList.remove("ok", "error");
    if (state === "ok") el.classList.add("ok");
    if (state === "error") el.classList.add("error");
  }

  function appendLog(el, line, autoScroll) {
    if (!el) return;
    el.textContent += line + "\n";
    if (autoScroll) {
      el.scrollTop = el.scrollHeight;
    }
  }

  async function jsonFetch(url, options = {}) {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || err.message || res.statusText);
    }
    return res.json();
  }

  const demoId = document.body.dataset.demoId;

  if (demoId) {
    const host = document.getElementById("host");
    const port = document.getElementById("port");
    const username = document.getElementById("username");
    const password = document.getElementById("password");
    const sudoPassword = document.getElementById("sudoPassword");
    const btnConnect = document.getElementById("btn-connect");
    const connectStatus = document.getElementById("connect-status");
    const btnSudo = document.getElementById("btn-sudo");
    const sudoStatus = document.getElementById("sudo-status");

    const btnDeploy = document.getElementById("btn-deploy");
    const btnCancel = document.getElementById("btn-cancel");
    const deployStatus = document.getElementById("deploy-status");
    const deployLog = document.getElementById("deploy-log");
    const autoScroll = document.getElementById("auto-scroll");

    const btnRun = document.getElementById("btn-run");
    const btnStop = document.getElementById("btn-stop");
    const runStatus = document.getElementById("run-status");
    const runLog = document.getElementById("run-log");
    const runError = document.getElementById("run-error");
    const video = document.getElementById("video");

    let jobSocket = null;
    let runSocket = null;
    let currentRunId = null;
    let pendingVideoUrl = null;

    async function refreshDeployStatus() {
      const sessionId = localStorage.getItem(sessionKey);
      if (!sessionId) return;
      try {
        const data = await jsonFetch(`/api/session/${sessionId}/demo/${demoId}/status`);
        if (data.deployed) {
          setStatus(deployStatus, "部署完成 ✅", "ok");
          btnRun.disabled = false;
        }
      } catch (_) {}
    }

    btnConnect.addEventListener("click", async () => {
      const h = (host.value || "").trim();
      if (!h) {
        setStatus(connectStatus, "请输入 Jetson IP", "error");
        return;
      }
      btnConnect.disabled = true;
      setStatus(connectStatus, "连接中...", "");
      try {
        const data = await jsonFetch("/api/session/connect", {
          method: "POST",
          body: JSON.stringify({
            ip: h,
            port: parseInt(port.value || "22", 10),
            username: (username.value || "seeed").trim(),
            password: (password.value || "").trim(),
            sudo_password: (sudoPassword.value || "").trim() || null,
          }),
        });
        localStorage.setItem(sessionKey, data.session_id);
        setStatus(connectStatus, "连接成功", "ok");
        btnSudo.disabled = false;
        if ((sudoPassword.value || "").trim()) {
          setStatus(sudoStatus, "sudo 密码已设置", "ok");
        }
        btnDeploy.disabled = false;
        await refreshDeployStatus();
      } catch (err) {
        setStatus(connectStatus, `连接失败: ${err.message}`, "error");
      } finally {
        btnConnect.disabled = false;
      }
    });

    btnSudo.addEventListener("click", async () => {
      const sessionId = localStorage.getItem(sessionKey);
      if (!sessionId) return;
      btnSudo.disabled = true;
      try {
        await jsonFetch(`/api/session/${sessionId}/sudo`, {
          method: "POST",
          body: JSON.stringify({
            sudo_password: (sudoPassword.value || "").trim() || null,
          }),
        });
        setStatus(sudoStatus, "sudo 密码已更新", "ok");
      } catch (err) {
        setStatus(sudoStatus, `更新失败: ${err.message}`, "error");
      } finally {
        btnSudo.disabled = false;
      }
    });

    btnDeploy.addEventListener("click", async () => {
      const sessionId = localStorage.getItem(sessionKey);
      if (!sessionId) {
        setStatus(deployStatus, "请先连接 Jetson", "error");
        return;
      }
      deployLog.textContent = "";
      runError.hidden = true;
      btnDeploy.disabled = true;
      btnCancel.disabled = false;
      setStatus(deployStatus, "部署中...", "");
      try {
        const data = await jsonFetch(`/api/session/${sessionId}/deploy/${demoId}`, { method: "POST" });
        const ws = new WebSocket(wsUrl(data.ws_url));
        jobSocket = ws;
        ws.onmessage = (event) => {
          const msg = JSON.parse(event.data);
          if (msg.type === "log") {
            appendLog(deployLog, msg.data, autoScroll.checked);
          } else if (msg.type === "status") {
            if (msg.data === "DONE") {
              setStatus(deployStatus, "部署完成 ✅", "ok");
              btnRun.disabled = false;
              btnDeploy.disabled = false;
              btnCancel.disabled = true;
            } else if (msg.data === "FAILED") {
              setStatus(deployStatus, `部署失败 (exit=${msg.exit_code ?? "?"})`, "error");
              btnDeploy.disabled = false;
              btnCancel.disabled = true;
            } else if (msg.data === "CANCELLED") {
              setStatus(deployStatus, "部署已取消", "error");
              btnDeploy.disabled = false;
              btnCancel.disabled = true;
            } else {
              setStatus(deployStatus, msg.data, "");
            }
          }
        };
        ws.onclose = () => {
          btnCancel.disabled = true;
        };
      } catch (err) {
        setStatus(deployStatus, `部署启动失败: ${err.message}`, "error");
        btnDeploy.disabled = false;
        btnCancel.disabled = true;
      }
    });

    btnCancel.addEventListener("click", async () => {
      const sessionId = localStorage.getItem(sessionKey);
      if (!sessionId) return;
      btnCancel.disabled = true;
      try {
        await jsonFetch(`/api/session/${sessionId}/deploy/${demoId}/cancel`, { method: "POST" });
        setStatus(deployStatus, "已发送取消", "");
        if (jobSocket) jobSocket.close();
      } catch (err) {
        setStatus(deployStatus, `取消失败: ${err.message}`, "error");
      }
    });

    btnRun.addEventListener("click", async () => {
      const sessionId = localStorage.getItem(sessionKey);
      if (!sessionId) return;
      runLog.textContent = "";
      runError.hidden = true;
      btnRun.disabled = true;
      btnStop.disabled = true;
      setStatus(runStatus, "启动中...", "");
      try {
        const data = await jsonFetch(`/api/session/${sessionId}/run/${demoId}`, { method: "POST" });
        currentRunId = data.run_id;
        pendingVideoUrl = data.video_url + `?t=${Date.now()}`;
        btnStop.disabled = false;
        const ws = new WebSocket(wsUrl(data.ws_url));
        runSocket = ws;
        ws.onmessage = (event) => {
          const msg = JSON.parse(event.data);
          if (msg.type === "log") {
            appendLog(runLog, msg.data, true);
          } else if (msg.type === "status") {
            if (msg.data === "RUNNING") {
              setStatus(runStatus, "运行中", "ok");
              btnRun.disabled = true;
              if (pendingVideoUrl) {
                video.src = pendingVideoUrl;
              }
            } else if (msg.data === "ERROR") {
              setStatus(runStatus, "启动失败", "error");
              btnRun.disabled = false;
              btnStop.disabled = true;
              video.src = "";
            } else if (msg.data === "STOPPED") {
              setStatus(runStatus, "已停止", "");
              btnRun.disabled = false;
              btnStop.disabled = true;
              video.src = "";
            } else {
              setStatus(runStatus, msg.data, "");
            }
          }
        };
        ws.onclose = () => {
          btnStop.disabled = true;
        };
        video.onerror = () => {
          runError.hidden = false;
          runError.textContent = "视频流连接失败，请检查摄像头与 Jetson 推理服务是否正常。";
        };
      } catch (err) {
        setStatus(runStatus, `启动失败: ${err.message}`, "error");
        btnRun.disabled = false;
        btnStop.disabled = true;
      }
    });

    btnStop.addEventListener("click", async () => {
      const sessionId = localStorage.getItem(sessionKey);
      if (!sessionId || !currentRunId) return;
      btnStop.disabled = true;
      try {
        await jsonFetch(`/api/session/${sessionId}/stop/${currentRunId}`, { method: "POST" });
        video.src = "";
        setStatus(runStatus, "已停止", "");
        btnRun.disabled = false;
        if (runSocket) runSocket.close();
      } catch (err) {
        setStatus(runStatus, `停止失败: ${err.message}`, "error");
      }
    });

    refreshDeployStatus();
  }
})();
