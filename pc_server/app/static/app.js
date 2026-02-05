(() => {
  const sessionKey = "session_id";

  function wsUrl(path) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}${path}`;
  }

  function setBadge(el, text, state) {
    if (!el) return;
    el.textContent = text;
    el.classList.remove("ok", "warn", "error");
    if (state) el.classList.add(state);
  }

  function setStatusLine(el, text, state) {
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

  function updateRunAvailability() {
    if (!btnRun) return;
    if (!runEnabled) {
      btnRun.disabled = true;
      return;
    }
    if (runState === "RUNNING" || runState === "STARTING") {
      btnRun.disabled = true;
      return;
    }
    if (requireCameraCheck) {
      btnRun.disabled = !(deployReady && cameraReady);
      return;
    }
    btnRun.disabled = !deployReady;
  }

  function updateCameraCheckAvailability() {
    if (!btnCameraCheck) return;
    btnCameraCheck.disabled = !deployReady;
  }

  function resetCameraCheck(message = "未检测") {
    if (requireCameraCheck) {
      cameraReady = false;
      if (cameraStatus) setStatusLine(cameraStatus, message, "");
    }
    updateRunAvailability();
  }

  function updateWebuiUI(running) {
    if (!webuiActions) return;
    if (runType !== "webui") {
      webuiActions.hidden = true;
      return;
    }
    webuiActions.hidden = !running;
  }

  function updateWebuiLink(url) {
    if (webuiUrl) webuiUrl.textContent = url || "";
    if (!btnOpenWebui) return;
    if (url) {
      btnOpenWebui.setAttribute("href", url);
      btnOpenWebui.setAttribute("aria-disabled", "false");
      btnOpenWebui.classList.remove("btn-disabled");
    } else {
      btnOpenWebui.removeAttribute("href");
      btnOpenWebui.setAttribute("aria-disabled", "true");
      btnOpenWebui.classList.add("btn-disabled");
    }
  }

  function resolveWebuiUrl() {
    const remoteUrl = runInfo?.remote_url;
    if (remoteUrl) return remoteUrl;
    const scheme = runInfo?.scheme || "http";
    const remotePort = runInfo?.remote_port;
    const hostVal = (host?.value || "").trim();
    if (runType === "webui" && hostVal && remotePort) {
      return `${scheme}://${hostVal}:${remotePort}`;
    }
    const localUrl = runInfo?.local_url;
    if (localUrl) return localUrl;
    const localPort = runInfo?.local_port;
    if (localPort) return `${scheme}://127.0.0.1:${localPort}`;
    return "";
  }

  function setFieldError(input, errorEl, message) {
    if (errorEl) errorEl.textContent = message || "";
    if (input) input.classList.toggle("input-error", Boolean(message));
  }

  function isValidHost(value) {
    if (!value) return false;
    const ipRegex = /^(\d{1,3}\.){3}\d{1,3}$/;
    const hostRegex = /^[a-zA-Z0-9.-]+$/;
    return ipRegex.test(value) || hostRegex.test(value);
  }

  function isValidPort(value) {
    const port = Number(value);
    return Number.isInteger(port) && port > 0 && port <= 65535;
  }

  async function copyToClipboard(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand("copy");
        document.body.removeChild(area);
        return true;
      } catch (err) {
        document.body.removeChild(area);
        return false;
      }
    }
  }

  async function jsonFetch(url, options = {}) {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (!res.ok) {
      let err = {};
      try {
        err = await res.json();
      } catch (_) {}
      const detail = err.detail || err.message || res.statusText;
      const message = typeof detail === "string" ? detail : detail?.message || res.statusText;
      const error = new Error(message);
      error.data = detail;
      throw error;
    }
    return res.json();
  }

  const demoId = document.body.dataset.demoId;
  if (!demoId) return;

  const runEnabled = document.body.dataset.runEnabled === "true";
  const runType = document.body.dataset.runType || "";
  const requireCameraCheck = document.body.dataset.requireCamera === "true";
  const previewSrc = document.body.dataset.previewSrc || "";
  const defaultRemoteDir = document.body.dataset.remoteDir || "";

  const host = document.getElementById("host");
  const port = document.getElementById("port");
  const username = document.getElementById("username");
  const password = document.getElementById("password");
  const sudoPassword = document.getElementById("sudoPassword");
  const remoteDir = document.getElementById("remoteDir");

  const connectBadge = document.getElementById("connect-badge");
  const connectStatus = document.getElementById("connect-status");
  const sudoStatus = document.getElementById("sudo-status");

  const deployBadge = document.getElementById("deploy-badge");
  const deployStatus = document.getElementById("deploy-status");
  const precheckStatus = document.getElementById("precheck-status");
  const precheckBanner = document.getElementById("deploy-precheck");
  const precheckText = document.getElementById("precheck-text");

  const deployLogShell = document.getElementById("deploy-log-shell");
  const deployLog = document.getElementById("deploy-log");
  const autoScroll = document.getElementById("auto-scroll");

  const btnConnect = document.getElementById("btn-connect");
  const btnSudo = document.getElementById("btn-sudo");
  const btnDeploy = document.getElementById("btn-deploy");
  const btnCancel = document.getElementById("btn-cancel");
  const btnSkip = document.getElementById("btn-skip");
  const btnForce = document.getElementById("btn-force");

  const btnRun = document.getElementById("btn-run");
  const btnStop = document.getElementById("btn-stop");
  const runBadge = document.getElementById("run-badge");
  const runStatus = document.getElementById("run-status");
  const btnCameraCheck = document.getElementById("btn-camera-check");
  const cameraStatus = document.getElementById("camera-status");
  const webuiActions = document.getElementById("webui-actions");
  const btnOpenWebui = document.getElementById("btn-open-webui");
  const webuiUrl = document.getElementById("webui-url");
  const runErrorSide = document.getElementById("run-error-side");
  const runLog = document.getElementById("run-log");
  const runError = document.getElementById("run-error");
  const video = document.getElementById("video");

  const errHost = document.getElementById("err-host");
  const errPort = document.getElementById("err-port");
  const errUsername = document.getElementById("err-username");
  const errPassword = document.getElementById("err-password");
  const errSudo = document.getElementById("err-sudoPassword");
  const errRemoteDir = document.getElementById("err-remoteDir");

  const btnLogExpand = document.getElementById("btn-log-expand");
  const btnLogCopy = document.getElementById("btn-log-copy");
  const btnLogClear = document.getElementById("btn-log-clear");
  const logOverlay = document.getElementById("log-overlay");
  let logAnchor = null;

  let jobSocket = null;
  let runSocket = null;
  let currentRunId = null;
  let pendingVideoUrl = null;
  let deployReady = false;
  let cameraReady = false;
  let runState = "IDLE";
  let runInfo = {};

  if (remoteDir && defaultRemoteDir) {
    remoteDir.value = defaultRemoteDir;
  }

  if (video && previewSrc) {
    video.src = previewSrc;
  }

  if (!requireCameraCheck) {
    cameraReady = true;
    if (btnCameraCheck) btnCameraCheck.hidden = true;
    if (cameraStatus) cameraStatus.hidden = true;
  }

  function validateConnectForm() {
    const hostVal = (host.value || "").trim();
    const portVal = (port.value || "").trim();
    const userVal = (username.value || "").trim();

    setFieldError(host, errHost, "");
    setFieldError(port, errPort, "");
    setFieldError(username, errUsername, "");
    setFieldError(password, errPassword, "");
    setFieldError(sudoPassword, errSudo, "");

    let ok = true;
    if (!hostVal || !isValidHost(hostVal)) {
      setFieldError(host, errHost, "请输入合法的 IP / 主机名");
      ok = false;
    }
    if (!isValidPort(portVal)) {
      setFieldError(port, errPort, "端口范围 1-65535");
      ok = false;
    }
    if (!userVal) {
      setFieldError(username, errUsername, "请输入用户名");
      ok = false;
    }
    return ok;
  }

  function validateDeployForm() {
    if (!remoteDir) return true;
    const dirVal = (remoteDir.value || "").trim();
    setFieldError(remoteDir, errRemoteDir, "");
    if (!dirVal) {
      setFieldError(remoteDir, errRemoteDir, "远程目录不能为空");
      return false;
    }
    if (!dirVal.startsWith("/")) {
      setFieldError(remoteDir, errRemoteDir, "必须是绝对路径（以 / 开头）");
      return false;
    }
    if (/\s/.test(dirVal)) {
      setFieldError(remoteDir, errRemoteDir, "路径不能包含空格");
      return false;
    }
    return true;
  }

  async function refreshPrecheck() {
    const sessionId = localStorage.getItem(sessionKey);
    if (!sessionId || !validateDeployForm()) return null;
    try {
      const payload = { remote_dir: (remoteDir?.value || "").trim() || null };
      const data = await jsonFetch(`/api/session/${sessionId}/demo/${demoId}/precheck`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const installed = Boolean(data.installed);
      const when = data.installed_at ? `，${data.installed_at}` : "";
      const ver = data.version ? `，${data.version}` : "";

      if (installed) {
        deployReady = true;
        setStatusLine(precheckStatus, "已安装" + ver, "ok");
        setBadge(deployBadge, "Installed", "ok");
        if (precheckText) precheckText.textContent = `检测到已配置${when}${ver}`;
        if (precheckBanner) precheckBanner.hidden = false;
        if (btnSkip) btnSkip.disabled = !runEnabled;
        if (btnForce) btnForce.disabled = false;
        updateCameraCheckAvailability();
        updateRunAvailability();
      } else {
        deployReady = false;
        setStatusLine(precheckStatus, "未安装", "");
        if (precheckBanner) precheckBanner.hidden = true;
        setBadge(deployBadge, "Idle", "");
        resetCameraCheck();
        updateCameraCheckAvailability();
      }
      return data;
    } catch (err) {
      setStatusLine(precheckStatus, `检查失败: ${err.message}`, "error");
      if (precheckBanner) precheckBanner.hidden = true;
      deployReady = false;
      resetCameraCheck("未检测");
      updateCameraCheckAvailability();
      if (String(err.message || "").includes("session not found")) {
        localStorage.removeItem(sessionKey);
        setBadge(connectBadge, "Not connected", "");
        setStatusLine(connectStatus, "未连接", "");
        btnDeploy.disabled = true;
      }
      return null;
    }
  }

  async function startDeploy(force = false) {
    const sessionId = localStorage.getItem(sessionKey);
    if (!sessionId) {
      setStatusLine(deployStatus, "请先连接 Jetson", "error");
      return;
    }
    if (!validateDeployForm()) return;

    deployLog.textContent = "";
    if (runError) runError.hidden = true;
    if (precheckBanner) precheckBanner.hidden = true;
    deployReady = false;
    resetCameraCheck("未检测");
    updateCameraCheckAvailability();

    btnDeploy.disabled = true;
    btnCancel.disabled = false;
    setBadge(deployBadge, "Deploying", "warn");
    setStatusLine(deployStatus, "部署中...", "");

    try {
      const payload = {
        remote_dir: (remoteDir?.value || "").trim() || null,
        force,
      };
      const data = await jsonFetch(`/api/session/${sessionId}/deploy/${demoId}`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const ws = new WebSocket(wsUrl(data.ws_url));
      jobSocket = ws;
      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === "log") {
          appendLog(deployLog, msg.data, autoScroll?.checked);
        } else if (msg.type === "status") {
          if (msg.data === "DONE") {
            setBadge(deployBadge, "Success", "ok");
            setStatusLine(deployStatus, "部署完成", "ok");
            deployReady = true;
            updateCameraCheckAvailability();
            updateRunAvailability();
            btnDeploy.disabled = false;
            btnCancel.disabled = true;
          } else if (msg.data === "FAILED") {
            setBadge(deployBadge, "Failed", "error");
            setStatusLine(deployStatus, `部署失败 (exit=${msg.exit_code ?? "?"})`, "error");
            deployReady = false;
            resetCameraCheck("未检测");
            updateCameraCheckAvailability();
            btnDeploy.disabled = false;
            btnCancel.disabled = true;
          } else if (msg.data === "CANCELLED") {
            setBadge(deployBadge, "Cancelled", "error");
            setStatusLine(deployStatus, "部署已取消", "error");
            deployReady = false;
            resetCameraCheck("未检测");
            updateCameraCheckAvailability();
            btnDeploy.disabled = false;
            btnCancel.disabled = true;
          } else {
            setStatusLine(deployStatus, msg.data, "");
          }
        }
      };
      ws.onclose = () => {
        btnCancel.disabled = true;
      };
    } catch (err) {
      if (err.data?.message === "already installed") {
        const when = err.data.installed_at ? `，${err.data.installed_at}` : "";
        const ver = err.data.version ? `，${err.data.version}` : "";
        setStatusLine(deployStatus, "检测到已配置" + ver, "ok");
        if (precheckText) precheckText.textContent = `检测到已配置${when}${ver}`;
        if (precheckBanner) precheckBanner.hidden = false;
        setBadge(deployBadge, "Installed", "ok");
        deployReady = true;
        updateCameraCheckAvailability();
        updateRunAvailability();
      } else {
        setStatusLine(deployStatus, `部署启动失败: ${err.message}`, "error");
        setBadge(deployBadge, "Failed", "error");
        deployReady = false;
        resetCameraCheck("未检测");
        updateCameraCheckAvailability();
      }
      btnDeploy.disabled = false;
      btnCancel.disabled = true;
    }
  }

  async function startRun() {
    const sessionId = localStorage.getItem(sessionKey);
    if (!sessionId) return;
    if (!runEnabled) {
      setStatusLine(runStatus, "该 Demo 无运行项", "");
      return;
    }
    if (!deployReady) {
      setStatusLine(runStatus, "请先完成部署", "error");
      return;
    }
    if (requireCameraCheck && !cameraReady) {
      setStatusLine(runStatus, "请先检测摄像头", "error");
      return;
    }
    runLog.textContent = "";
    if (runError) runError.hidden = true;
    if (runErrorSide) runErrorSide.hidden = true;
    runInfo = {};
    updateWebuiUI(false);
    updateWebuiLink("");
    if (btnOpenWebui) btnOpenWebui.textContent = "打开 WebUI";
    runState = "STARTING";
    updateRunAvailability();
    btnRun.disabled = true;
    btnStop.disabled = true;
    setBadge(runBadge, "Starting", "warn");
    setStatusLine(runStatus, "启动中...", "");
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
          if (msg.info) {
            runInfo = msg.info;
          }
          if (msg.data === "RUNNING") {
            setBadge(runBadge, "Running", "ok");
            setStatusLine(runStatus, "运行中", "ok");
            runState = "RUNNING";
            updateRunAvailability();
            updateWebuiUI(true);
            if (runType === "webui") {
              setStatusLine(runStatus, "运行中（点击按钮打开）", "ok");
              const url = resolveWebuiUrl();
              if (btnOpenWebui) {
                btnOpenWebui.textContent = url ? `打开 WebUI (${url})` : "打开 WebUI";
              }
              updateWebuiLink(url);
              if (video) {
                video.hidden = false;
                if (!video.src && previewSrc) video.src = previewSrc;
              }
            } else if (pendingVideoUrl && video) {
              video.hidden = false;
              video.src = pendingVideoUrl;
            }
          } else if (msg.data === "ERROR") {
            setBadge(runBadge, "Error", "error");
            setStatusLine(runStatus, "启动失败", "error");
            runState = "ERROR";
            updateRunAvailability();
            btnStop.disabled = true;
            updateWebuiUI(false);
            if (video) video.src = previewSrc;
            if (video) video.hidden = false;
            if (runType === "webui" && runErrorSide) {
              runErrorSide.hidden = false;
              runErrorSide.textContent = "WebUI 启动失败，请检查日志与 Jetson 服务状态。";
            }
          } else if (msg.data === "STOPPED") {
            setBadge(runBadge, "Stopped", "");
            setStatusLine(runStatus, "已停止", "");
            runState = "STOPPED";
            updateRunAvailability();
            btnStop.disabled = true;
            updateWebuiUI(false);
            if (video) video.src = previewSrc;
            if (video) video.hidden = false;
          } else {
            setStatusLine(runStatus, msg.data, "");
          }
        }
      };
      ws.onclose = () => {
        btnStop.disabled = true;
      };
      if (video) {
        video.onerror = () => {
          if (runError) {
            runError.hidden = false;
            runError.textContent = "视频流连接失败，请检查摄像头与 Jetson 推理服务。";
          }
        };
      }
    } catch (err) {
      setBadge(runBadge, "Error", "error");
      setStatusLine(runStatus, `启动失败: ${err.message}`, "error");
      runState = "ERROR";
      updateRunAvailability();
      btnStop.disabled = true;
    }
  }

  btnConnect?.addEventListener("click", async () => {
    if (!validateConnectForm()) return;
    btnConnect.disabled = true;
    setBadge(connectBadge, "Connecting", "warn");
    setStatusLine(connectStatus, "连接中...", "");
    try {
      const data = await jsonFetch("/api/session/connect", {
        method: "POST",
        body: JSON.stringify({
          ip: (host.value || "").trim(),
          port: parseInt(port.value || "22", 10),
          username: (username.value || "seeed").trim(),
          password: (password.value || "").trim(),
          sudo_password: (sudoPassword.value || "").trim() || null,
        }),
      });
      localStorage.setItem(sessionKey, data.session_id);
      setBadge(connectBadge, "Connected", "ok");
      setStatusLine(connectStatus, "连接成功", "ok");
      runState = "IDLE";
      deployReady = false;
      resetCameraCheck("未检测");
      updateCameraCheckAvailability();
      updateRunAvailability();
      btnSudo.disabled = false;
      if ((sudoPassword.value || "").trim()) {
        setStatusLine(sudoStatus, "sudo 密码已保存", "ok");
      }
      btnDeploy.disabled = false;
      await refreshPrecheck();
    } catch (err) {
      setBadge(connectBadge, "Failed", "error");
      setStatusLine(connectStatus, `连接失败: ${err.message}`, "error");
      deployReady = false;
      resetCameraCheck("未检测");
      updateCameraCheckAvailability();
      updateRunAvailability();
    } finally {
      btnConnect.disabled = false;
    }
  });

  btnSudo?.addEventListener("click", async () => {
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
      setStatusLine(sudoStatus, "sudo 密码已更新", "ok");
    } catch (err) {
      setStatusLine(sudoStatus, `更新失败: ${err.message}`, "error");
    } finally {
      btnSudo.disabled = false;
    }
  });

  btnDeploy?.addEventListener("click", async () => {
    const sessionId = localStorage.getItem(sessionKey);
    if (!sessionId) {
      setStatusLine(deployStatus, "请先连接 Jetson", "error");
      return;
    }
    const status = await refreshPrecheck();
    if (status?.installed) {
      if (precheckBanner) precheckBanner.hidden = false;
      return;
    }
    startDeploy(false);
  });

  btnCancel?.addEventListener("click", async () => {
    const sessionId = localStorage.getItem(sessionKey);
    if (!sessionId) return;
    btnCancel.disabled = true;
    try {
      await jsonFetch(`/api/session/${sessionId}/deploy/${demoId}/cancel`, { method: "POST" });
      setStatusLine(deployStatus, "已发送取消", "");
      if (jobSocket) jobSocket.close();
    } catch (err) {
      setStatusLine(deployStatus, `取消失败: ${err.message}`, "error");
    }
  });

  btnSkip?.addEventListener("click", async () => {
    if (!runEnabled) {
      setStatusLine(deployStatus, "该 Demo 无运行项", "");
      return;
    }
    startRun();
  });

  btnForce?.addEventListener("click", async () => {
    startDeploy(true);
  });

  btnCameraCheck?.addEventListener("click", async () => {
    const sessionId = localStorage.getItem(sessionKey);
    if (!sessionId) {
      setStatusLine(cameraStatus, "请先连接 Jetson", "error");
      return;
    }
    if (!deployReady) {
      setStatusLine(cameraStatus, "请先完成部署", "error");
      return;
    }
    btnCameraCheck.disabled = true;
    setStatusLine(cameraStatus, "检测中...", "");
    try {
      const payload = { remote_dir: (remoteDir?.value || "").trim() || null };
      const data = await jsonFetch(`/api/session/${sessionId}/demo/${demoId}/camera_check`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      cameraReady = Boolean(data.ok);
      if (cameraReady) {
        setStatusLine(cameraStatus, data.message || "摄像头可用", "ok");
      } else {
        setStatusLine(cameraStatus, `检测失败: ${data.message || "未知错误"}`, "error");
      }
    } catch (err) {
      cameraReady = false;
      setStatusLine(cameraStatus, `检测失败: ${err.message}`, "error");
    } finally {
      updateRunAvailability();
      updateCameraCheckAvailability();
    }
  });

  btnOpenWebui?.addEventListener("click", (event) => {
    const url = resolveWebuiUrl();
    if (!url) {
      event.preventDefault();
      if (webuiUrl) webuiUrl.textContent = "WebUI 地址不可用";
      return;
    }
    updateWebuiLink(url);
    const width = Math.min(1280, screen.availWidth - 80);
    const height = Math.min(900, screen.availHeight - 80);
    const left = Math.max(0, screen.availWidth - width - 20);
    const top = Math.max(0, Math.round((screen.availHeight - height) / 2));
    const features = `noopener,noreferrer,width=${width},height=${height},left=${left},top=${top}`;
    const win = window.open(url, "webui", features);
    if (win) {
      win.focus();
      event.preventDefault();
    }
  });

  btnRun?.addEventListener("click", async () => {
    startRun();
  });

  btnStop?.addEventListener("click", async () => {
    const sessionId = localStorage.getItem(sessionKey);
    if (!sessionId || !currentRunId) return;
    btnStop.disabled = true;
    try {
      await jsonFetch(`/api/session/${sessionId}/stop/${currentRunId}`, { method: "POST" });
      if (video) video.src = previewSrc;
      setStatusLine(runStatus, "已停止", "");
      setBadge(runBadge, "Stopped", "");
      runState = "STOPPED";
      updateRunAvailability();
      if (runSocket) runSocket.close();
    } catch (err) {
      setStatusLine(runStatus, `停止失败: ${err.message}`, "error");
    }
  });

  function openLogFullscreen() {
    if (!deployLogShell || !logOverlay) return;
    if (!logAnchor) {
      logAnchor = document.createComment("log-shell-anchor");
      deployLogShell.parentNode?.insertBefore(logAnchor, deployLogShell);
    }
    logOverlay.hidden = false;
    logOverlay.appendChild(deployLogShell);
    deployLogShell.classList.add("fullscreen");
    document.body.classList.add("log-fullscreen");
    if (btnLogExpand) btnLogExpand.textContent = "收起";
  }

  function closeLogFullscreen() {
    if (!deployLogShell || !logOverlay) return;
    deployLogShell.classList.remove("fullscreen");
    document.body.classList.remove("log-fullscreen");
    if (logAnchor && logAnchor.parentNode) {
      logAnchor.parentNode.insertBefore(deployLogShell, logAnchor);
      logAnchor.remove();
    }
    logAnchor = null;
    logOverlay.hidden = true;
    if (btnLogExpand) btnLogExpand.textContent = "展开";
  }

  btnLogExpand?.addEventListener("click", () => {
    if (!deployLogShell || !logOverlay) return;
    const isFull = !logOverlay.hidden;
    if (isFull) {
      closeLogFullscreen();
    } else {
      openLogFullscreen();
    }
  });

  logOverlay?.addEventListener("click", (event) => {
    if (event.target === logOverlay) {
      closeLogFullscreen();
    }
  });

  btnLogCopy?.addEventListener("click", async () => {
    if (!deployLog) return;
    const ok = await copyToClipboard(deployLog.textContent || "");
    setStatusLine(deployStatus, ok ? "日志已复制" : "复制失败", ok ? "ok" : "error");
  });

  btnLogClear?.addEventListener("click", () => {
    if (deployLog) deployLog.textContent = "";
  });

  refreshPrecheck();
})();
