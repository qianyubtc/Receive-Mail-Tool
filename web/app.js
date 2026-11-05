"use strict";

const $ = (id) => document.getElementById(id);
const BATCH_SIZE = 20;
const RECEIVE_SETTINGS_KEY = "mailtool.receive-settings";
const ACTIVE_STATUS_REFRESH_MS = 500;
const IDLE_STATUS_REFRESH_MS = 2000;
const state = { accounts: [], trashAccounts: [], selection: new Set(), page: 0, trash: false,
  managing: false, csrf: "", active: null, messages: [], currentCode: "", lastCheck: null,
  selectedMessage: null, busy: false, generation: 0, changingBatch: false, copiedAccountId: null,
  batch: { running: false, ids: [], events: [], results: {}, checked: 0, in_flight: 0 },
  feedSignature: "", listSignature: "", pollTimer: null, pendingPurge: [], purging: false };
let toastTimer;

function toast(text) {
  $("toast").textContent = text; $("toast").hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3000);
}
function notice(id, text) { $(id).textContent = text || ""; $(id).hidden = !text; }
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function number(value) { return "#" + String(value).padStart(3, "0"); }
function accountLabel(id) {
  const account = state.accounts.find((item) => item.id === Number(id));
  return account ? number(account.number) : "已移除邮箱";
}
function formattedDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleString("zh-CN", { hour12: false });
}
function relativeDate(value) {
  const seconds = Math.floor((Date.now() - new Date(value).getTime()) / 1000);
  if (!Number.isFinite(seconds)) return "时间未知";
  if (seconds < 0) return formattedDate(value);
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return Math.floor(seconds / 60) + " 分钟前";
  return formattedDate(value);
}
async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Mailtool-Token": state.csrf },
    body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求失败，请重试");
  return result;
}
async function copy(text, label, accountId = null) {
  if (!text) return;
  try { await navigator.clipboard.writeText(text); }
  catch { toast("复制失败，请手动选中文本复制"); return; }
  if (accountId !== null) {
    state.copiedAccountId = accountId;
    renderAccounts();
  }
  toast(label + "已复制");
}

