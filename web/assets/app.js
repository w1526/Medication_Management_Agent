(function () {
  "use strict";

  var ROLE_META = {
    elder: {
      label: "老人端",
      overline: "ELDER · SIMPLE TODAY",
      title: "今天的用药，安心过一天。",
      lead: "大字、少步骤：看提醒、确认吃药，家属和医生会同步收到结果。",
      path: "/elder"
    },
    family: {
      label: "家属端",
      overline: "FAMILY · CARE DESK",
      title: "把照护安排好，家人更放心。",
      lead: "建立用药计划、跟进提醒，把每一次服药确认同步给医生和老人。",
      path: "/family"
    },
    doctor: {
      label: "医生端",
      overline: "DOCTOR · CLINICAL REVIEW",
      title: "用一份清晰记录，守住用药安全。",
      lead: "审核计划、查看依从情况，所有调整和反馈都留在同一条可追溯链路上。",
      path: "/doctor"
    }
  };

  var state = {
    role: detectRole(),
    elderId: "E001",
    tasks: [],
    plans: [],
    events: [],
    notifications: [],
    escalations: [],
    seenNotifications: {},
    lastAgentResult: null,
    refreshGeneration: 0,
    refreshController: null,
    refreshPromise: null,
    notificationController: null,
    notificationPromise: null,
    notificationElderId: null
  };

  function $(selector) {
    return document.querySelector(selector);
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function detectRole() {
    var path = window.location.pathname.replace(/\/+$/, "") || "/";
    if (path === "/elder" || path.endsWith("/elder")) return "elder";
    if (path === "/doctor" || path.endsWith("/doctor")) return "doctor";
    return "family";
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function api(path, options) {
    var config = options || {};
    config.headers = Object.assign({ "Content-Type": "application/json" }, config.headers || {});
    return fetch(path, config).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) throw new Error(data.error || "请求失败（HTTP " + response.status + "）");
        return data;
      });
    });
  }

  function post(path, payload) {
    return api(path, { method: "POST", body: JSON.stringify(payload || {}) });
  }

  function readValue(id, fallback) {
    var node = byId(id);
    var value = node && typeof node.value === "string" ? node.value.trim() : "";
    return value || fallback || "";
  }

  function setText(id, value) {
    var node = byId(id);
    if (node) node.textContent = value == null ? "" : value;
  }

  function shanghaiDateTime() {
    var parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false
    }).formatToParts(new Date()).reduce(function (out, item) {
      out[item.type] = item.value;
      return out;
    }, {});
    var hour = Number(parts.hour);
    if (hour === 24) hour = 0;
    return {
      date: parts.year + "-" + parts.month + "-" + parts.day,
      hour: hour,
      minute: Number(parts.minute)
    };
  }

  function setDefaults() {
    var current = shanghaiDateTime();
    var minute = current.minute + 3;
    var hour = current.hour;
    if (minute >= 60) {
      minute -= 60;
      hour = (hour + 1) % 24;
    }
    var startDate = byId("start-date");
    var scheduleTime = byId("schedule-time");
    if (startDate && !startDate.value) startDate.value = current.date;
    if (scheduleTime && !scheduleTime.value) {
      scheduleTime.value = String(hour).padStart(2, "0") + ":" + String(minute).padStart(2, "0");
    }
    setText("today-label", current.date);
  }

  function clock(value) {
    if (!value) return "—";
    try {
      return new Date(value).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hour12: false
      });
    } catch (_) {
      return value;
    }
  }

  function shortTime(value) {
    if (!value) return "—";
    try {
      return new Date(value).toLocaleTimeString("zh-CN", {
        timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false
      });
    } catch (_) {
      return String(value).slice(11, 16);
    }
  }

  function formatScheduleTime(value) {
    if (!value) return "—";
    var match = String(value).trim().match(/^(\d{1,2})(?::(\d{1,2}))?$/);
    if (!match) return String(value);
    return String(Number(match[1])).padStart(2, "0") + ":" + String(Number(match[2] || 0)).padStart(2, "0");
  }

  function formatDateLabel(value) {
    var match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
    return match ? match[1] + "年" + match[2] + "月" + match[3] + "日" : (value || "—");
  }

  function statusInfo(status) {
    var map = {
      draft: ["草稿", "soft-slate"],
      pending_confirmation: ["待医生确认", "soft-blue"],
      active: ["生效中", "soft-green"],
      paused: ["已暂停", "soft-slate"],
      completed: ["历史版本", "soft-slate"],
      unconfirmed: ["待确认", "soft-blue"],
      confirmed_taken: ["已服用", "soft-green"],
      skipped: ["已跳过", "soft-slate"],
      closed_unconfirmed: ["未确认", "soft-slate"],
      cancelled: ["已失效", "soft-slate"],
      OPEN: ["待处理", "soft-amber"],
      ACKNOWLEDGED: ["已接手", "soft-blue"],
      RESOLVED: ["已关闭", "soft-green"],
      CANCELLED: ["已取消", "soft-slate"],
      EXHAUSTED: ["待人工处理", "soft-amber"],
      PASS: ["PASS · 当前规则范围内通过", "soft-green"],
      WARN: ["WARN · 可继续但有警告", "soft-amber"],
      BLOCK: ["BLOCK · 禁止调度", "soft-red"],
      CHECK_FAILED: ["CHECK_FAILED · 未放行", "soft-red"],
      NOT_CHECKED: ["未检查", "soft-slate"]
    };
    return map[status] || [status || "未知", "soft-slate"];
  }

  function badge(status) {
    var info = statusInfo(status);
    return '<span class="soft-badge ' + info[1] + '">' + escapeHtml(info[0]) + "</span>";
  }

  function toast(message, kind) {
    var container = byId("toast-container");
    if (!container) return;
    var node = document.createElement("div");
    var tone = kind === "error" ? "danger" : kind === "reminder" ? "warning" : "dark";
    node.className = "toast show align-items-center text-bg-" + tone + (kind === "reminder" ? " reminder-toast" : "");
    node.setAttribute("role", "alert");
    node.innerHTML = '<div class="d-flex"><div class="toast-body">' + escapeHtml(message) + '</div><button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button></div>';
    container.appendChild(node);
    window.setTimeout(function () { node.remove(); }, 3800);
  }

  function reminderLabel(item) {
    return (item.drug_name_snapshot || "用药") + " " + (item.dosage_snapshot || "");
  }

  function beep() {
    try {
      var AudioContext = window.AudioContext || window.webkitAudioContext;
      if (!AudioContext) return;
      var context = new AudioContext();
      var oscillator = context.createOscillator();
      var gain = context.createGain();
      oscillator.type = "sine";
      oscillator.frequency.value = 880;
      gain.gain.setValueAtTime(0.001, context.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.16, context.currentTime + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, context.currentTime + 0.42);
      oscillator.connect(gain).connect(context.destination);
      oscillator.start();
      oscillator.stop(context.currentTime + 0.45);
      window.setTimeout(function () { context.close(); }, 600);
    } catch (_) {
      // Browsers may block audio until the page has a user gesture.
    }
  }

  function announceReminder(item) {
    var message = "该吃药了：" + reminderLabel(item);
    toast(message, "reminder");
    if (state.role === "elder") beep();
    if (typeof Notification !== "undefined" && Notification.permission === "granted") {
      try {
        new Notification("用药提醒", { body: message, tag: item.notification_id });
      } catch (_) {
        // Visual reminder remains available.
      }
    }
  }

  function enableNotifications() {
    if (typeof Notification === "undefined") {
      toast("当前浏览器不支持系统通知，页面提醒仍会正常显示", "error");
      return;
    }
    Notification.requestPermission().then(function (permission) {
      toast(permission === "granted" ? "系统通知已开启" : "未开启系统通知，页面提醒仍会正常显示");
    });
  }

  function canRespond() {
    return state.role === "elder" || state.role === "family";
  }

  function renderNotifications() {
    var center = byId("reminder-center");
    var list = byId("reminder-list");
    if (!center || !list) return;
    if (!state.notifications.length) {
      center.classList.add("d-none");
      list.innerHTML = "";
      renderWorkflow();
      return;
    }
    center.classList.remove("d-none");
    list.innerHTML = state.notifications.map(function (item) {
      var relation = item.relation_to_meal_snapshot ? " · " + escapeHtml(item.relation_to_meal_snapshot) : "";
      var actions = canRespond() ?
        '<div class="reminder-actions">' +
        '<button class="btn btn-reminder-taken" data-reminder-action="taken" data-interaction="' + escapeHtml(item.interaction_id) + '">吃了</button>' +
        '<button class="btn btn-reminder-delay" data-reminder-action="delay" data-interaction="' + escapeHtml(item.interaction_id) + '">晚点</button>' +
        '<button class="btn btn-reminder-skip" data-reminder-action="skip" data-interaction="' + escapeHtml(item.interaction_id) + '">跳过</button>' +
        '</div>' : '<span class="view-only-note">等待老人确认</span>';
      return '<article class="reminder-item" aria-label="用药提醒">' +
        '<div class="reminder-icon">⏰</div>' +
        '<div class="reminder-main"><div class="reminder-kicker">现在需要确认</div>' +
        '<div class="reminder-title">' + escapeHtml(reminderLabel(item)) + '</div>' +
        '<div class="reminder-detail">' + escapeHtml(item.elder_id) + relation + ' · ' + escapeHtml(item.text || "请按计划服药") + '</div></div>' +
        actions +
        '</article>';
    }).join("");
    renderWorkflow();
  }

  function applyNotifications(items, elderId) {
    var currentElderId = readValue("elder-id", "E001");
    if (elderId && currentElderId !== elderId) return;
    (items || []).forEach(function (item) {
      if (!state.seenNotifications[item.notification_id]) {
        state.seenNotifications[item.notification_id] = true;
        announceReminder(item);
      }
    });
    state.notifications = items || [];
    renderNotifications();
    renderSharedContext();
  }

  function pollNotifications() {
    var elderId = readValue("elder-id", "E001");
    if (state.refreshPromise && state.elderId === elderId) return state.refreshPromise;
    if (state.notificationPromise && state.notificationElderId === elderId) return state.notificationPromise;
    if (state.notificationController && state.notificationController.abort) state.notificationController.abort();
    var controller = typeof AbortController === "undefined" ? null : new AbortController();
    state.notificationController = controller;
    state.notificationElderId = elderId;
    var options = controller ? { signal: controller.signal } : {};
    var promise = api("/api/v1/medication/notifications?elder_id=" + encodeURIComponent(elderId), options)
      .then(function (result) { applyNotifications(result.items || [], elderId); })
      .catch(function (error) {
        if (error && error.name === "AbortError") return;
        // The main refresh path reports service errors; background polling stays quiet.
      })
      .finally(function () {
        if (state.notificationPromise === promise) {
          state.notificationPromise = null;
          state.notificationController = null;
          state.notificationElderId = null;
        }
      });
    state.notificationPromise = promise;
    return promise;
  }

  function handleReminderAction(action, interactionId) {
    var delayMinutes = null;
    if (action === "DELAY") {
      var value = window.prompt("延后多少分钟？", "30");
      if (!value || Number(value) <= 0) return;
      delayMinutes = Number(value);
    }
    respond(action, interactionId, delayMinutes);
  }

  function setConnection(online) {
    var dot = byId("connection-dot");
    if (dot) dot.className = "status-dot " + (online ? "online" : "offline");
    setText("connection-label", online ? "服务在线" : "服务不可用");
  }

  function renderWorkflow() {
    var container = byId("workflow-steps");
    var summary = byId("workflow-summary");
    if (!container || !summary) return;
    var steps = container.querySelectorAll(".workflow-step");
    var hasDraft = state.plans.some(function (plan) {
      return plan.status === "draft" || plan.status === "pending_confirmation";
    });
    var hasActive = state.plans.some(function (plan) { return plan.status === "active"; });
    var hasPendingTask = state.tasks.some(function (task) { return task.intake_status === "unconfirmed"; });
    var hasOpenReminder = state.notifications.length > 0;
    var hasTaken = state.tasks.some(function (task) { return task.intake_status === "confirmed_taken"; });
    var currentStep = 1;
    var summaryText = "先由家属建立一份清晰的用药计划。";
    var allDone = false;

    if (hasOpenReminder) {
      currentStep = 4;
      summaryText = state.role === "doctor" ? "提醒已发出，等待老人完成本次服药确认。" : "提醒已经出现，请在提醒卡片或今日任务中点击“吃了”。";
    } else if (hasDraft) {
      currentStep = 2;
      summaryText = state.role === "doctor" ? "有计划等待审核，请核对剂量、时间与服用关系。" : "计划草稿已生成，医生审核后才会进入调度。";
    } else if (hasTaken && !hasPendingTask) {
      currentStep = 4;
      allDone = true;
      summaryText = "今天这次用药已经确认，闭环完成。";
    } else if (hasActive && hasPendingTask) {
      currentStep = 3;
      summaryText = "计划已生效；到点后会自动提醒。测试时也可以运行一轮调度。";
    } else if (hasActive) {
      currentStep = 3;
      summaryText = "计划已审批生效，系统正在等待下一次提醒时间。";
    }

    summary.textContent = summaryText;
    Array.prototype.forEach.call(steps, function (step) {
      var number = Number(step.getAttribute("data-step"));
      var done = allDone || number < currentStep;
      var current = !done && number === currentStep;
      step.classList.toggle("is-done", done);
      step.classList.toggle("is-current", current);
      step.setAttribute("aria-current", current ? "step" : "false");
    });
  }

  function renderMetrics() {
    var total = state.tasks.length;
    var taken = state.tasks.filter(function (item) { return item.intake_status === "confirmed_taken"; }).length;
    var pending = state.tasks.filter(function (item) { return item.intake_status === "unconfirmed"; }).length;
    var finished = state.tasks.filter(function (item) {
      return ["confirmed_taken", "skipped", "closed_unconfirmed"].indexOf(item.intake_status) >= 0;
    }).length;
    var rate = finished ? Math.round(taken / finished * 100) + "%" : "—";
    setText("metric-total", total);
    setText("metric-taken", taken);
    setText("metric-pending", pending);
    setText("metric-rate", rate);
  }

  function renderHeroState() {
    var value = "—";
    var label = "共享状态";
    var note = "数据正在同步";
    if (state.role === "elder") {
      var next = state.tasks.find(function (task) { return task.intake_status === "unconfirmed"; });
      value = next ? shortTime(next.scheduled_at) : "完成";
      label = next ? "下一次服用" : "今日状态";
      note = next ? reminderLabel(next) : "今天暂时没有待确认任务";
    } else if (state.role === "family") {
      var drafts = state.plans.filter(function (plan) { return plan.status === "draft" || plan.status === "pending_confirmation"; }).length;
      value = String(drafts);
      label = "待协同计划";
      note = drafts ? "等待医生确认后才会调度" : "目前没有待协同计划";
    } else {
      var waiting = state.plans.filter(function (plan) { return plan.status === "draft" || plan.status === "pending_confirmation"; }).length;
      value = String(waiting);
      label = "待审核计划";
      note = waiting ? "请核对后审批生效" : "目前没有待审核计划";
    }
    setText("hero-stat-value", value);
    setText("hero-stat-label", label);
    setText("hero-stat-note", note);
  }

  function renderTasks() {
    var container = byId("task-list");
    if (!container) return;
    if (!state.tasks.length) {
      container.innerHTML = '<div class="empty-state"><span class="empty-icon">◷</span>今天还没有生成用药任务<br><small>医生审批一个计划后，未来 7 天任务会自动出现</small></div>';
      return;
    }
    container.innerHTML = state.tasks.map(function (task) {
      var interaction = (task.interactions || []).slice().reverse().find(function (item) { return item.status === "open"; });
      var actions = "";
      if (task.intake_status === "unconfirmed" && interaction && canRespond()) {
        actions = '<div class="task-actions">' +
          '<button class="btn btn-taken" data-action="taken" data-interaction="' + escapeHtml(interaction.interaction_id) + '">吃了</button>' +
          '<button class="btn btn-delay" data-action="delay" data-interaction="' + escapeHtml(interaction.interaction_id) + '">晚点</button>' +
          '<button class="btn btn-skip" data-action="skip" data-interaction="' + escapeHtml(interaction.interaction_id) + '">跳过</button>' +
          '</div>';
      } else if (task.intake_status === "unconfirmed") {
        actions = '<span class="text-muted small">等待提醒或老人回应</span>';
      }
      var relation = task.relation_to_meal_snapshot ? " · " + escapeHtml(task.relation_to_meal_snapshot) : "";
      return '<div class="task-row">' +
        '<div class="task-time">' + escapeHtml(shortTime(task.scheduled_at)) + '</div>' +
        '<div class="task-main"><div class="task-drug">' + escapeHtml(task.drug_name_snapshot) + ' <span class="text-muted fw-normal">' + escapeHtml(task.dosage_snapshot) + '</span></div>' +
        '<div class="task-detail">第 ' + escapeHtml(task.plan_version) + ' 版' + relation + ' · ' + escapeHtml(clock(task.scheduled_at).split(" ")[0] || "今日") + '</div></div>' +
        '<div class="task-status">' + badge(task.intake_status) + '</div>' + actions +
        '</div>';
    }).join("");
  }

  function renderPlans() {
    var body = byId("plan-list");
    if (!body) return;
    var count = byId("plan-count");
    if (count) count.textContent = state.plans.length + " 个版本";
    if (!state.plans.length) {
      body.innerHTML = '<tr><td colspan="5"><div class="empty-state">还没有计划</div></td></tr>';
      return;
    }
    body.innerHTML = state.plans.map(function (plan) {
      var action = "";
      if (state.role === "doctor" && (plan.status === "draft" || plan.status === "pending_confirmation")) {
        action = '<button class="btn btn-sm btn-primary" data-action="approve" data-plan="' + escapeHtml(plan.plan_id) + '" data-version="' + escapeHtml(plan.version) + '">审批生效</button>';
      } else if (state.role === "family" && plan.status === "draft") {
        action = '<button class="btn btn-sm btn-primary btn-submit-plan" data-action="submit" data-plan="' + escapeHtml(plan.plan_id) + '" data-version="' + escapeHtml(plan.version) + '">提交医生</button>';
      } else if (state.role === "doctor" && plan.status === "active") {
        action = '<button class="btn btn-sm btn-ghost-secondary" data-action="pause" data-plan="' + escapeHtml(plan.plan_id) + '" data-version="' + escapeHtml(plan.version) + '">暂停</button>';
      } else if (plan.status === "pending_confirmation") {
        action = '<span class="table-note">等待医生</span>';
      }
      var safety = plan.safety_check || {};
      var safetyStatus = plan.safety_status || safety.status || "NOT_CHECKED";
      var findings = safety.findings || [];
      var coverage = safety.coverage || {};
      var coverageText = Object.keys(coverage).map(function (key) {
        return key + ": " + coverage[key];
      }).join(" · ");
      var safetyDetails = '<div class="plan-safety-details">' +
        '<div>检查时间：' + escapeHtml(clock(safety.checked_at)) + ' · ruleset：' + escapeHtml(safety.ruleset_version || "—") + '</div>' +
        '<div>coverage：' + escapeHtml(coverageText || "—") + '</div>' +
        (findings.length ? '<details><summary>' + escapeHtml(findings.length + " 条 finding") + '</summary>' + findings.map(function (finding) {
          return '<div class="safety-finding"><span>' + escapeHtml(finding.severity || "") + '</span> ' + escapeHtml(finding.code || "") + ' · ' + escapeHtml(finding.message || "") + '</div>';
        }).join("") + '</details>' : '<div>findings：0</div>') +
        '</div>';
      var safetyMarkup = '<div class="plan-safety">' + badge(safetyStatus) + safetyDetails + '</div>';
      return '<tr><td><div class="plan-drug">' + escapeHtml(plan.drug_name) + '</div><div class="plan-dose">' + escapeHtml(plan.dosage_text) + ' · ' + escapeHtml(plan.elder_id) + '</div>' + safetyMarkup + '</td>' +
        '<td>' + escapeHtml(formatScheduleTime(plan.schedule_time)) + '</td><td>v' + escapeHtml(plan.version) + '</td><td>' + badge(plan.status) + '</td><td>' + action + '</td></tr>';
    }).join("");
  }

  function eventLabel(type) {
    var map = {
      "medication.plan.created": "创建计划草稿",
      "medication.plan.pending_confirmation": "计划进入待确认",
      "medication.plan.approved": "计划审批生效",
      "medication.plan.paused": "计划已暂停",
      "medication.reminder_due": "到点提醒已生成",
      "device.interaction.request": "提醒已发给老人",
      "medication.user_response": "收到老人回复",
      "medication.intake.updated": "服药状态已更新",
      "medication.reminder.delayed": "提醒已延后",
      "medication.intake.unconfirmed": "任务关闭为未确认",
      "medication.escalation.opened": "异常升级已创建",
      "medication.escalation.escalated": "异常升级已升级",
      "caregiver.task.assign": "护工任务已模拟派发",
      "family_notify.request": "家属通知已模拟派发",
      "manual_review.request": "人工复核已模拟派发",
      "medication.escalation.acknowledged": "异常已确认接手",
      "medication.escalation.resolved": "异常已关闭",
      "medication.safety.checked": "安全检查已完成",
      "medication.safety.warning": "安全检查存在警告",
      "medication.safety.blocked": "安全检查阻断计划",
      "medication.safety.check_failed": "安全检查失败，未放行"
    };
    return map[type] || type;
  }

  function escalationLevelLabel(level) {
    return {
      CAREGIVER: "护工",
      FAMILY: "家属",
      MANUAL_REVIEW: "人工复核",
      EMERGENCY: "预留高优先级"
    }[level] || level || "—";
  }

  function renderEscalations() {
    var container = byId("escalation-list");
    if (!container) return;
    if (!state.escalations.length) {
      container.innerHTML = '<div class="event-empty">暂无异常升级事件</div>';
      return;
    }
    container.innerHTML = state.escalations.map(function (item) {
      var medication = item.medication || {};
      var deadline = item.status === "ACKNOWLEDGED" ? item.resolution_deadline_at : item.next_escalation_at;
      var deadlineLabel = item.status === "ACKNOWLEDGED" ? "处理截止 " : "下一截止 ";
      var timing = deadline ? " · " + deadlineLabel + clock(deadline) : "";
      var action = "";
      if (item.status === "OPEN") {
        action = '<button class="btn btn-sm btn-outline-primary" data-escalation-action="acknowledge" data-escalation="' + escapeHtml(item.escalation_id) + '">确认接手</button>';
      } else if (item.status === "ACKNOWLEDGED") {
        action = '<button class="btn btn-sm btn-primary" data-escalation-action="resolve" data-escalation="' + escapeHtml(item.escalation_id) + '">关闭事件</button>';
      }
      return '<div class="escalation-row"><div class="escalation-main"><div class="escalation-title">' +
        escapeHtml(medication.name || item.drug_name_snapshot || "用药异常") + ' ' + escapeHtml(medication.dose || item.dosage_snapshot || "") + '</div>' +
        '<div class="escalation-detail">' + escapeHtml(item.reason || "—") + ' · ' + escapeHtml(escalationLevelLabel(item.current_level)) + timing + '</div>' +
        '<div class="escalation-meta">' + escapeHtml(item.elder_id) + ' · 开始 ' + escapeHtml(clock(item.opened_at)) + '</div></div>' +
        '<div class="escalation-status">' + badge(item.status) + action + '</div></div>';
    }).join("");
  }

  function renderEvents() {
    var container = byId("event-list");
    if (!container) return;
    if (!state.events.length) {
      container.innerHTML = '<div class="event-empty">暂无事件记录</div>';
      return;
    }
    container.innerHTML = state.events.slice(0, 10).map(function (event) {
      return '<div class="event-item"><span class="event-dot"></span><div><div class="event-type">' + escapeHtml(eventLabel(event.event_type)) + '</div>' +
        '<div class="event-meta"><span>' + escapeHtml(event.source || "系统") + '</span><span>' + escapeHtml(clock(event.occurred_at)) + '</span></div></div></div>';
    }).join("");
  }

  function renderAgentStatus(status) {
    var dot = byId("agent-status-dot");
    var label = byId("agent-status-label");
    var hint = byId("agent-hint");
    if (!dot || !label || !hint) return;
    if (!status || !status.sdk_installed) {
      dot.className = "status-dot offline";
      label.textContent = "需 Python 3.10+ 与 SDK";
      hint.textContent = "当前 Harness SDK 未安装；普通计划和调度仍可继续使用。";
    } else if (!status.key_configured) {
      dot.className = "status-dot";
      label.textContent = "等待 DEEPSEEK_API_KEY";
      hint.textContent = "在启动服务的终端设置 Key 后刷新页面。";
    } else if (status.ready) {
      dot.className = "status-dot online";
      label.textContent = "Harness 已就绪 · " + (status.model || "DeepSeek");
      hint.textContent = "自然语言只会生成 Draft，需医生审批后才会调度。";
    } else {
      dot.className = "status-dot offline";
      label.textContent = "Harness 未就绪";
      hint.textContent = "请检查 Python 环境、SDK 和 API Key。";
    }
  }

  function renderAgentResult(result, kind) {
    var container = byId("agent-result");
    if (!container || !result) return;
    var mode = kind || "success";
    var content = "";
    if (result.kind === "plan_draft_created") {
      var draft = result.draft || {};
      var notes = [];
      var isPendingApproval = draft.status === "draft" || draft.status === "pending_confirmation";
      var planStatus = statusInfo(draft.status);
      var resultTitle = isPendingApproval ? "已生成计划草稿" : draft.status === "active" ? "计划已审批生效" : "计划状态已更新";
      var statusLabel = isPendingApproval ? "待审批" : planStatus[0];
      var nextStep;
      if (isPendingApproval && state.role === "family") {
        nextStep = '下一步：核对信息后提交给医生。 <button class="btn btn-sm btn-primary" data-action="scroll-plans">去查看计划</button>';
      } else if (isPendingApproval && state.role === "doctor") {
        nextStep = "计划已经进入共享列表，请在下方核对并审批。";
      } else if (isPendingApproval) {
        nextStep = "计划草稿已同步给家属和医生，请由家属核对、医生审批。";
      } else if (draft.status === "active") {
        nextStep = "审批已完成，计划已经生效，系统将按计划生成用药提醒。";
      } else {
        nextStep = "当前计划状态已更新，请在角色工作台中查看详情。";
      }
      if (result.start_date_defaulted) notes.push("未提供开始日期，已默认从今天开始");
      if (result.schedule_time_normalized) notes.push("已将中文时段换算为 24 小时制");
      content = '<div class="agent-result-title"><strong>' + escapeHtml(resultTitle) + '</strong><span class="soft-badge ' + escapeHtml(planStatus[1]) + '">' + escapeHtml(statusLabel) + '</span></div>' +
        '<div class="agent-draft-grid">' +
        '<div><span>药品</span><strong>' + escapeHtml(draft.drug_name) + " " + escapeHtml(draft.dosage_text) + '</strong></div>' +
        '<div><span>每日时间</span><strong class="agent-time-value">' + escapeHtml(formatScheduleTime(draft.schedule_time)) + '</strong></div>' +
        '<div><span>开始日期</span><strong>' + escapeHtml(formatDateLabel(draft.start_date)) + '</strong></div>' +
        '<div><span>服用关系</span><strong>' + escapeHtml(draft.relation_to_meal || "不指定") + '</strong></div>' +
        '</div>' + (notes.length ? '<div class="agent-result-note">✓ ' + escapeHtml(notes.join("；")) + '</div>' : '') +
        '<div class="agent-result-next">' + nextStep + '</div>';
    } else if (result.kind === "plan_clarification") {
      mode = "warning";
      content = "<strong>需要补充信息：</strong>" + escapeHtml(result.message || (result.missing_fields || []).join("、"));
    } else if (result.kind === "medication_response") {
      content = "<strong>已处理老人回复：</strong>" + escapeHtml(result.action || "已确认") + "，对应任务状态已由业务服务更新。";
    } else {
      content = escapeHtml(result.message || "智能体已返回结果。");
    }
    container.className = "agent-result " + mode;
    container.innerHTML = content;
  }

  function syncAgentPlanResult() {
    var result = state.lastAgentResult;
    if (!result || result.kind !== "plan_draft_created" || !result.draft) return;
    var draft = result.draft;
    var currentPlan = state.plans.find(function (plan) {
      return plan.plan_id === draft.plan_id && Number(plan.version) === Number(draft.version);
    });
    if (!currentPlan) return;
    state.lastAgentResult = Object.assign({}, result, { draft: Object.assign({}, draft, currentPlan) });
    renderAgentResult(state.lastAgentResult);
  }

  function renderSharedContext() {
    var pending = state.tasks.filter(function (task) { return task.intake_status === "unconfirmed"; }).length;
    var open = state.notifications.length;
    var active = state.plans.filter(function (plan) { return plan.status === "active"; }).length;
    var latest = state.events[0];
    var summary = open ? open + " 条提醒等待确认" : pending ? "今日还有 " + pending + " 项待处理" : "今日暂无待确认提醒";
    setText("sync-summary", summary + " · " + active + " 个生效计划");
    setText("shared-status-title", open ? "有新的提醒需要老人确认" : "三方协同已连接");
    setText("shared-status-detail", latest ? "最近同步：" + eventLabel(latest.event_type) + " · " + clock(latest.occurred_at) : "家属、医生和老人看到的是同一份计划、提醒与审计记录。");
    setText("shared-status-badge", open ? "待确认" : "同步中");
    var shared = byId("shared-status");
    if (shared) shared.classList.toggle("has-alert", open > 0);
  }

  function refresh() {
    var elderId = readValue("elder-id", "E001");
    state.elderId = elderId;
    state.refreshGeneration += 1;
    var generation = state.refreshGeneration;
    if (state.refreshController && state.refreshController.abort) state.refreshController.abort();
    if (state.notificationController && state.notificationController.abort) state.notificationController.abort();
    var controller = typeof AbortController === "undefined" ? null : new AbortController();
    state.refreshController = controller;
    var options = controller ? { signal: controller.signal } : {};
    var promise = api("/api/v1/medication/dashboard?elder_id=" + encodeURIComponent(elderId) + "&limit=30", options)
      .then(function (result) {
        if (generation !== state.refreshGeneration || state.elderId !== elderId) return;
        setConnection(true);
        state.tasks = (result.today && result.today.items) || [];
        state.plans = (result.plans && result.plans.items) || [];
        state.events = (result.events && result.events.items) || [];
        state.escalations = (result.escalations && result.escalations.items) || [];
        renderAgentStatus(result.agent);
        syncAgentPlanResult();
        applyNotifications(result.notifications && result.notifications.items, elderId);
        renderMetrics();
        renderHeroState();
        renderTasks();
        renderPlans();
        renderEvents();
        renderEscalations();
        renderWorkflow();
        renderSharedContext();
      })
      .catch(function (error) {
        if (error && error.name === "AbortError") return;
        if (generation === state.refreshGeneration) {
          setConnection(false);
          toast(error.message, "error");
        }
      })
      .finally(function () {
        if (state.refreshPromise === promise) {
          state.refreshPromise = null;
          state.refreshController = null;
        }
      });
    state.refreshPromise = promise;
    return promise;
  }

  function createPlan(event) {
    event.preventDefault();
    var payload = {
      elder_id: state.elderId,
      drug_name: readValue("drug-name"),
      dosage_text: readValue("dosage-text"),
      schedule_time: readValue("schedule-time"),
      start_date: readValue("start-date"),
      relation_to_meal: readValue("relation-to-meal") || null,
      created_by: readValue("created-by", "family:F001")
    };
    if (!payload.drug_name || !payload.dosage_text || !payload.schedule_time || !payload.start_date) {
      toast("请把药品、剂量、时间和开始日期填写完整", "error");
      return;
    }
    post("/api/v1/medication/plans/draft", payload)
      .then(function () {
        toast("计划草稿已创建，请提交给医生确认");
        return refresh();
      })
      .then(function () {
        var list = byId("plan-lifecycle");
        if (list) list.scrollIntoView({ behavior: "smooth", block: "center" });
      })
      .catch(function (error) { toast(error.message, "error"); });
  }

  function sendAgent(event) {
    event.preventDefault();
    var text = readValue("agent-text");
    if (!text) {
      toast("请先输入一句话", "error");
      return;
    }
    var defaultCreator = state.role === "elder" ? "elder:" + state.elderId : "family:F001";
    post("/api/v1/medication/agent/message", {
      elder_id: state.elderId,
      text: text,
      source: state.role + "_ui",
      created_by: readValue("created-by", defaultCreator)
    }).then(function (result) {
      state.lastAgentResult = result;
      renderAgentResult(result);
      toast(result.message || "智能体处理完成");
      var input = byId("agent-text");
      if (input) input.value = "";
      return refresh().then(function () {
        if (result.kind === "plan_draft_created") {
          var list = byId("plan-lifecycle");
          if (list) list.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      });
    }).catch(function (error) {
      renderAgentResult({ message: error.message }, "error");
      toast(error.message, "error");
    });
  }

  function runScheduler() {
    post("/api/v1/medication/scheduler/run", {}).then(function (result) {
      var count = (result.reminders_claimed || []).length;
      toast(count ? "已生成 " + count + " 条到点提醒" : "调度完成，当前没有新的到点提醒");
      return refresh();
    }).catch(function (error) { toast(error.message, "error"); });
  }

  function respond(action, interactionId, delayMinutes) {
    var payload = {
      elder_id: state.elderId,
      interaction_id: interactionId,
      action: action,
      source: state.role + "_ui"
    };
    if (delayMinutes) payload.delay_minutes = delayMinutes;
    post("/api/v1/medication/responses", payload).then(function () {
      toast(action === "CONFIRM_TAKEN" ? "已记录：吃了" : action === "SKIP" ? "已记录：跳过" : "提醒已延后");
      return refresh();
    }).catch(function (error) { toast(error.message, "error"); });
  }

  function submitPlan(button) {
    post("/api/v1/medication/plans/" + encodeURIComponent(button.getAttribute("data-plan")) + "/submit", {
      version: Number(button.getAttribute("data-version"))
    }).then(function () {
      toast("计划已提交给医生，等待审核");
      return refresh();
    }).catch(function (error) { toast(error.message, "error"); });
  }

  function approvePlan(button) {
    post("/api/v1/medication/plans/" + encodeURIComponent(button.getAttribute("data-plan")) + "/approve", {
      version: Number(button.getAttribute("data-version")),
      approved_by: readValue("approved-by", "doctor:D001")
    }).then(function () {
      toast("计划已审批生效");
      return refresh();
    }).catch(function (error) { toast(error.message, "error"); });
  }

  function pausePlan(button) {
    if (!window.confirm("暂停这个计划，并取消未来尚未开始的任务？")) return;
    post("/api/v1/medication/plans/" + encodeURIComponent(button.getAttribute("data-plan")) + "/pause", {
      version: Number(button.getAttribute("data-version"))
    }).then(function () {
      toast("计划已暂停");
      return refresh();
    }).catch(function (error) { toast(error.message, "error"); });
  }

  function handleEscalationAction(action, escalationId) {
    var actorRole = state.role === "doctor" ? "manual_reviewer" : state.role;
    var actorId = state.role === "family" ? "family:F001" : state.role === "doctor" ? "doctor:D001" : "caregiver-001";
    if (action === "acknowledge") {
      post("/api/v1/medication/escalations/" + encodeURIComponent(escalationId) + "/acknowledge", {
        actor_id: actorId,
        actor_role: actorRole,
        event_id: "web-ack-" + Date.now()
      }).then(function () {
        toast("已确认接手，自动升级已暂停");
        return refresh();
      }).catch(function (error) { toast(error.message, "error"); });
      return;
    }
    var code = window.prompt("处置结果（TAKEN_VERIFIED / NOT_TAKEN / REFUSED / NOT_FOUND / DEVICE_ERROR / OTHER）", "NOT_TAKEN");
    if (!code) return;
    var note = window.prompt("处置备注（可选）", "") || "";
    post("/api/v1/medication/escalations/" + encodeURIComponent(escalationId) + "/resolve", {
      actor_id: actorId,
      actor_role: actorRole,
      resolution_code: code.trim().toUpperCase(),
      resolution_note: note,
      event_id: "web-resolve-" + Date.now()
    }).then(function () {
      toast("异常事件已关闭");
      return refresh();
    }).catch(function (error) { toast(error.message, "error"); });
  }

  function renderRoleMarkup() {
    var metrics = '<section class="metric-grid" id="metric-grid">' +
      '<div class="metric-card metric-blue"><div class="metric-top"><span class="metric-label">今日任务</span><span class="metric-icon">◷</span></div><div class="metric-value" id="metric-total">—</div><div class="metric-foot">已生成的服药任务</div></div>' +
      '<div class="metric-card metric-green"><div class="metric-top"><span class="metric-label">已确认</span><span class="metric-icon">✓</span></div><div class="metric-value" id="metric-taken">—</div><div class="metric-foot">老人已反馈“吃了”</div></div>' +
      '<div class="metric-card metric-amber"><div class="metric-top"><span class="metric-label">待处理</span><span class="metric-icon">!</span></div><div class="metric-value" id="metric-pending">—</div><div class="metric-foot">等待提醒或老人回应</div></div>' +
      '<div class="metric-card metric-violet"><div class="metric-top"><span class="metric-label">依从率</span><span class="metric-icon">↗</span></div><div class="metric-value" id="metric-rate">—</div><div class="metric-foot">基于今日已结束任务</div></div>' +
      '</section>';

    var reminder = '<section class="reminder-center d-none" id="reminder-center" aria-live="assertive" aria-atomic="false">' +
      '<div class="reminder-center-header"><div><span class="reminder-live-dot"></span><span class="section-kicker">LIVE REMINDER</span><h3>现在需要确认用药</h3></div>' +
      '<button class="btn btn-sm btn-reminder-outline" data-action="enable-notifications">开启系统通知</button></div>' +
      '<div id="reminder-list" class="reminder-list"></div></section>';

    var workflow = '<section class="card card-elevated workflow-card" aria-label="三方协同流程"><div class="workflow-header"><div><div class="section-kicker">SHARED CARE FLOW</div><h3 class="card-title mt-1 mb-0">从计划到一次安心确认</h3></div><div class="workflow-summary" id="workflow-summary">先由家属建立一份清晰的用药计划。</div></div>' +
      '<div class="workflow-track" id="workflow-steps"><div class="workflow-step is-current" data-step="1"><div class="workflow-index">1</div><div><div class="workflow-title">建立计划</div><div class="workflow-detail">药名、剂量、每天几点</div></div></div>' +
      '<div class="workflow-step" data-step="2"><div class="workflow-index">2</div><div><div class="workflow-title">医生审核</div><div class="workflow-detail">核对时间与服用关系</div></div></div>' +
      '<div class="workflow-step" data-step="3"><div class="workflow-index">3</div><div><div class="workflow-title">自动提醒</div><div class="workflow-detail">到点生成可信交互</div></div></div>' +
      '<div class="workflow-step" data-step="4"><div class="workflow-index">4</div><div><div class="workflow-title">老人确认</div><div class="workflow-detail">吃了 / 晚点 / 跳过</div></div></div></div></section>';

    var escalationPanel = '<section class="card card-elevated escalation-panel" id="escalation-panel"><div class="card-header border-0 pb-0 d-flex justify-content-between align-items-start"><div><div class="section-kicker">M6 · ESCALATION</div><h3 class="card-title mt-1">异常升级</h3><p class="card-caption">未确认事件会按确定性规则通知护工、家属并进入人工复核。</p></div><span class="soft-badge soft-amber">可审计</span></div><div class="card-body pt-3"><div id="escalation-list" class="escalation-list"></div></div></section>';

    if (state.role === "elder") {
      return '<section class="role-hero elder-hero"><div class="hero-content"><div class="hero-kicker"><span class="pulse-ring"></span>老人端 · 今日陪伴</div><h2>按时吃药，身体会记得这份认真。</h2><p>提醒出现时，点击“吃了”就完成确认；如果还没准备好，也可以选择“晚点”。</p><div class="hero-actions"><button class="btn btn-light" data-action="enable-notifications">开启提醒声音</button><button class="btn btn-ghost-light" data-action="scroll-tasks">看今天的任务</button></div></div><div class="hero-stat"><span class="hero-stat-label" id="hero-stat-label">下一次服用</span><strong id="hero-stat-value">—</strong><span class="hero-stat-note" id="hero-stat-note">正在读取计划</span></div></section>' +
        reminder + metrics + escalationPanel +
        '<section class="split-grid elder-grid"><div class="card card-elevated" id="today-tasks"><div class="card-header border-0"><div><div class="section-kicker">TODAY · <span id="today-label">—</span></div><h3 class="card-title mt-1">今天要吃什么</h3></div></div><div class="card-body pt-2"><div id="task-list" class="task-list"></div></div></div>' +
        '<div class="card card-elevated assistant-card"><div class="assistant-intro"><div class="agent-badge"><span class="status-dot" id="agent-status-dot"></span><span id="agent-status-label">智能助手检查中</span></div><div class="section-kicker mt-3">EASY REPLY</div><h3 class="card-title mt-1">也可以直接告诉我</h3><p>例如说“我吃了”，系统会把回复绑定到当前提醒，并同步给家属和医生。</p></div>' +
        agentForm("我吃了", "例如：我吃了，或每天晚上10点提醒我吃药") + '</div></section>' + workflow +
        '<section class="split-grid elder-bottom"><div class="card card-elevated reassurance-card"><div class="section-kicker">安心提示</div><h3 class="card-title mt-1">每一次点击，都会被认真记录</h3><p>如果你选择“晚点”，系统只会延后提醒，不会改变原本的服药时间。家属和医生可以看到完整记录。</p><div class="reassurance-list"><span>✓ 不需要记住复杂编号</span><span>✓ 只处理当前这一剂药</span><span>✓ 任何状态变化都有记录</span></div></div>' +
        '<div class="card card-elevated"><div class="card-header border-0 pb-0"><div><div class="section-kicker">RECENT CARE LOG</div><h3 class="card-title mt-1">最近同步</h3></div><span class="soft-badge soft-green">共享</span></div><div class="card-body pt-3"><div id="event-list" class="event-list"></div></div></div></section>';
    }

    if (state.role === "doctor") {
      return '<section class="role-hero doctor-hero"><div class="hero-content"><div class="hero-kicker"><span class="pulse-ring"></span>医生端 · 临床审核</div><h2>让每一份计划，都经得起核对。</h2><p>在审批前确认药品、剂量、时间和服用关系；老人每次反馈后，依从率会实时更新。</p><div class="hero-actions"><button class="btn btn-light" data-action="run-scheduler">▶ 运行一轮调度</button><button class="btn btn-ghost-light" data-action="scroll-plans">查看待审核计划</button></div></div><div class="hero-stat doctor-hero-stat"><span class="hero-stat-label" id="hero-stat-label">待审核计划</span><strong id="hero-stat-value">—</strong><span class="hero-stat-note" id="hero-stat-note">正在读取计划</span></div></section>' +
        reminder + metrics + escalationPanel +
        '<section class="split-grid doctor-main"><div class="card card-elevated" id="plan-lifecycle"><div class="card-header border-0 pb-0 d-flex justify-content-between align-items-start"><div><div class="section-kicker">CLINICAL REVIEW</div><h3 class="card-title mt-1">计划审核队列</h3><p class="card-caption">只审批家属提交的计划，生效后系统才会生成任务。</p></div><span class="soft-badge soft-slate" id="plan-count">0 个版本</span></div><div class="doctor-identity"><div><span class="mini-label">当前审核人</span><strong>医生 D001</strong></div><div class="identity-input"><label for="approved-by">审批记录使用</label><input class="form-control" id="approved-by" value="doctor:D001" /></div></div><div class="table-responsive"><table class="table table-vcenter card-table"><thead><tr><th>药品</th><th>时间</th><th>版本</th><th>状态</th><th class="w-1"></th></tr></thead><tbody id="plan-list"></tbody></table></div></div>' +
        '<div class="card card-elevated" id="today-tasks"><div class="card-header border-0 pb-0 d-flex justify-content-between align-items-start"><div><div class="section-kicker">ADHERENCE · <span id="today-label">—</span></div><h3 class="card-title mt-1">今日执行情况</h3><p class="card-caption">查看提醒是否送达，以及老人是否完成确认。</p></div><button class="btn btn-sm btn-ghost-secondary" data-action="refresh">刷新状态</button></div><div class="card-body pt-3"><div id="task-list" class="task-list"></div></div></div></section>' +
        workflow +
        '<section class="split-grid doctor-bottom"><div class="card card-elevated doctor-note-card"><div class="section-kicker">CLINICAL NOTE</div><h3 class="card-title mt-1">审核提示</h3><p>审批只会激活当前计划版本；后续剂量或时间发生变化时，请通过新版本重新审核，旧版本会保留在审计链中。</p><button class="btn btn-outline-primary" data-action="run-scheduler">运行调度检查</button></div><div class="card card-elevated"><div class="card-header border-0 pb-0 d-flex justify-content-between align-items-start"><div><div class="section-kicker">AUDIT TRAIL</div><h3 class="card-title mt-1">最近事件</h3></div><span class="soft-badge soft-green">Live</span></div><div class="card-body pt-3"><div id="event-list" class="event-list"></div></div></div></section>';
    }

    return '<section class="role-hero family-hero"><div class="hero-content"><div class="hero-kicker"><span class="pulse-ring"></span>家属端 · 照护中枢</div><h2>把复杂的用药安排，变成清楚的下一步。</h2><p>家属创建草稿、提交医生审核；提醒出现后，也可以协助老人记录“吃了、晚点或跳过”。</p><div class="hero-actions"><button class="btn btn-light" data-action="run-scheduler">▶ 运行一轮调度</button><button class="btn btn-ghost-light" data-action="scroll-plan">＋ 新建计划</button></div></div><div class="hero-stat"><span class="hero-stat-label" id="hero-stat-label">待协同计划</span><strong id="hero-stat-value">—</strong><span class="hero-stat-note" id="hero-stat-note">正在读取计划</span></div></section>' +
      metrics + escalationPanel +
      '<section class="split-grid family-builder"><div class="card card-elevated" id="plan-editor"><div class="card-header border-0 pb-0"><div><div class="section-kicker">PLAN BUILDER</div><h3 class="card-title mt-1">创建用药计划</h3><p class="card-caption">先保存草稿，再交给医生确认。</p></div><span class="soft-badge soft-blue">Family draft</span></div><div class="card-body pt-3"><form id="plan-form"><div class="mb-3"><label class="form-label" for="drug-name">药品名称</label><input class="form-control" id="drug-name" required value="氨氯地平" placeholder="例如：氨氯地平" /></div><div class="form-row"><div><label class="form-label" for="dosage-text">剂量</label><input class="form-control" id="dosage-text" required value="5mg" placeholder="例如：5mg" /></div><div><label class="form-label" for="schedule-time">每日时间（24小时制）</label><input class="form-control" id="schedule-time" type="time" required /></div></div><div class="form-row"><div><label class="form-label" for="relation-to-meal">服用关系</label><select class="form-select" id="relation-to-meal"><option value="">不指定</option><option value="餐前">餐前</option><option value="餐后" selected>餐后</option></select></div><div><label class="form-label" for="start-date">开始日期（默认今天）</label><input class="form-control" id="start-date" type="date" required /></div></div><div class="form-row"><div><label class="form-label" for="created-by">录入人</label><input class="form-control" id="created-by" value="family:F001" /></div><div><label class="form-label" for="approved-by">协作医生</label><input class="form-control" id="approved-by" value="doctor:D001" /></div></div><div class="form-hint form-hint-box"><span class="hint-dot"></span>固定每日时间 · Asia/Shanghai · 审批后生成未来 7 天任务</div><button class="btn btn-primary w-100 mt-3" type="submit">创建草稿</button></form></div></div>' +
      '<div class="card card-elevated" id="today-tasks"><div class="card-header border-0 pb-0 d-flex justify-content-between align-items-start"><div><div class="section-kicker">TODAY · <span id="today-label">—</span></div><h3 class="card-title mt-1">老人今天的任务</h3></div><button class="btn btn-sm btn-ghost-secondary" data-action="refresh">刷新状态</button></div><div class="card-body pt-3"><div id="task-list" class="task-list"></div></div></div></section>' +
      '<section class="card card-elevated agent-card"><div class="agent-layout"><div class="agent-intro"><div class="agent-badge"><span class="status-dot" id="agent-status-dot"></span><span id="agent-status-label">Harness 检查中</span></div><div class="section-kicker mt-3">SEMANTIC ASSISTANT</div><h3 class="agent-title">用自然语言快速建计划</h3><p>说出药品、剂量和时间，系统会把中文时段规范成 24 小时制，并生成一份待医生审核的草稿。</p><div class="agent-examples"><span>每天晚上10点提醒吃氨氯地平5mg</span><span>每天早上8点吃二甲双胍</span></div></div>' + agentForm("例如：每天晚上10点提醒我吃氨氯地平5mg", "例如：每天晚上10点提醒我吃氨氯地平5mg") + '</div><div class="agent-result d-none" id="agent-result"></div></section>' +
      '<section class="split-grid family-bottom"><div class="card card-elevated" id="plan-lifecycle"><div class="card-header border-0 pb-0 d-flex justify-content-between align-items-start"><div><div class="section-kicker">PLAN LIFECYCLE</div><h3 class="card-title mt-1">计划版本与协作状态</h3></div><span class="soft-badge soft-slate" id="plan-count">0 个版本</span></div><div class="table-responsive"><table class="table table-vcenter card-table"><thead><tr><th>药品</th><th>时间</th><th>版本</th><th>状态</th><th class="w-1"></th></tr></thead><tbody id="plan-list"></tbody></table></div></div><div class="card card-elevated"><div class="card-header border-0 pb-0 d-flex justify-content-between align-items-start"><div><div class="section-kicker">AUDIT TRAIL</div><h3 class="card-title mt-1">最近事件</h3></div><span class="soft-badge soft-green">Live</span></div><div class="card-body pt-3"><div id="event-list" class="event-list"></div></div></div></section>' + workflow;
  }

  function agentForm(placeholder, example) {
    return '<form id="agent-form" class="agent-form"><label class="form-label" for="agent-text">发送给用药智能体</label><textarea class="form-control" id="agent-text" rows="3" placeholder="' + escapeHtml(placeholder) + '"></textarea><div class="agent-form-footer"><span class="form-hint" id="agent-hint">' + escapeHtml(example || "自然语言只会生成待审核草稿") + '</span><button class="btn btn-primary" type="submit">发送 ↗</button></div></form>';
  }

  function mountRole() {
    var meta = ROLE_META[state.role];
    document.body.setAttribute("data-role", state.role);
    setText("role-overline", meta.overline);
    setText("role-title", meta.title);
    setText("role-lead", meta.lead);
    document.title = meta.label + " · 用药守护台";
    var content = byId("role-content");
    if (content) content.innerHTML = renderRoleMarkup();
    Array.prototype.forEach.call(document.querySelectorAll("[data-role-link]"), function (link) {
      var active = link.getAttribute("data-role-link") === state.role;
      link.classList.toggle("is-active", active);
      link.setAttribute("aria-current", active ? "page" : "false");
    });
    setDefaults();
    renderMetrics();
    renderHeroState();
    renderTasks();
    renderPlans();
    renderEvents();
    renderEscalations();
    renderAgentStatus(null);
    renderWorkflow();
    renderSharedContext();
  }

  document.addEventListener("click", function (event) {
    var escalationButton = event.target.closest("[data-escalation-action]");
    if (escalationButton) {
      handleEscalationAction(
        escalationButton.getAttribute("data-escalation-action"),
        escalationButton.getAttribute("data-escalation")
      );
      return;
    }
    var reminderButton = event.target.closest("[data-reminder-action]");
    if (reminderButton) {
      var reminderAction = reminderButton.getAttribute("data-reminder-action");
      var reminderActionMap = { taken: "CONFIRM_TAKEN", delay: "DELAY", skip: "SKIP" };
      handleReminderAction(reminderActionMap[reminderAction], reminderButton.getAttribute("data-interaction"));
      return;
    }
    var button = event.target.closest("[data-action]");
    if (!button) return;
    var action = button.getAttribute("data-action");
    if (action === "refresh") refresh();
    if (action === "run-scheduler") runScheduler();
    if (action === "enable-notifications") enableNotifications();
    if (action === "scroll-plan") {
      var editor = byId("plan-editor");
      if (editor) editor.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    if (action === "scroll-plans" || action === "scroll-tasks") {
      var target = byId(action === "scroll-plans" ? "plan-lifecycle" : "today-tasks");
      if (target) target.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    if (action === "taken") respond("CONFIRM_TAKEN", button.getAttribute("data-interaction"));
    if (action === "skip") respond("SKIP", button.getAttribute("data-interaction"));
    if (action === "delay") {
      var minutes = window.prompt("延后多少分钟？", "30");
      if (minutes && Number(minutes) > 0) respond("DELAY", button.getAttribute("data-interaction"), Number(minutes));
    }
    if (action === "submit") submitPlan(button);
    if (action === "approve") approvePlan(button);
    if (action === "pause") pausePlan(button);
  });

  document.addEventListener("submit", function (event) {
    if (event.target.id === "plan-form") createPlan(event);
    if (event.target.id === "agent-form") sendAgent(event);
  });

  var elderInput = byId("elder-id");
  if (elderInput) {
    elderInput.addEventListener("change", refresh);
    elderInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") refresh();
    });
  }

  mountRole();
  refresh();
  window.setInterval(pollNotifications, 2000);
  // Keep plan lifecycle, adherence metrics and audit events in sync across the three role pages.
  window.setInterval(function () {
    if (!state.refreshPromise) refresh();
  }, 8000);
  window.__medicationApp = { refresh: refresh, role: state.role };
}());
