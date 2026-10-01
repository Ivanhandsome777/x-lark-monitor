const state = { csrf: "", accounts: [], lastLogCount: 0, clearedAt: 0, route: "feed" };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(state.csrf ? { "X-CSRF-Token": state.csrf } : {}), ...(options.headers || {}) },
  });
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(result.error || "请求失败");
  return result;
}

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast show${isError ? " error-toast" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.className = "toast"; }, 2800);
}

function setRoute(route, push = false) {
  state.route = route === "settings" ? "settings" : "feed";
  $$('[data-page]').forEach((page) => { page.hidden = page.dataset.page !== state.route; });
  $$('.nav-item').forEach((item) => item.classList.toggle("active", item.dataset.route === state.route));
  if (push) history.pushState({}, "", state.route === "settings" ? "/settings" : "/");
  document.title = state.route === "settings" ? "账户与统计 · X Lark Monitor" : "采集内容 · X Lark Monitor";
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (state.route === "feed") refreshDashboard();
}

function renderAccounts() {
  const list = $("#account-list");
  list.replaceChildren(...state.accounts.map((name) => {
    const chip = document.createElement("span");
    chip.className = "account-chip";
    chip.append(document.createTextNode(`@${name}`));
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("aria-label", `移除 @${name}`);
    button.textContent = "×";
    button.addEventListener("click", () => {
      state.accounts = state.accounts.filter((item) => item !== name);
      renderAccounts();
    });
    chip.append(button);
    return chip;
  }));
  $("#account-count").textContent = `${state.accounts.length} 个`;
  const select = $("#feed-account");
  const selected = select.value;
  select.replaceChildren(new Option("全部账号", ""), ...state.accounts.map((name) => new Option(`@${name}`, name)));
  if (state.accounts.includes(selected)) select.value = selected;
}

function addAccount() {
  const input = $("#account-input");
  const name = input.value.trim().replace(/^@/, "").toLowerCase();
  const error = $("#account-error");
  if (!name) return;
  if (!/^[a-z0-9_]{1,15}$/i.test(name)) {
    error.textContent = "账号名只能包含字母、数字和下划线，最多 15 个字符。";
    return;
  }
  error.textContent = "";
  if (!state.accounts.includes(name)) state.accounts.push(name);
  input.value = "";
  renderAccounts();
  input.focus();
}

function applyConfig(config) {
  state.accounts = config.usernames || [];
  renderAccounts();
  $(`input[name="mode"][value="${config.mode}"]`).checked = true;
  $("#poll-interval").value = config.poll_interval;
  $("#include-replies").checked = config.include_replies;
  $("#include-retweets").checked = config.include_retweets;
  $("#push-existing").checked = config.push_existing;
  if (config.has_bearer_token) $("#bearer-token").placeholder = `已保存 ${config.bearer_token_hint}，留空保持不变`;
  if (config.has_lark_webhook) $("#lark-webhook").placeholder = `已保存 ${config.lark_webhook_hint}，留空保持不变`;
  if (config.has_signing_secret) $("#signing-secret").placeholder = `已保存 ${config.signing_secret_hint}，留空保持不变`;
  if (config.has_proxy) $("#proxy-url").placeholder = `已保存 ${config.proxy_hint}，留空保持不变`;
  updateModeUI();
}

function updateModeUI() {
  const mode = $("input[name='mode']:checked").value;
  $("#poll-settings").hidden = mode !== "poll";
  $("#mode-display").textContent = mode === "stream" ? "实时流" : "定时轮询";
}

function configPayload() {
  return {
    bearer_token: $("#bearer-token").value,
    lark_webhook_url: $("#lark-webhook").value,
    lark_signing_secret: $("#signing-secret").value,
    proxy_url: $("#proxy-url").value,
    usernames: state.accounts,
    mode: $("input[name='mode']:checked").value,
    poll_interval: Number($("#poll-interval").value),
    include_replies: $("#include-replies").checked,
    include_retweets: $("#include-retweets").checked,
    push_existing: $("#push-existing").checked,
  };
}