function filteredAccounts() {
  const query = $("search").value.trim().toLowerCase().replace(/^#/, "");
  const filter = $("account-filter").value;
  return (state.trash ? state.trashAccounts : state.accounts).filter((account) =>
    (!query || account.email.toLowerCase().includes(query) || (/^\d+$/.test(query) && account.number === Number(query))) &&
    (state.trash || filter === "all" || (filter === "used" ? account.used : !account.used)));
}
function pageAccounts() {
  const accounts = filteredAccounts();
  state.page = Math.max(0, Math.min(state.page, Math.ceil(accounts.length / BATCH_SIZE) - 1));
  return accounts.slice(state.page * BATCH_SIZE, (state.page + 1) * BATCH_SIZE);
}
function renderAccounts(force = false) {
  const all = filteredAccounts(), page = pageAccounts();
  const signature = JSON.stringify([page.map((item) => [item.id, item.number, item.email, item.used, item.error]),
    [...state.selection], state.active, state.trash, state.managing, state.batch.running, state.batch.ids, state.copiedAccountId]);
  $("account-count").textContent = state.accounts.length;
  $("page-label").textContent = all.length ? (state.page * BATCH_SIZE + 1) + "–" + (state.page * BATCH_SIZE + page.length) + " / " + all.length : "0 / 0";
  $("page-prev").disabled = !state.page;
  $("page-next").disabled = (state.page + 1) * BATCH_SIZE >= all.length;
  $("select-page").checked = !!page.length && page.every((item) => state.selection.has(item.id));
  $("select-page").indeterminate = page.some((item) => state.selection.has(item.id)) && !$("select-page").checked;
  $("select-page").disabled = !page.length;
  $("manage-tools").hidden = !state.managing;
  $("manage").textContent = state.managing ? "完成管理" : "管理邮箱";
  $("delete-selected").textContent = (state.trash ? "恢复选中" : "删除选中") + " (" + state.selection.size + ")";
  $("delete-selected").disabled = !state.selection.size;
  $("purge-selected").hidden = !state.trash;
  $("purge-selected").disabled = !state.selection.size;
  $("purge-selected").textContent = "彻底删除选中 (" + state.selection.size + ")";
  $("purge-all").hidden = !state.trash;
  $("purge-all").disabled = !state.trashAccounts.length;
  $("trash-toggle").firstChild.textContent = state.trash ? "返回邮箱库 " : "回收站 ";
  if (!force && signature === state.listSignature) return;
  state.listSignature = signature;
  const list = $("accounts"); list.replaceChildren();
  if (!page.length) {
    const empty = element("div", "sidebar-empty", state.trash ? "回收站是空的" : state.accounts.length ? "没有匹配的邮箱" : `一次导入邮箱，每批选 ${BATCH_SIZE} 个收码。`);
    if (!state.accounts.length && !state.trash) {
      const button = element("button", "", "＋ 导入邮箱"); button.addEventListener("click", openImport); empty.append(button);
    }
    list.append(empty);
  }
  for (const account of page) {
    const copied = state.copiedAccountId === account.id;
    const row = element("div", "account-item" + (state.active === account.id ? " active" : "") + (copied ? " copied" : ""));
    const checkbox = element("input"); checkbox.type = "checkbox"; checkbox.checked = state.selection.has(account.id);
    checkbox.setAttribute("aria-label", "选择 " + number(account.number) + " " + account.email);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked && state.selection.size >= BATCH_SIZE) {
        checkbox.checked = false;
        toast(`每批最多选择 ${BATCH_SIZE} 个邮箱`);
        return;
      }
      if (checkbox.checked) state.selection.add(account.id); else state.selection.delete(account.id);
      renderAccounts(); updateControls();
    });
    const button = element("button", "account-main"); button.title = number(account.number) + " " + account.email;
    button.append(element("span", "account-number", number(account.number)));
    const info = element("span", "account-info"); info.append(element("span", "account-address", account.email));
    const listening = state.batch.running && state.batch.ids.includes(account.id);
    const status = state.trash ? "已移入回收站" : account.error ? "收信失败" : listening ? "监听中" : account.used ? "已使用" : "未使用";
    info.append(element("span", "account-status", status)); button.append(info);
    button.addEventListener("click", () => { if (!state.trash) selectAccount(account.id); });
    const copyButton = element("button", "row-copy", copied ? "已复制" : "复制");
    copyButton.title = "复制邮箱"; copyButton.setAttribute("aria-label", "复制邮箱 " + number(account.number) + " " + account.email);
    copyButton.addEventListener("click", () => copy(account.email, number(account.number) + " 邮箱", account.id));
    row.append(checkbox, button, copyButton);
    if (state.managing) {
      const action = element("button", "row-delete", state.trash ? "↶" : "×");
      action.setAttribute("aria-label", (state.trash ? "恢复 " : "删除 ") + number(account.number) + " " + account.email);
      action.addEventListener("click", () => accountAction([account.id], state.trash ? "restore" : "delete")); row.append(action);
      if (state.trash) {
        const purge = element("button", "row-delete", "×");
        purge.title = "彻底删除";
        purge.setAttribute("aria-label", "彻底删除 " + number(account.number) + " " + account.email);
        purge.addEventListener("click", () => requestPurge([account.id])); row.append(purge);
      }
    }
    list.append(row);
  }
}

function updateControls() {
  const sameSelection = state.selection.size === state.batch.ids.length && state.batch.ids.every((id) => state.selection.has(id));
  $("selected-count").textContent = `已选 ${state.selection.size} / ${BATCH_SIZE}`;
  $("batch-start").textContent = state.batch.running && !sameSelection ? "监听选中" : state.batch.running ? "重启本批" : "开始监听";
  $("batch-start").disabled = state.trash || !state.selection.size || state.changingBatch;
  $("batch-stop").disabled = !state.batch.running || state.changingBatch;
  $("batch-next").disabled = state.trash || state.changingBatch || (!state.batch.running && !state.selection.size);
  $("copy-email").disabled = !state.active;
  $("next-account").disabled = !state.active || !state.accounts.some((item) => item.id > state.active);
  $("refresh").disabled = !state.active || state.busy;
  $("refresh-icon").className = state.busy ? "spin" : "";
  $("state-dot").className = "state-dot" + (state.batch.running ? " on" : "");
  const errors = Object.entries(state.batch.results || {}).filter(([, result]) => result.error || (result.warnings || []).length);
  $("monitor-label").textContent = state.batch.running ? "监听中 · " + state.batch.ids.length + " 个" : "未监听";
  $("batch-progress").textContent = state.batch.ids.length ? "已检查 " + state.batch.checked + "/" + state.batch.ids.length + " · 查询中 " + state.batch.in_flight + " · 异常 " + errors.length : "";
  notice("warning", errors.length ? errors.length + " 个邮箱存在读取异常，其余邮箱继续：" + errors.slice(0, 2).map(([id, result]) => accountLabel(id) + " " + (result.error || result.warnings.join("；"))).join("；") : "");
}

