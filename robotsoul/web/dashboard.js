(() => {
  const state = { csrf: null, overview: null, pendingAction: null, voice: null, voiceBusy: false, levels: null };
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
    $("#operator-password").value = "";
    $("#account-current-password").value = "";
    $("#account-password").value = "";
    $("#account-password-confirm").value = "";
    $("#llm-api-key").value = "";
    $("#dashboard-nav").classList.add("hidden");
    $("#dashboard-view").classList.add("hidden");
    $("#login-view").classList.remove("hidden");
    $("#sign-out").classList.add("hidden");
    setConnection("pending", "未登录");
    selectView("overview");
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
    return ({ normal: "常规", confirm: "确认", high: "高危" })[risk] || "未分类";
  }

  function disabledLabel(reason) {
    return ({
      "AI reviewer is not configured": "未配置 AI 复核，暂时不能执行",
      "Dashboard caller is not authorized by policy": "当前策略不允许在控制台执行",
    })[reason] || text(reason, "不可用");
  }

  function capabilityLabel(capability) {
    return text(capability.label, capability.name || capability.operation);
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

  function renderBattery(battery) {
    const node = $("#battery-status");
    const fill = $("#battery-fill");
    const label = $("#battery-label");
    if (!node || !fill || !label) return;
    const level = battery && Number.isInteger(battery.level_percent) ? battery.level_percent : null;
    const visible = level !== null && level >= 0 && level <= 100;
    node.classList.toggle("hidden", !visible);
    if (!visible) {
      node.classList.remove("charging", "low");
      fill.style.width = "0";
      label.textContent = "—";
      return;
    }
    const charging = battery.charging === true ? true : battery.charging === false ? false : null;
    node.classList.toggle("charging", charging === true);
    node.classList.toggle("low", charging !== true && level <= 20);
    fill.style.width = `${level}%`;
    const chargeText = charging === true ? "充电中" : charging === false ? "未充电" : "";
    label.textContent = chargeText ? `${level}% · ${chargeText}` : `${level}%`;
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
    renderBattery(robot.battery);
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
    if (Array.isArray(data.voice_events)) {
      renderVoiceLog(data.voice_events, "#voice-log");
      if (monitorEnabled("logs")) renderVoiceLog(data.voice_events, "#monitor-log");
    }
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

  function renderVoiceLog(events, selector) {
    const root = $(selector);
    if (!root) return;
    root.replaceChildren();
    if (!events.length) {
      root.append(empty("还没有唤醒或识别记录"));
      return;
    }
    for (const event of events) {
      const row = document.createElement("article");
      row.className = "activity-row";
      const title = document.createElement("strong");
      title.textContent = event.kind === "wake" ? "已唤醒" : "识别文本";
      const when = document.createElement("time");
      when.dateTime = event.timestamp || "";
      when.textContent = formatVoiceTime(event.timestamp);
      const source = document.createElement("span");
      source.textContent = event.source === "call" ? "网页通话" : "本地麦克风";
      const spoken = document.createElement("span");
      spoken.className = "activity-result";
      spoken.textContent = text(event.text, "（空）");
      row.append(title, when, source, spoken);
      root.append(row);
    }
  }

  function formatVoiceTime(value) {
    if (!value) return "时间未知";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return text(value);
    return parsed.toLocaleString("zh-CN", { hour12: false });
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
      strong.textContent = capabilityLabel(capability);
      strong.title = capability.name;
      const stateLabel = document.createElement("span");
      stateLabel.textContent = capability.enabled ? "可用" : disabledLabel(capability.disabled_reason);
      name.append(strong, stateLabel);

      const parameterText = Object.entries(capability.parameters || {}).map(([key, value]) => {
        const caption = value.label || key;
        const range = value.type === "number" ? `${text(value.min, "−∞")} … ${text(value.max, "+∞")}` : value.type;
        return `${caption}: ${range}`;
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
    const available = capabilities.filter(item => item.dashboard_allowed);
    if (!available.length) {
      root.append(empty("当前没有开放给 Dashboard 的控制操作"));
      return;
    }
    const posture = ["sport.stand_up", "sport.stand_down"]
      .map(name => available.find(item => item.name === name))
      .filter(Boolean);
    const grouped = new Set(posture);
    if (posture.length) {
      const heading = document.createElement("h3");
      heading.className = "control-group";
      heading.textContent = "站立与卧倒";
      root.append(heading);
      for (const capability of posture) root.append(controlRow(capability));
    }
    const groups = [
      ["normal", "常规"],
      ["confirm", "需确认"],
      ["high", "高危"],
    ];
    for (const [risk, title] of groups) {
      const items = available.filter(item => item.risk_level === risk && !grouped.has(item));
      if (!items.length) continue;
      items.forEach(item => grouped.add(item));
      const heading = document.createElement("h3");
      heading.className = "control-group";
      heading.textContent = `${title} · ${items.length}`;
      root.append(heading);
      for (const capability of items) root.append(controlRow(capability));
    }
    for (const capability of available) {
      if (!grouped.has(capability)) root.append(controlRow(capability));
    }
  }

  function controlRow(capability) {
    const row = document.createElement("form");
    row.className = "control-row";
    if (!capability.enabled) row.classList.add("is-disabled");
    row.dataset.skill = capability.name;

    const meta = document.createElement("div");
    meta.className = "control-meta";
    const name = document.createElement("strong");
    name.textContent = capabilityLabel(capability);
    name.title = capability.name;
    meta.append(name, pill(riskLabel(capability.risk_level), riskClass(capability.risk_level)));
    if (!capability.enabled) {
      const note = document.createElement("p");
      note.className = "control-note";
      note.textContent = disabledLabel(capability.disabled_reason);
      meta.append(note);
    }

    const fields = document.createElement("div");
    fields.className = "parameter-fields";
    for (const [parameter, spec] of Object.entries(capability.parameters || {})) {
      const label = document.createElement("label");
      label.className = "parameter-field";
      const caption = document.createElement("span");
      const bounds = spec.type === "number" ? `（${text(spec.min, "−∞")} … ${text(spec.max, "+∞")}）` : "";
      caption.textContent = `${spec.label || parameter}${bounds}`;
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
      input.disabled = !capability.enabled;
      label.append(caption, input);
      fields.append(label);
    }

    const execute = document.createElement("button");
    execute.className = capability.risk_level === "high" ? "danger-button" : "primary-button";
    execute.type = "submit";
    execute.disabled = !capability.enabled;
    execute.textContent = !capability.enabled
      ? "不可用"
      : capability.risk_level === "high" || capability.operator_confirmation_required
        ? "确认操作"
        : capability.risk_level === "confirm"
          ? "确认执行"
          : "执行";
    row.append(meta, fields, execute);
    row.addEventListener("submit", event => {
      event.preventDefault();
      if (!capability.enabled) return;
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
    return row;
  }

  function openConfirmation(capability, parameters) {
    state.pendingAction = { capability, parameters };
    $("#dialog-risk").textContent = `${riskLabel(capability.risk_level)} · ${capabilityLabel(capability)}`;
    $("#dialog-risk").className = `eyebrow ${riskClass(capability.risk_level)}`;
    $("#dialog-title").textContent = "确认本次操作";
    const reviewText = capability.risk_level !== "confirm"
      ? ""
      : capability.ai_review_required
        ? "服务端将先对这组固定参数进行 AI 复核。"
        : "本次由已登录的操作员确认后执行。";
    const operatorText = capability.risk_level === "high" || capability.operator_confirmation_required ? "本次执行会记录为当前操作员的明确确认。" : "";
    $("#dialog-description").textContent = `目标机器人：${text(state.overview?.robot?.robot_id)}。${reviewText}${operatorText}操作仍会经过服务端策略和适配器本地检查。`;
    const summary = $("#dialog-parameters");
    summary.replaceChildren();
    for (const [key, value] of Object.entries(parameters)) {
      const item = document.createElement("div");
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = capability.parameters?.[key]?.label || key;
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
        const password = window.prompt("请输入当前账户密码以确认本次操作");
        if (password === null) throw new Error("已取消操作员确认");
        body.operator_password = password;
      }
      const data = await request("/api/actions/execute", {
        method: "POST",
        body: JSON.stringify({ ...body, challenge_id: prepared.challenge_id }),
      });
      if (prepared.ai_review_required) {
        const review = data.action?.review;
        if (!review || review.verdict !== "allow" || review.reviewer === "dashboard_operator") {
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
      operation.textContent = action.label || action.operation || "机器人任务";
      operation.title = action.operation || "";
      const detail = document.createElement("span");
      detail.textContent = `${callerLabel(action.source || "dashboard")} · ${riskLabel(action.risk_level)}`;
      if (action.review?.reviewer === "dashboard_operator") detail.textContent += " · 操作员已确认";
      else if (action.review) detail.textContent += ` · AI 复核：${action.review.verdict || "已记录"}`;
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
      const data = await request(`/api/dashboard/overview${voiceEventsNeeded() ? "" : "?voice_events=0"}`);
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
    if (name === "monitor") {
      syncMonitorPanels();
      if (monitorEnabled("logs") && state.csrf && !Array.isArray(state.overview?.voice_events)) refresh();
    } else stopMonitor();
    if (name === "overview" && state.csrf && !Array.isArray(state.overview?.voice_events)) refresh();
    if (name === "settings") loadSettings();
  }

  function monitorEnabled(name) {
    return Boolean($(`input[data-monitor="${name}"]`)?.checked);
  }

  function selectedAudioChannels() {
    return ["input", "output"].filter(monitorEnabled);
  }

  function voiceEventsNeeded() {
    const overviewOpen = !$("[data-panel=overview]")?.hidden;
    const monitorOpen = !$("[data-panel=monitor]")?.hidden;
    return overviewOpen || (monitorOpen && monitorEnabled("logs"));
  }

  function syncMonitorPanels() {
    $$("[data-monitor-panel]").forEach(panel => {
      panel.hidden = !monitorEnabled(panel.dataset.monitorPanel);
    });
    const monitorOpen = !$("[data-panel=monitor]")?.hidden;
    if (monitorOpen && selectedAudioChannels().length) startMonitor();
    else stopMonitor();
    if (monitorEnabled("logs") && Array.isArray(state.overview?.voice_events)) {
      renderVoiceLog(state.overview.voice_events, "#monitor-log");
    }
  }

  function startMonitor() {
    if (state.monitorTimer) return;
    pollMonitor();
    state.monitorTimer = window.setInterval(pollMonitor, 200);
  }

  function stopMonitor() {
    if (!state.monitorTimer) return;
    window.clearInterval(state.monitorTimer);
    state.monitorTimer = null;
  }

  async function pollMonitor() {
    const channels = selectedAudioChannels();
    if (!channels.length) {
      stopMonitor();
      return;
    }
    if (!state.csrf || document.hidden) return;
    try {
      const levels = await request(`/api/voice/monitor?channels=${channels.join(",")}`);
      state.levels = levels;
      if (channels.includes("input")) {
        const inputStatus = $("#monitor-input-status");
        if (inputStatus) inputStatus.textContent = levels.input_active ? "正在采集" : "当前没有输入";
      }
      if (channels.includes("output")) {
        const outputStatus = $("#monitor-output-status");
        if (outputStatus) outputStatus.textContent = levels.output_active ? "正在输出" : "当前没有输出";
      }
      drawLevels(levels);
    } catch (error) {
      if (state.csrf) showError(error.message);
    }
  }

  function drawLevels(levels) {
    if (!levels) return;
    if (monitorEnabled("input") && Object.prototype.hasOwnProperty.call(levels, "input")) {
      drawWaveform($("#waveform-input"), levels.input || [], levels.input_active);
    }
    if (monitorEnabled("output") && Object.prototype.hasOwnProperty.call(levels, "output")) {
      drawWaveform($("#waveform-output"), levels.output || [], levels.output_active);
    }
  }

  function drawWaveform(canvas, points, active) {
    if (!canvas || canvas.offsetParent === null) return;
    const width = canvas.clientWidth || 640;
    const height = canvas.clientHeight || 220;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.floor(width * ratio));
    canvas.height = Math.max(1, Math.floor(height * ratio));
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    context.fillStyle = "#101716";
    context.fillRect(0, 0, width, height);
    context.strokeStyle = "#2c3d3b";
    context.beginPath();
    context.moveTo(0, height / 2);
    context.lineTo(width, height / 2);
    context.stroke();
    if (!points.length) return;
    let peak = 0.02;
    for (const point of points) {
      peak = Math.max(peak, Math.abs(Number(point.min) || 0), Math.abs(Number(point.max) || 0));
    }
    const slot = width / points.length;
    context.fillStyle = active ? "#7ddec8" : "#4e6662";
    points.forEach((point, index) => {
      const min = (Number(point.min) || 0) / peak;
      const max = (Number(point.max) || 0) / peak;
      const top = (1 - Math.max(-1, Math.min(1, max))) * height / 2;
      const bottom = (1 - Math.max(-1, Math.min(1, min))) * height / 2;
      const barHeight = Math.max(1, bottom - top);
      context.fillRect(index * slot, top, Math.max(1, slot - 1), barHeight);
    });
  }

  function setFormStatus(selector, message, isError = false) {
    const node = $(selector);
    if (!node) return;
    node.textContent = message || "";
    node.classList.toggle("error", Boolean(isError && message));
  }

  function integerValue(selector) {
    const value = Number.parseInt($(selector).value, 10);
    if (!Number.isInteger(value)) throw new Error("请填写整数");
    return value;
  }

  function renderAccounts(accounts, currentUsername) {
    const root = $("#account-list");
    root.replaceChildren();
    if (!accounts.length) {
      root.append(empty("还没有登录账户"));
      return;
    }
    for (const account of accounts) {
      const row = document.createElement("div");
      row.className = "account-row";
      const name = document.createElement("strong");
      name.textContent = account.username;
      if (account.username === currentUsername) {
        const mark = document.createElement("span");
        mark.className = "subtle";
        mark.textContent = "当前登录";
        name.append(mark);
      }
      row.append(name);
      if (account.username !== currentUsername) {
        const remove = document.createElement("button");
        remove.className = "quiet-button";
        remove.type = "button";
        remove.textContent = "删除";
        remove.setAttribute("aria-label", `删除账户 ${account.username}`);
        remove.addEventListener("click", () => deleteAccount(account.username));
        row.append(remove);
      }
      root.append(row);
    }
  }

  function applySettings(data) {
    state.settings = data;
    const llm = data.llm || {};
    $("#llm-provider").value = llm.provider || "openai-compatible";
    $("#llm-model").value = llm.model || "";
    $("#llm-base-url").value = llm.base_url || "";
    $("#llm-timeout").value = llm.timeout_seconds || 30;
    $("#llm-api-key").value = "";
    $("#llm-api-key").placeholder = llm.api_key_configured ? "已保存，留空则保持不变" : "未设置";
    $("#llm-clear-key").checked = false;
    const audio = data.audio || {};
    $("#audio-robot-name").value = audio.robot_name || "";
    $("#audio-wake-phrase").value = audio.wake_phrase || "";
    $("#audio-language").value = audio.language || "";
    $("#audio-input-device").value = audio.input_device || "default";
    $("#audio-output-device").value = audio.output_device || "default";
    $("#audio-follow-up").value = audio.follow_up_seconds ?? "";
    $("#audio-session-max").value = audio.session_max_seconds ?? "";
    $("#audio-reconnect").value = audio.call_reconnect_grace_seconds ?? "";
    const models = audio.models || {};
    $("#audio-models").value = `KWS ${text(models.kws)}；ASR ${text(models.asr)}；VAD ${text(models.vad)}；TTS ${text(models.tts)}`;
    renderAccounts(data.accounts || [], data.current_username);
  }

  async function loadSettings() {
    try {
      applySettings(await request("/api/settings"));
    } catch (error) {
      if (state.csrf) showError(error.message);
    }
  }

  async function deleteAccount(username) {
    const currentPassword = $("#account-current-password").value;
    if (!currentPassword) {
      setFormStatus("#account-settings-status", "删除账户前请先填写当前账户密码", true);
      $("#account-current-password").focus();
      return;
    }
    try {
      const data = await request("/api/settings/accounts/delete", {
        method: "POST",
        body: JSON.stringify({ username, current_password: currentPassword }),
      });
      $("#account-current-password").value = "";
      renderAccounts(data.accounts || [], data.current_username);
      setFormStatus("#account-settings-status", "账户已删除", false);
    } catch (error) {
      setFormStatus("#account-settings-status", error.message, true);
    }
  }

  $("#login-form").addEventListener("submit", async event => {
    event.preventDefault();
    $("#login-error").textContent = "";
    const username = $("#operator-username").value;
    const password = $("#operator-password").value;
    try {
      const data = await request("/api/login", { method: "POST", body: JSON.stringify({ username, password }) });
      state.csrf = data.csrf_token;
      $("#operator-password").value = "";
      showDashboard();
      await refresh();
    } catch (error) {
      $("#login-error").textContent = error.message;
    }
  });

  $("#llm-settings-form").addEventListener("submit", async event => {
    event.preventDefault();
    const submit = $("#llm-settings-form button[type=submit]");
    submit.disabled = true;
    setFormStatus("#llm-settings-status", "");
    try {
      const payload = {
        provider: $("#llm-provider").value,
        base_url: $("#llm-base-url").value,
        model: $("#llm-model").value,
        timeout_seconds: integerValue("#llm-timeout"),
      };
      const apiKey = $("#llm-api-key").value;
      if (apiKey) payload.api_key = apiKey;
      else if ($("#llm-clear-key").checked) payload.api_key = "";
      const data = await request("/api/settings/llm", { method: "POST", body: JSON.stringify(payload) });
      state.settings = { ...(state.settings || {}), llm: data.llm };
      $("#llm-api-key").value = "";
      $("#llm-api-key").placeholder = data.llm?.api_key_configured ? "已保存，留空则保持不变" : "未设置";
      $("#llm-clear-key").checked = false;
      setFormStatus("#llm-settings-status", data.llm?.api_key_configured ? "模型设置已保存，密钥不会回显" : "模型设置已保存", false);
    } catch (error) {
      setFormStatus("#llm-settings-status", error.message, true);
    } finally {
      submit.disabled = false;
    }
  });

  $("#audio-settings-form").addEventListener("submit", async event => {
    event.preventDefault();
    const submit = $("#audio-settings-form button[type=submit]");
    submit.disabled = true;
    setFormStatus("#audio-settings-status", "");
    try {
      const data = await request("/api/settings/audio", {
        method: "POST",
        body: JSON.stringify({
          robot_name: $("#audio-robot-name").value,
          wake_phrase: $("#audio-wake-phrase").value,
          language: $("#audio-language").value,
          input_device: $("#audio-input-device").value,
          output_device: $("#audio-output-device").value,
          follow_up_seconds: integerValue("#audio-follow-up"),
          session_max_seconds: integerValue("#audio-session-max"),
          call_reconnect_grace_seconds: integerValue("#audio-reconnect"),
        }),
      });
      state.settings = { ...(state.settings || {}), audio: data.audio };
      const models = data.audio?.models || {};
      $("#audio-models").value = `KWS ${text(models.kws)}；ASR ${text(models.asr)}；VAD ${text(models.vad)}；TTS ${text(models.tts)}`;
      setFormStatus("#audio-settings-status", "音频设置已保存。唤醒短语、语言和设备将在下次启动语音服务后生效", false);
    } catch (error) {
      setFormStatus("#audio-settings-status", error.message, true);
    } finally {
      submit.disabled = false;
    }
  });

  $("#account-settings-form").addEventListener("submit", async event => {
    event.preventDefault();
    const password = $("#account-password").value;
    if (password !== $("#account-password-confirm").value) {
      setFormStatus("#account-settings-status", "两次输入的新密码不一致", true);
      return;
    }
    const submit = $("#account-settings-form button[type=submit]");
    submit.disabled = true;
    setFormStatus("#account-settings-status", "");
    try {
      const data = await request("/api/settings/accounts", {
        method: "POST",
        body: JSON.stringify({
          username: $("#account-username").value,
          password,
          current_password: $("#account-current-password").value,
        }),
      });
      $("#account-password").value = "";
      $("#account-password-confirm").value = "";
      $("#account-current-password").value = "";
      renderAccounts(data.accounts || [], data.current_username);
      setFormStatus("#account-settings-status", "账户已保存", false);
    } catch (error) {
      setFormStatus("#account-settings-status", error.message, true);
    } finally {
      submit.disabled = false;
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
  $$("input[data-monitor]").forEach(input => input.addEventListener("change", () => {
    syncMonitorPanels();
    if (input.dataset.monitor === "logs" && input.checked && state.csrf && !Array.isArray(state.overview?.voice_events)) refresh();
  }));
  window.addEventListener("resize", () => { if (state.levels) drawLevels(state.levels); });
  $$('[data-open-view]').forEach(button => button.addEventListener("click", () => selectView(button.dataset.openView)));

  request("/api/session").then(data => {
    if (data.authenticated) {
      state.csrf = data.csrf_token;
      showDashboard();
      refresh();
    } else showLogin();
  }).catch(() => showLogin());
})();