function formatUptime(seconds) {
  if (!seconds) return "—";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours} 小时 ${minutes} 分` : `${Math.max(1, minutes)} 分钟`;
}

function setStatusPill(pill, running) {
  pill.className = `status-pill ${running ? "is-running" : "is-stopped"}`;
  pill.querySelector("b").textContent = running ? "运行中" : "未运行";
}

function renderStatus(status) {
  const running = status.running;
  setStatusPill($("#status-pill"), running);
  setStatusPill($("#status-pill-feed"), running);
  $("#status-text").textContent = running ? "正在监听新帖" : (status.last_exit_code ? `已停止 · 错误 ${status.last_exit_code}` : "等待启动");
  $("#uptime").textContent = formatUptime(status.uptime_seconds);
  $("#stop-monitor").disabled = !running;
  $("#save-start").textContent = running ? "保存并重启" : "保存并启动";
  renderLogs(status.logs || []);
}

function renderLogs(logs) {
  const visible = logs.slice(state.clearedAt);
  const view = $("#log-view");
  if (!visible.length) {
    view.innerHTML = '<p class="empty-log">服务启动后，日志会显示在这里。</p>';
    return;
  }
  const atBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 50;
  view.replaceChildren(...visible.map((log) => {
    const line = document.createElement("div");
    line.className = `log-line ${log.level || "info"}`;
    const clock = document.createElement("time");
    clock.textContent = log.time;
    const text = document.createElement("span");
    text.textContent = log.message;
    line.append(clock, text);
    return line;
  }));
  if (atBottom || visible.length !== state.lastLogCount) view.scrollTop = view.scrollHeight;
  state.lastLogCount = visible.length;
}

function money(value) { return `$${Number(value || 0).toFixed(3)}`; }

function renderMetrics(metrics) {
  $("#feed-today").textContent = `${metrics.today_posts} 条`;
  $("#feed-total").textContent = `${metrics.total_posts} 条`;
  $("#feed-delivered").textContent = `${metrics.delivered_posts} 条`;
  $("#feed-cost").textContent = money(metrics.estimated_total_usd);
  $("#metric-total-cost").textContent = money(metrics.estimated_total_usd);
  $("#metric-month-cost").textContent = money(metrics.estimated_month_usd);
  $("#metric-month-posts").textContent = metrics.month_posts;
  $("#metric-pending").textContent = metrics.pending_posts;
  renderDailyChart(metrics.daily || []);
  renderAccountStats(metrics.accounts || []);
}

function renderDailyChart(days) {
  const chart = $("#daily-chart");
  const max = Math.max(1, ...days.map((day) => day.count));
  chart.replaceChildren(...days.map((day) => {
    const item = document.createElement("div");
    item.className = "day-column";
    const value = document.createElement("strong");
    value.textContent = day.count || "";
    const track = document.createElement("div");
    track.className = "day-track";
    const bar = document.createElement("span");
    bar.style.height = `${Math.max(day.count ? 8 : 2, day.count / max * 100)}%`;
    track.append(bar);
    const label = document.createElement("small");
    label.textContent = day.date.slice(5).replace("-", "/");
    item.append(value, track, label);
    return item;
  }));
}

function renderAccountStats(accounts) {
  const container = $("#account-stats");
  if (!accounts.length) {
    container.innerHTML = '<p class="muted-line">暂无账号采集数据。</p>';
    return;
  }
  container.replaceChildren(...accounts.map((account) => {
    const row = document.createElement("div");
    row.className = "account-stat-row";
    const identity = document.createElement("div");
    identity.innerHTML = `<span class="account-avatar">${account.username[0].toUpperCase()}</span><span><b>@${account.username}</b><small>最近采集 ${formatDate(account.last_collected_at)}</small></span>`;
    const count = document.createElement("strong");
    count.textContent = `${account.count} 条`;
    row.append(identity, count);
    return row;
  }));
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value.endsWith("Z") ? value : `${value}Z`);
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
}

function renderPosts(posts) {
  const feed = $("#content-feed");
  if (!posts.length) {
    feed.innerHTML = '<div class="empty-state"><div class="empty-mark">X</div><h2>还没有采集内容</h2><p>配置监控账号并启动服务后，新推文会出现在这里。</p><a href="/settings" class="button primary route-link">前往账户管理</a></div>';
    bindRouteLinks();
    return;
  }
  feed.replaceChildren(...posts.map((post) => {
    const article = document.createElement("article");
    article.className = "post-card";
    const header = document.createElement("header");
    const identity = document.createElement("div");
    identity.className = "post-identity";
    const avatar = document.createElement("span");
    avatar.className = "account-avatar";
    avatar.textContent = (post.username || "X")[0].toUpperCase();
    const names = document.createElement("span");
    const name = document.createElement("b");
    name.textContent = post.name || post.username;
    const username = document.createElement("small");
    username.textContent = `@${post.username}`;
    names.append(name, username);
    identity.append(avatar, names);
    const badge = document.createElement("span");
    badge.className = `delivery-badge ${post.delivered ? "sent" : "pending"}`;
    badge.textContent = post.delivered ? "已推送" : "待推送";
    header.append(identity, badge);
    const text = document.createElement("p");
    text.className = "post-text";
    text.textContent = post.text || "（无文字内容）";
    const footer = document.createElement("footer");
    const time = document.createElement("time");
    time.textContent = formatDate(post.created_at || post.collected_at);
    const link = document.createElement("a");
    link.href = post.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "在 X 中查看 ↗";
    footer.append(time, link);
    article.append(header, text, footer);
    return article;
  }));
}

async function refreshDashboard() {
  try {
    const username = $("#feed-account").value;
    const result = await api(`/api/dashboard?limit=100&username=${encodeURIComponent(username)}`);
    renderMetrics(result.metrics);
    renderPosts(result.posts);
  } catch (error) { toast(error.message, true); }
}

async function refreshStatus() {
  try {
    const result = await api("/api/status");
    renderStatus(result.status);
  } catch (error) { $("#status-text").textContent = "后台连接中断"; }
}

function bindRouteLinks() {
  $$('.nav-item, .route-link').forEach((link) => {
    if (link.dataset.bound) return;
    link.dataset.bound = "true";
    link.addEventListener("click", (event) => {
      if (event.metaKey || event.ctrlKey) return;
      event.preventDefault();
      setRoute(link.getAttribute("href") === "/settings" ? "settings" : "feed", true);
    });
  });
}

async function initialize() {
  try {
    setRoute(location.pathname === "/settings" ? "settings" : "feed");
    bindRouteLinks();
    const result = await api("/api/config");
    state.csrf = result.csrf_token;
    applyConfig(result.config);
    await Promise.all([refreshStatus(), refreshDashboard()]);
    setInterval(refreshStatus, 2000);
    setInterval(() => { if (state.route === "feed") refreshDashboard(); }, 10000);
  } catch (error) { toast(error.message, true); }
}

window.addEventListener("popstate", () => setRoute(location.pathname === "/settings" ? "settings" : "feed"));
$("#add-account").addEventListener("click", addAccount);
$("#account-input").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); addAccount(); } });
$$('input[name="mode"]').forEach((input) => input.addEventListener("change", updateModeUI));
$$('.reveal').forEach((button) => button.addEventListener("click", () => {
  const input = document.getElementById(button.dataset.target);
  input.type = input.type === "password" ? "text" : "password";
  button.textContent = input.type === "password" ? "显示" : "隐藏";
}));
$("#feed-account").addEventListener("change", refreshDashboard);
$("#refresh-feed").addEventListener("click", async () => { await refreshDashboard(); toast("内容已刷新"); });

$("#config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("#form-message");
  const button = $("#save-start");
  button.disabled = true;
  message.className = "form-message";
  message.textContent = "正在保存配置…";
  try {
    const saved = await api("/api/config", { method: "POST", body: JSON.stringify(configPayload()) });
    applyConfig(saved.config);
    $("#bearer-token").value = "";
    $("#lark-webhook").value = "";
    $("#signing-secret").value = "";
    $("#proxy-url").value = "";
    await api("/api/start", { method: "POST", body: "{}" });
    message.textContent = "配置已保存，监控服务已启动。";
    toast("监控已启动");
    await Promise.all([refreshStatus(), refreshDashboard()]);
  } catch (error) {
    message.className = "form-message error-message";
    message.textContent = error.message;
    toast(error.message, true);
  } finally { button.disabled = false; }
});

$("#stop-monitor").addEventListener("click", async () => {
  try {
    await api("/api/stop", { method: "POST", body: "{}" });
    toast("监控已停止");
    await refreshStatus();
  } catch (error) { toast(error.message, true); }
});

$("#test-lark").addEventListener("click", async () => {
  const button = $("#test-lark");
  button.disabled = true;
  try {
    await api("/api/test-lark", { method: "POST", body: JSON.stringify({ lark_webhook_url: $("#lark-webhook").value, lark_signing_secret: $("#signing-secret").value }) });
    toast("测试消息已发送，请检查 Lark 群");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});

$("#clear-log-view").addEventListener("click", async () => {
  try {
    const result = await api("/api/status");
    state.clearedAt = result.status.logs.length;
    renderLogs(result.status.logs);
  } catch (error) { toast(error.message, true); }
});

initialize();