function renderFeed() {
  const includeRecent = $("include-recent").checked;
  const event = (state.batch.events || [])
    .filter((item) => item.is_new || includeRecent)
    .sort((a, b) => Date.parse(b.received_at) - Date.parse(a.received_at) || b.id - a.id)[0];
  const signature = JSON.stringify([event, state.batch.running, state.batch.ids]);
  if (signature === state.feedSignature) return;
  state.feedSignature = signature;
  const feed = $("code-feed"); feed.replaceChildren();
  if (!event) {
    feed.append(element("div", "feed-empty", state.batch.running ? "等待新验证码…" : "暂无验证码"));
    return;
  }
  const card = element("article", "incoming-code");
  const top = element("div", "incoming-top");
  top.append(element("span", "incoming-number", number(event.number)), element("span", "incoming-email", event.email));
  const copyEmail = element("button", "text-button", "复制邮箱");
  copyEmail.addEventListener("click", () => copy(event.email, number(event.number) + " 邮箱", event.account_id)); top.append(copyEmail);
  const center = element("div", "incoming-center"); center.append(element("strong", "incoming-value", event.code));
  const copyCode = element("button", "button copy-button", "复制验证码");
  copyCode.setAttribute("aria-label", "复制 " + number(event.number) + " 验证码 " + event.code);
  copyCode.addEventListener("click", () => copy(event.code, number(event.number) + " 验证码")); center.append(copyCode);
  const meta = element("div", "incoming-meta" + (!event.is_new || event.confidence === "possible" ? " caution" : ""));
  meta.append(element("span", "badge" + (event.is_new ? " fresh" : " old"), event.is_new ? event.confidence === "possible" ? "新邮件 · 疑似" : "新验证码" : "监听前的邮件"));
  meta.append(element("span", "", event.folder + " · " + formattedDate(event.received_at)));
  const subject = element("button", "incoming-subject", "邮件详情");
  subject.title = event.subject; subject.addEventListener("click", () => selectAccount(event.account_id));
  card.append(center, top, meta, subject); feed.append(card);
}

async function loadAccounts() {
  const response = await api("/api/accounts"); state.accounts = response.accounts; state.csrf = response.token;
  $("trash-count").textContent = response.trash_count;
  if (state.trash) state.trashAccounts = (await api("/api/accounts?trash=1")).accounts;
  const valid = new Set((state.trash ? state.trashAccounts : state.accounts).map((item) => item.id));
  state.selection = new Set([...state.selection].filter((id) => valid.has(id)));
  if (state.active && !state.accounts.some((item) => item.id === state.active)) clearActive();
  else if (state.active) updateActiveTitle();
  renderAccounts(true); updateControls();
}
async function accountAction(ids, action, confirmed = false) {
  if (!ids.length) return false;
  try {
    await api("/api/accounts/action", { ids, action, confirmed });
    ids.forEach((id) => state.selection.delete(id));
    await loadAccounts(); await pollMonitor(false);
    toast(action === "delete" ? "已移入回收站，邮箱序号已自动补齐" : action === "purge" ? "已彻底删除选中的本地邮箱资料" : "邮箱已恢复，序号已更新");
    return true;
  } catch (error) { notice(action === "purge" ? "purge-error" : "error", error.message); return false; }
}
function requestPurge(ids) {
  const selected = state.trashAccounts.filter((account) => ids.includes(account.id));
  if (!selected.length || state.purging) return;
  state.pendingPurge = selected.map((account) => account.id);
  $("purge-description").textContent = "即将彻底删除回收站中的 " + selected.length + " 个邮箱：";
  $("purge-list").replaceChildren(...selected.slice(0, 5).map((account) => element("div", "", number(account.number) + " " + account.email)));
  if (selected.length > 5) $("purge-list").append(element("div", "", "以及另外 " + (selected.length - 5) + " 个邮箱"));
  $("purge-confirm").textContent = "确认彻底删除 " + selected.length + " 个";
  notice("purge-error", ""); $("purge-dialog").showModal(); $("purge-cancel").focus();
}

