(() => {
  const state = { csrf: null, overview: null, pendingAction: null, voice: null, voiceBusy: false };
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    if (options.method && options.method !== "GET" && state.csrf) headers.set("X-CSRF-Token", state.csrf);
    const response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store" });
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) {
      endVoiceCall({ quiet: true });
      showLogin();
      throw new Error(data.error || "登录已过期");
    }
    if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
    return data;
  }

  function showLogin() {
    state.csrf = null;
    $("#dashboard-nav").classList.add("hidden");
    $("#dashboard-view").classList.add("hidden");
    $("#login-view").classList.remove("hidden");
    $("#sign-out").classList.add("hidden");
    setConnection("pending", "未登录");
  }

  function showDashboard() {
    $("#dashboard-nav").classList.remove("hidden");
    $("#login-view").classList.add("hidden");
    $("#dashboard-view").classList.remove("hidden");
    $("#sign-out").classList.remove("hidden");
  }

  function setVoiceStatus(kind, label, detail) {
    const pill = $("#voice-status-pill");
    pill.className = `status-pill ${kind}`;
    pill.textContent = label;
    $("#voice-status-text").textContent = detail;
  }

  function setVoiceBusy(busy) {
    state.voiceBusy = busy;
    $("#voice-start").disabled = busy || Boolean(state.voice);
    $("#voice-hangup").disabled = busy || !state.voice;
  }

  async function endVoiceCall({ notifyServer = true, quiet = false } = {}) {
    const voice = state.voice;
    if (!voice) return;
    state.voice = null;
    voice.pc?.close();
    voice.stream?.getTracks().forEach(track => track.stop());
    const audio = $("#voice-remote-audio");
    audio.pause();
    audio.srcObject = null;
    setVoiceBusy(false);
    setVoiceStatus("muted", "已挂断", "机器人将重新检测本地麦克风");
    if (notifyServer) {
      try { await request(`/api/voice/calls/${encodeURIComponent(voice.callId)}/hangup`, { method: "POST", body: "{}" }); }
      catch (error) { if (!quiet && state.csrf) showError(error.message); }
    }
  }

  async function startVoiceCall() {
    if (state.voice || state.voiceBusy) return;
    if (!navigator.mediaDevices?.getUserMedia || !window.RTCPeerConnection) {
      setVoiceStatus("bad", "不可用", "此浏览器不支持 WebRTC 通话");
      return;
    }
    setVoiceBusy(true);
    setVoiceStatus("pending", "正在准备", "正在释放机器人本地麦克风");
    let callId = null;
    let stream = null;
    let pc = null;
    try {
      const started = await request("/api/voice/calls/start", { method: "POST", body: "{}" });
      callId = started.call_id;
      if (!callId || started.local_mic_released !== true || started.offer_required !== true) {
        throw new Error("机器人未确认释放本地麦克风，通话未启动");
      }
      setVoiceStatus("pending", "请求麦克风", "请在浏览器提示中允许麦克风访问");
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false });
      pc = new RTCPeerConnection({ iceServers: [] });
      pc.ontrack = event => {
        const audio = $("#voice-remote-audio");
        audio.srcObject = event.streams[0] || new MediaStream([event.track]);
        audio.play().catch(() => setVoiceStatus("pending", "通话已连接", "点击页面后允许播放机器人语音"));
      };
      pc.onconnectionstatechange = () => {
        if (state.voice?.pc !== pc) return;
        if (pc.connectionState === "connected") setVoiceStatus("good", "通话中", "浏览器通话已连接");
        else if (pc.connectionState === "disconnected") setVoiceStatus("pending", "正在重连", "等待局域网连接恢复");
        else if (pc.connectionState === "failed") { setVoiceStatus("bad", "连接失败", "正在清理通话资源"); endVoiceCall(); }
      };
      stream.getAudioTracks().forEach(track => pc.addTrack(track, stream));
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      if (pc.iceGatheringState !== "complete") {
        await new Promise((resolve, reject) => {
          const timeout = window.setTimeout(() => reject(new Error("WebRTC 网络候选收集超时")), 8000);
          pc.addEventListener("icegatheringstatechange", () => {
            if (pc.iceGatheringState === "complete") { window.clearTimeout(timeout); resolve(); }
          }, { once: false });
        });
      }
      const result = await request(`/api/voice/calls/${encodeURIComponent(callId)}/answer`, {
        method: "POST", body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type }),
      });
      const answer = result.answer;
      if (!answer?.sdp || answer.type !== "answer") throw new Error("服务器返回的 WebRTC 应答无效");
      state.voice = { callId, stream, pc };
      setVoiceStatus("pending", "连接中", "正在建立局域网音频连接");
      await pc.setRemoteDescription(answer);
    } catch (error) {
      if (state.voice?.callId === callId) state.voice = null;
      pc?.close();
      stream?.getTracks().forEach(track => track.stop());
      if (callId) {
        try { await request(`/api/voice/calls/${encodeURIComponent(callId)}/hangup`, { method: "POST", body: "{}" }); } catch (_cleanupError) {}
      }
      setVoiceStatus("bad", "未连接", error.message);
      if (state.csrf) showError(error.message);
    } finally {
      setVoiceBusy(false);
    }
  }

  function setConnection(kind, label) {
    const node = $("#connection-state");
    node.className = `connection-state ${kind}`;
    node.lastElementChild.textContent = label;
  }

  function text(value, fallback = "—") {
    return value === null || value === undefined || value === "" ? fallback : String(value);
  }

  function riskLabel(risk) {
    return ({ normal: "正常", confirm: "AI 复核", high: "高危" })[risk] || "未分类";
  }

  function riskClass(risk) {
    return ["normal", "confirm", "high"].includes(risk) ? risk : "high";
  }

  function pill(label, className = "muted") {
    const node = document.createElement("span");
    node.className = `status-pill ${className}`;
    node.textContent = label;
    return node;
  }

  function renderOverview(data) {
    const robot = data.robot || {};
    const voice = data.voice || {};
    if (state.voice && !["CALL", "CALL_RECONNECTING"].includes(voice.state)) {
      window.queueMicrotask(() => endVoiceCall({ quiet: true }));
    }
    const healthy = robot.connected === true && robot.healthy === true;
    const known = robot.connected !== null && robot.healthy !== null;
    $("#robot-identity").textContent = `${text(robot.manufacturer)} ${text(robot.model)} · ${text(robot.robot_id)}`;
    $("#health-indicator").className = `health-dot ${healthy ? "good" : known ? "bad" : "unknown"}`;
    $("#health-label").textContent = healthy ? "已连接 · 健康" : known ? "断开或异常" : "状态未知";
    setConnection(healthy ? "connected" : known ? "disconnected" : "pending", healthy ? "已连接" : known ? "异常" : "状态未知");
    $("#active-task").textContent = text(data.active_task, "无");
    $("#latest-feedback").textContent = text(data.latest_feedback, "暂无");
    $("#policy-version").textContent = text(data.policy_version);
    if (!state.voiceBusy && !state.voice) {
      const details = {
        PASSIVE: ["good", "唤醒待机", `${text(voice.robot_name, "机器人")}正在监听唤醒词`],
        SLEEP: ["muted", "休眠", "没有可用的机器人麦克风；可使用网页通话"],
        CALL_PENDING: ["pending", "切换音源", "正在释放机器人本地麦克风"],
        CALL: ["good", "通话中", "网页麦克风是当前唯一音源"],
        CALL_RECONNECTING: ["pending", "正在重连", "重连窗口内保留通话音源租约"],
      }[voice.state] || ["muted", "不可用", voice.error || "语音服务未启动"];
      setVoiceStatus(...details);
    }
    renderRobotInformation(data.robot_configuration || {});
    renderActions(data.actions || [], "#overview-actions", 5);
    renderCapabilities(data.capabilities || []);
    renderControls(data.capabilities || []);
  }

  function renderRobotInformation(configuration) {
    const root = $("#robot-info-list");
    const fields = [
      ["厂商", configuration.manufacturer],
      ["型号", configuration.model],
      ["机器人 ID", configuration.robot_id],
      ["机器人模块", configuration.robot_module],
      ["Profile", configuration.profile_id],
      ["驱动适配器", configuration.adapter_id],
      ["配置版本", configuration.profile_version],
    ];
    root.replaceChildren();
    for (const [label, value] of fields) {
      const item = document.createElement("div");
      const term = document.createElement("dt");
      const detail = document.createElement("dd");
      term.textContent = label;
      detail.textContent = text(value);
      item.append(term, detail);
      root.append(item);
    }
  }

  function renderCapabilities(capabilities) {
    const root = $("#capability-list");
    root.replaceChildren();
    if (!capabilities.length) {
      root.append(empty("当前 profile 未启用可调用能力"));
      return;
    }
    for (const capability of capabilities) {
      const row = document.createElement("article");
      row.className = "capability-row";
      const name = document.createElement("div");
      name.className = "capability-name";
      const strong = document.createElement("strong");
      strong.textContent = capability.name;
      const stateLabel = document.createElement("span");
      stateLabel.textContent = capability.enabled ? "可用" : text(capability.disabled_reason, "不可用");
      name.append(strong, stateLabel);

      const parameterText = Object.entries(capability.parameters || {}).map(([key, value]) => {
        const range = value.type === "number" ? `${text(value.min, "−∞")} … ${text(value.max, "+∞")}` : value.type;
        return `${key}: ${range}`;
      }).join(" · ") || "无参数";
      const parameters = document.createElement("div");
      parameters.className = "capability-detail";
      parameters.textContent = parameterText;

      const risk = document.createElement("div");
      risk.append(pill(riskLabel(capability.risk_level), riskClass(capability.risk_level)));

      const access = document.createElement("div");
      access.className = "capability-detail";
      access.textContent = `入口：${(capability.allowed_callers || []).map(callerLabel).join("、") || "无"}`;
      row.append(name, parameters, risk, access);
      root.append(row);
    }
  }

  function callerLabel(caller) {
    return ({ agent: "Agent", dashboard: "Dashboard", maintenance: "维护" })[caller] || caller;
  }

  function renderControls(capabilities) {
    const root = $("#controls-list");
    root.replaceChildren();
    const available = capabilities.filter(item => item.enabled && item.dashboard_allowed);
    if (!available.length) {
      root.append(empty("当前没有开放给 Dashboard 的控制操作"));
      return;
    }
    for (const capability of available) {
      const row = document.createElement("form");
      row.className = "control-row";
      row.dataset.skill = capability.name;

      const meta = document.createElement("div");
      meta.className = "control-meta";
      const name = document.createElement("strong");
      name.textContent = capability.name;
      meta.append(name, pill(riskLabel(capability.risk_level), riskClass(capability.risk_level)));

      const fields = document.createElement("div");
      fields.className = "parameter-fields";
      for (const [parameter, spec] of Object.entries(capability.parameters || {})) {
        const label = document.createElement("label");
        label.className = "parameter-field";
        const caption = document.createElement("span");
        caption.textContent = parameter;
        let input;
        if (spec.type === "boolean") {
          input = document.createElement("select");
          input.add(new Option("开启", "true"));
          input.add(new Option("关闭", "false"));
        } else {
          input = document.createElement("input");
          input.type = spec.type === "string" ? "text" : "number";
          if (spec.min !== null && spec.min !== undefined) input.min = spec.min;
          if (spec.max !== null && spec.max !== undefined) input.max = spec.max;
          if (spec.max_length) input.maxLength = spec.max_length;
          if (input.type === "number") input.step = "any";
        }
        input.name = parameter;
        input.required = true;
        label.append(caption, input);
        fields.append(label);
      }

      const execute = document.createElement("button");
      execute.className = capability.risk_level === "high" ? "danger-button" : "primary-button";
      execute.type = "submit";
      execute.textContent = capability.risk_level === "high" || capability.operator_confirmation_required ? "确认操作" : "执行";
      row.append(meta, fields, execute);
      row.addEventListener("submit", event => {
        event.preventDefault();
        const parameters = {};
        for (const [key, value] of new FormData(row).entries()) {
          const spec = capability.parameters[key];
          parameters[key] = spec.type === "number" ? Number(value) : spec.type === "boolean" ? value === "true" : value;
        }
        const needsReview = capability.risk_level === "confirm";
        const needsOperator = capability.risk_level === "high" || capability.operator_confirmation_required;
        if (needsReview || needsOperator) openConfirmation(capability, parameters);
        else invokeAction(capability, parameters);
      });
      root.append(row);
    }
  }

  function openConfirmation(capability, parameters) {
    state.pendingAction = { capability, parameters };
    $("#dialog-risk").textContent = `${riskLabel(capability.risk_level)} · ${capability.name}`;
    $("#dialog-risk").className = `eyebrow ${riskClass(capability.risk_level)}`;
    $("#dialog-title").textContent = "确认本次操作";
    const reviewText = capability.risk_level === "confirm" ? "服务端将先对这组固定参数进行 AI 复核。" : "";
    const operatorText = capability.risk_level === "high" || capability.operator_confirmation_required ? "本次执行会记录为当前操作员的明确确认。" : "";
    $("#dialog-description").textContent = `目标机器人：${text(state.overview?.robot?.robot_id)}。${reviewText}${operatorText}操作仍会经过服务端策略和适配器本地检查。`;
    const summary = $("#dialog-parameters");
    summary.replaceChildren();
    for (const [key, value] of Object.entries(parameters)) {
      const item = document.createElement("div");
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = key;
      dd.textContent = String(value);
      item.append(dt, dd);
      summary.append(item);
    }
    $("#explicit-confirm-row").classList.toggle("hidden", capability.risk_level !== "high");
    $("#explicit-confirm").checked = false;
    $("#dialog-execute").disabled = capability.risk_level === "high";
    $("#dialog-execute").className = capability.risk_level === "high" ? "danger-button" : "primary-button";
    $("#action-dialog").showModal();
  }

  async function invokeAction(capability, parameters) {
    $("#action-dialog").close();
    try {
      const prepared = await request("/api/actions/prepare", {
        method: "POST",
        body: JSON.stringify({ skill: capability.name, parameters }),
      });
      const body = {};
      if (prepared.requires_operator_confirmation) {
        const pin = window.prompt("请输入当前操作员 PIN 以确认本次操作");
        if (pin === null) throw new Error("已取消操作员确认");
        body.operator_pin = pin;
      }
      const data = await request("/api/actions/execute", {
        method: "POST",
        body: JSON.stringify({ ...body, challenge_id: prepared.challenge_id }),
      });
      if (prepared.risk_level === "confirm") {
        const review = data.action?.review;
        if (!review || review.verdict !== "allow") {
          showError("AI 复核未通过；操作未执行");
          return;
        }
      }
      announce(data.action?.message || "操作已处理");
      await refresh();
    } catch (error) {
      showError(error.message);
    }
  }

  function renderActions(actions, selector, limit = 50) {
    const root = $(selector);
    root.replaceChildren();
    if (!actions.length) {
      root.append(empty("暂无操作记录"));
      return;
    }
    for (const action of actions.slice(0, limit)) {
      const row = document.createElement("article");
      row.className = "activity-row";
      const operation = document.createElement("strong");
      operation.textContent = action.operation || "机器人任务";
      const detail = document.createElement("span");
      detail.textContent = `${callerLabel(action.source || "dashboard")} · ${riskLabel(action.risk_level)}`;
      if (action.review) detail.textContent += ` · AI 复核：${action.review.verdict || "已记录"}`;
      if (action.operator_confirmed) detail.textContent += " · 操作员已确认";
      const time = document.createElement("time");
      time.dateTime = action.timestamp || "";
      time.textContent = action.timestamp ? new Date(action.timestamp).toLocaleString() : "";
      const result = document.createElement("span");
      result.className = "activity-result";
      result.textContent = text(action.message || action.status, "—");
      row.append(operation, detail, time, result);
      root.append(row);
    }
  }

  function empty(message) {
    const node = document.createElement("p");
    node.className = "empty-state";
    node.textContent = message;
    return node;
  }

  function showError(message) {
    $("#global-error").textContent = message;
    announce(message);
  }

  function announce(message) {
    $("#live-region").textContent = message;
  }

  async function refresh() {
    try {
      const data = await request("/api/dashboard/overview");
      state.overview = data;
      renderOverview(data);
      renderActions(data.actions || [], "#activity-list");
      $("#global-error").textContent = "";
    } catch (error) {
      if (state.csrf) showError(error.message);
    }
  }

  function selectView(name) {
    $$(".nav-item").forEach(button => button.classList.toggle("active", button.dataset.view === name));
    $$(".view-panel").forEach(panel => {
      const active = panel.dataset.panel === name;
      panel.classList.toggle("active", active);
      panel.hidden = !active;
    });
  }

  $("#login-form").addEventListener("submit", async event => {
    event.preventDefault();
    $("#login-error").textContent = "";
    const pin = $("#operator-pin").value;
    try {
      const data = await request("/api/login", { method: "POST", body: JSON.stringify({ pin }) });
      state.csrf = data.csrf_token;
      $("#operator-pin").value = "";
      showDashboard();
      await refresh();
    } catch (error) {
      $("#login-error").textContent = error.message;
    }
  });

  $("#task-form").addEventListener("submit", async event => {
    event.preventDefault();
    const field = $("#task-text");
    const submit = $("#task-form button[type=submit]");
    submit.disabled = true;
    try {
      await request("/api/tasks", { method: "POST", body: JSON.stringify({ text: field.value }) });
      field.value = "";
      await refresh();
      announce("任务已处理");
    } catch (error) {
      showError(error.message);
    } finally {
      submit.disabled = false;
    }
  });

  $("#explicit-confirm").addEventListener("change", event => {
    $("#dialog-execute").disabled = !event.target.checked;
  });
  $("#dialog-execute").addEventListener("click", () => {
    if (!state.pendingAction) return;
    const { capability, parameters } = state.pendingAction;
    state.pendingAction = null;
    invokeAction(capability, parameters);
  });
  $("#dialog-cancel").addEventListener("click", () => {
    state.pendingAction = null;
    $("#action-dialog").close();
  });
  $("#action-dialog").addEventListener("close", () => {
    state.pendingAction = null;
    $("#explicit-confirm").checked = false;
  });
  $("#sign-out").addEventListener("click", async () => {
    await endVoiceCall({ quiet: true });
    try { await request("/api/logout", { method: "POST", body: "{}" }); } catch (_error) {}
    showLogin();
  });
  $("#voice-start").addEventListener("click", startVoiceCall);
  $("#voice-hangup").addEventListener("click", () => endVoiceCall());
  window.addEventListener("pagehide", () => {
    const voice = state.voice;
    if (!voice) return;
    voice.pc?.close();
    voice.stream?.getTracks().forEach(track => track.stop());
    if (state.csrf) fetch(`/api/voice/calls/${encodeURIComponent(voice.callId)}/hangup`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
      body: "{}", credentials: "same-origin", keepalive: true,
    }).catch(() => {});
  });
  $("#refresh").addEventListener("click", refresh);
  window.setInterval(() => {
    if (state.csrf && !document.hidden) refresh();
  }, 5000);
  $$(".nav-item").forEach(button => button.addEventListener("click", () => selectView(button.dataset.view)));
  $$('[data-open-view]').forEach(button => button.addEventListener("click", () => selectView(button.dataset.openView)));

  request("/api/session").then(data => {
    if (data.authenticated) {
      state.csrf = data.csrf_token;
      showDashboard();
      refresh();
    } else showLogin();
  }).catch(() => showLogin());
})();