function applyReceiveSettings(settings) {
  for (const key of ["protocol", "interval"]) {
    const select = $(key);
    const value = String(settings?.[key] ?? "");
    if (Array.from(select.options).some((option) => option.value === value)) select.value = value;
  }
}

async function startBatch() {
  if (!state.selection.size || state.trash || state.changingBatch) return;
  state.changingBatch = true; updateControls();
  try {
    state.batch = await api("/api/monitor/start", { ids: [...state.selection].sort((a, b) => a - b), protocol: $("protocol").value, interval: Number($("interval").value) });
    const { protocol, interval } = state.batch;
    try {
      localStorage.setItem(RECEIVE_SETTINGS_KEY, JSON.stringify({ protocol, interval }));
    } catch { /* Listening also works when browser storage is disabled. */ }
    notice("error", ""); renderFeed(); renderAccounts();
    scheduleMonitorPoll();
  } catch (error) { notice("error", error.message); }
  finally { state.changingBatch = false; updateControls(); }
}
async function stopBatch() {
  try { state.batch = await api("/api/monitor/stop", {}); renderAccounts(); renderFeed(); updateControls(); }
  catch (error) { notice("error", error.message); }
}
async function nextBatch() {
  if (state.changingBatch) return;
  const ids = state.batch.running ? state.batch.ids : [...state.selection];
  if (!ids.length) return;
  const keepRunning = state.batch.running;
  let advanced = false;
  state.changingBatch = true; updateControls();
  try {
    state.batch = await api("/api/monitor/stop", {});
    await api("/api/accounts/action", { ids, action: "used" });
    state.trash = false; $("search").value = ""; $("account-filter").value = "unused"; state.page = 0;
    await loadAccounts(); state.selection = new Set(pageAccounts().map((item) => item.id));
    state.batch = { ...state.batch, ids: [], events: [], results: {}, checked: 0, in_flight: 0 };
    renderAccounts(); $("accounts").scrollTop = 0; renderFeed(); advanced = true;
    toast(state.selection.size ? "本批已标记使用，已选择下一批 " + state.selection.size + " 个" : "全部邮箱已完成，本批已标记使用");
  } catch (error) { notice("error", error.message); }
  finally { state.changingBatch = false; updateControls(); }
  if (advanced && keepRunning && state.selection.size) await startBatch();
}

function scheduleMonitorPoll() {
  clearTimeout(state.pollTimer);
  const delay = state.batch.running && !document.hidden ? ACTIVE_STATUS_REFRESH_MS : IDLE_STATUS_REFRESH_MS;
  state.pollTimer = setTimeout(pollMonitor, delay);
}

async function pollMonitor(reschedule = true) {
  if (reschedule) clearTimeout(state.pollTimer);
  try {
    const snapshot = await api("/api/monitor");
    if ((snapshot.generation || 0) < (state.batch.generation || 0)) return;
    state.batch = snapshot;
    if ($("error").textContent.startsWith("无法连接本地服务")) notice("error", "");
    for (const account of state.accounts) {
      const result = state.batch.results[account.id];
      if (result && state.batch.ids.includes(account.id)) { account.error = result.error; account.checked_at = result.checked_at; }
    }
    renderFeed(); updateControls(); renderAccounts();
    const activeResult = state.batch.results[state.active];
    if (state.active && activeResult && !activeResult.error && activeResult.checked_at !== state.lastCheck) await loadCached(state.active);
  } catch (error) { notice("error", "无法连接本地服务：" + error.message); }
  finally { if (reschedule) scheduleMonitorPoll(); }
}

function clearActive() {
  state.generation += 1; state.active = null; state.messages = []; state.currentCode = ""; state.lastCheck = null;
  $("active-email").textContent = "尚未选择邮箱"; $("detail-name").textContent = "";
  $("mail-detail").open = false; $("mail-detail").hidden = true;
  renderMessages(); renderCode();
}
async function selectAccount(id) {
  const account = state.accounts.find((item) => item.id === id); if (!account) return;
  state.generation += 1; state.active = id; state.messages = []; state.lastCheck = null;
  const detail = $("mail-detail");
  updateActiveTitle(); detail.hidden = false; detail.open = true;
  detail.scrollIntoView({ block: "nearest" });
  renderAccounts(); renderMessages(); renderCode(); updateControls(); await loadCached(id);
}
function updateActiveTitle() {
  const account = state.accounts.find((item) => item.id === state.active);
  if (!account) return;
  $("active-email").textContent = number(account.number) + " " + account.email;
  $("detail-name").textContent = number(account.number) + " " + account.email;
}
async function loadCached(id) {
  const generation = state.generation;
  try {
    const result = await api("/api/accounts/" + id + "/messages");
    if (generation !== state.generation || state.active !== id) return;
    state.messages = result.messages; state.lastCheck = result.checked_at;
    renderMessages(state.selectedMessage); renderCode();
  } catch (error) { if (state.active === id) notice("error", error.message); }
}
async function fetchMessages() {
  if (!state.active || state.busy) return;
  state.busy = true; updateControls(); const id = state.active, generation = state.generation;
  try {
    const result = await api("/api/fetch", { account_id: id, protocol: $("protocol").value });
    if (state.active !== id || generation !== state.generation) return;
    state.messages = result.messages; state.lastCheck = result.checked_at;
    notice("error", ""); renderMessages(); renderCode();
  } catch (error) { if (state.active === id) notice("error", error.message); }
  finally { state.busy = false; updateControls(); }
}
function renderMessages(preferredId) {
  $("message-count").textContent = state.messages.length;
  const list = $("messages"); list.replaceChildren();
  if (!state.messages.length) {
    const empty = element("div", "empty-state"); empty.append(element("div", "empty-envelope", "✉"), element("h3", "", "暂无已查收的邮件"), element("p", "", "监听这批邮箱后，结果会自动更新。")); list.append(empty);
    $("preview-empty").hidden = false; $("preview-content").hidden = true; return;
  }
  state.selectedMessage = state.messages.some((item) => item.id === preferredId) ? preferredId : state.messages[0].id;
  for (const message of state.messages) {
    const button = element("button", "message-row"); button.dataset.messageId = message.id;
    const top = element("div", "message-row-top"); top.append(element("span", "message-subject", message.subject), element("span", "message-date", relativeDate(message.received_at)));
    const tags = element("div", "message-tags"); tags.append(element("span", "folder-tag", message.folder));
    if (message.code) tags.append(element("span", "mini-code", message.code));
    button.append(top, element("div", "message-sender", message.sender), tags); button.addEventListener("click", () => showMessage(message.id)); list.append(button);
  }
  showMessage(state.selectedMessage);
}
function showMessage(id) {
  const message = state.messages.find((item) => item.id === id); if (!message) return;
  state.selectedMessage = id;
  for (const button of $("messages").querySelectorAll(".message-row")) button.classList.toggle("active", button.dataset.messageId === id);
  $("preview-empty").hidden = true; $("preview-content").hidden = false;
  $("preview-subject").textContent = message.subject;
  $("preview-meta").textContent = message.sender + " · " + formattedDate(message.received_at) + " · " + message.folder;
  $("preview-body").textContent = message.body || "（邮件没有可显示的正文）";
}
function renderCode() {
  const message = state.messages.find((item) => item.code && item.code_confidence === "high") || state.messages.find((item) => item.code);
  state.currentCode = message ? message.code : ""; $("copy-code").disabled = !message;
  $("code").classList.toggle("empty", !message); $("code").textContent = message ? message.code : "— — — — — —";
  $("code-heading").textContent = message && message.code_confidence === "possible" ? "疑似验证码" : "最新验证码";
  const age = message ? (Date.now() - new Date(message.received_at).getTime()) / 60000 : Infinity;
  const old = !Number.isFinite(age) || age > 10 || age < -1;
  $("code-badge").className = "badge" + (message ? old ? " old" : " fresh" : "");
  $("code-badge").textContent = message ? old ? "历史邮件 · 请核对时间" : "最近 10 分钟 · 请核对正文" : "尚无验证码";
  $("code-source").textContent = message ? message.subject + " · " + message.sender : "单个邮箱详情显示最近查收的邮件；新验证码也会出现在上方本批列表。";
  $("code-time").textContent = message ? message.folder + " · " + formattedDate(message.received_at) : "收件箱与垃圾箱一起查收";
  $("checked-time").textContent = state.lastCheck ? "上次查收 " + formattedDate(state.lastCheck) : "尚未查收";
}

function openImport() { notice("import-error", ""); $("import-dialog").showModal(); $("import-text").focus(); }
function choosePage(change) {
  state.page += change; state.selection = new Set(pageAccounts().map((item) => item.id));
  renderAccounts(); $("accounts").scrollTop = 0; updateControls();
}
$("manage").addEventListener("click", async () => {
  state.managing = !state.managing;
  if (!state.managing && state.trash) {
    state.trash = false; state.page = 0; await loadAccounts();
    state.selection = new Set(state.batch.running ? state.batch.ids : pageAccounts().map((item) => item.id));
  }
  renderAccounts(); updateControls();
});
$("trash-toggle").addEventListener("click", async () => {
  state.trash = !state.trash; state.page = 0; state.selection.clear(); $("search").value = "";
  try { await loadAccounts(); } catch (error) { notice("error", error.message); }
});
$("delete-selected").addEventListener("click", () => accountAction([...state.selection], state.trash ? "restore" : "delete"));
$("purge-selected").addEventListener("click", () => requestPurge([...state.selection]));
$("purge-all").addEventListener("click", () => requestPurge(state.trashAccounts.map((account) => account.id)));
for (const id of ["purge-cancel", "purge-close"]) $(id).addEventListener("click", () => { if (!state.purging) $("purge-dialog").close(); });
$("purge-dialog").addEventListener("cancel", (event) => { if (state.purging) event.preventDefault(); });
$("purge-dialog").addEventListener("close", () => { state.pendingPurge = []; });
$("purge-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (state.purging || !state.pendingPurge.length) return;
  state.purging = true;
  for (const id of ["purge-confirm", "purge-cancel", "purge-close"]) $(id).disabled = true;
  try { if (await accountAction([...state.pendingPurge], "purge", true)) $("purge-dialog").close(); }
  finally { state.purging = false; for (const id of ["purge-confirm", "purge-cancel", "purge-close"]) $(id).disabled = false; }
});
$("select-page").addEventListener("change", () => {
  state.selection = $("select-page").checked ? new Set(pageAccounts().map((item) => item.id)) : new Set(); renderAccounts(); updateControls();
});
$("page-prev").addEventListener("click", () => choosePage(-1)); $("page-next").addEventListener("click", () => choosePage(1));
for (const id of ["search", "account-filter"]) $(id).addEventListener(id === "search" ? "input" : "change", () => { state.page = 0; renderAccounts(); updateControls(); });
$("batch-start").addEventListener("click", startBatch); $("batch-stop").addEventListener("click", stopBatch); $("batch-next").addEventListener("click", nextBatch);
$("include-recent").addEventListener("change", renderFeed);
$("copy-email").addEventListener("click", () => {
  const account = state.accounts.find((item) => item.id === state.active);
  if (account) copy(account.email, "邮箱", account.id);
});
$("copy-code").addEventListener("click", () => copy(state.currentCode, "验证码")); $("refresh").addEventListener("click", fetchMessages);
$("next-account").addEventListener("click", () => { const next = state.accounts.find((item) => item.id > state.active); if (next) selectAccount(next.id); });
$("import-open").addEventListener("click", openImport); $("import-close").addEventListener("click", () => $("import-dialog").close());
$("choose-file").addEventListener("click", () => $("import-file").click());
$("import-file").addEventListener("change", async () => {
  const file = $("import-file").files[0]; if (!file) return;
  if (file.size > 16 * 1024 * 1024) { notice("import-error", "文件超过 16 MB，请分批导入"); return; }
  $("import-text").value = await file.text(); $("import-file").value = "";
});
$("import-form").addEventListener("submit", async (event) => {
  event.preventDefault(); $("import-submit").disabled = true; notice("import-error", "");
  try {
    const result = await api("/api/import", { text: $("import-text").value });
    state.trash = false; state.page = 0; $("search").value = ""; $("account-filter").value = "all";
    await loadAccounts(); if (!state.batch.running) state.selection = new Set(pageAccounts().map((item) => item.id));
    $("import-dialog").close(); $("import-text").value = ""; renderAccounts(); updateControls();
    toast("新增 " + result.added + " 个，更新 " + result.updated + " 个；点击监听即可开始");
  } catch (error) { notice("import-error", error.message); }
  finally { $("import-submit").disabled = false; }
});
async function init() {
  try {
    applyReceiveSettings(JSON.parse(localStorage.getItem(RECEIVE_SETTINGS_KEY)));
  } catch { /* Use the page defaults if browser storage is unavailable. */ }
  try {
    await loadAccounts(); await pollMonitor();
    state.selection = new Set(state.batch.ids.length ? state.batch.ids : pageAccounts().map((item) => item.id));
    if (state.batch.running) applyReceiveSettings(state.batch);
    renderAccounts(); updateControls();
  } catch (error) { notice("error", "无法连接本地服务：" + error.message); }
}
init();
