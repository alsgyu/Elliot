"use strict";
const $ = id => document.getElementById(id);
const fmt = (value, digits = 2) => value === null || value === undefined ? "—" : Number(value).toLocaleString("ko-KR", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const signed = value => `${Number(value) > 0 ? "+" : ""}${fmt(value)}`;
const pct = value => value === null || value === undefined ? "—" : `${fmt(Number(value) * 100)}%`;
const clock = value => new Date(value * 1000).toLocaleTimeString("en-GB", { hour12: false, timeZone: "Asia/Seoul" });
const reasons = { NONE: "매매 중", USER_PAUSED: "진입 중단", DAILY_LOSS_LIMIT: "일일 손실 한도", TOTAL_LOSS_LIMIT: "누적 손실 한도", EMERGENCY_STOP: "비상 정지", PROTECTION_FAILED: "보호 주문 실패", STALE_MARKET_DATA: "시세 지연", RECONCILIATION_REQUIRED: "주문 대조 필요", UNKNOWN_ORDER_STATE: "주문 상태 불명", EXCHANGE_MISMATCH: "거래소 설정 불일치" };
const purposes = { entry: "그리드 매수", take_profit: "익절", protection_stop: "비상 손절", manual_close: "포지션 정리" };
const states = { NEW: "대기", PARTIALLY_FILLED: "부분 체결", FILLED: "체결", CANCELED: "취소", UNKNOWN: "확인 필요", REJECTED: "거부" };
const openStates = new Set(["NEW", "PARTIALLY_FILLED"]);
let latest = null, dirty = false, activeView = "dashboard", refreshing = false, noticeTimer, busy = false;
let analyticsAt = 0;

function notice(message, error = false) {
  clearTimeout(noticeTimer);
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = false;
  if (!error) noticeTimer = setTimeout(() => $("notice").hidden = true, 6500);
}
function localError(text) {
  const map = {
    "leverage must be between 1 and 5": "레버리지는 1~5배 범위에서 설정해 주세요.",
    "pause and flatten all positions and orders before applying settings": "설정 적용 전에 진입을 중단하고 포지션과 미체결 주문을 정리해 주세요.",
    "loss limit is still exceeded": "현재 손실이 한도를 초과하여 재개할 수 없습니다. 위험 한도를 확인해 주세요.",
    "grid interval does not cover estimated fees, spread, slippage and funding": "그리드 간격이 예상 수수료·스프레드·슬리피지·펀딩보다 작습니다.",
    "entry notional below exchange minimum": "수량을 거래소 단위로 반올림하면 최소 주문 금액에 미달합니다. 주문 금액을 늘려 주세요.",
    "lower_price must be below upper_price": "하한 가격은 상한 가격보다 작아야 합니다.",
    "stop_loss_price must be below lower_price for a long grid": "손절 가격은 0보다 크고 그리드 하한보다 낮아야 합니다.",
    "market is below the stop-loss price": "현재 가격이 손절 가격 이하이므로 시작할 수 없습니다.",
    "fresh market data is required before resuming": "최신 시세를 받은 뒤 다시 시작해 주세요.",
    "daily loss limit must not exceed total loss limit": "일일 손실 한도는 누적 손실 한도보다 클 수 없습니다.",
    "settings changes are available in paper mode only": "화면에서의 설정 변경은 모의거래 모드에서 지원합니다."
  };
  return map[text] || text;
}
async function api(path, body) {
  const options = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": latest?.csrf_token || "" }, body: JSON.stringify(body) };
  const response = await fetch(path, { ...options, signal: AbortSignal.timeout(path === "/api/backtest" ? 120000 : 15000) });
  const result = await response.json();
  if (!response.ok || result.ok === false) throw new Error(localError(result.error || "요청 처리에 실패했습니다."));
  return result;
}
function pnlText(id, value) {
  $(id).textContent = signed(value || 0);
  $(id).classList.toggle("positive", Number(value) > 0);
  $(id).classList.toggle("negative", Number(value) < 0);
}
function rows(id, data, cells, empty) {
  const fragment = document.createDocumentFragment();
  for (const item of data) {
    const row = document.createElement("tr");
    for (const value of cells(item)) {
      const td = document.createElement("td");
      if (typeof value === "object" && value !== null) {
        const span = document.createElement("span");
        span.textContent = value.text; span.className = value.className; td.append(span);
      } else td.textContent = value;
      row.append(td);
    }
    fragment.append(row);
  }
  if (!data.length) {
    const row = document.createElement("tr"), td = document.createElement("td");
    td.colSpan = 8; td.className = "empty-cell"; td.textContent = empty; row.append(td); fragment.append(row);
  }
  $(id).replaceChildren(fragment);
}
const side = value => ({ text: value === "BUY" ? "매수" : "매도", className: `tag ${value === "BUY" ? "buy" : "sell"}` });

function render(data) {
  const grid = data.grid, p = data.position, pnl = data.pnl || {};
  const running = data.flags.can_enter && !data.flags.manual_resume_required;
  $("mode").textContent = data.mode === "paper" ? "모의거래" : "테스트넷";
  $("symbol").textContent = `BTC / ${data.market_rules.quote_asset}`;
  $("quote-unit").textContent = data.market_rules.quote_asset;
  $("price").textContent = fmt(data.market?.price);
  $("source").textContent = data.market?.source === "paper-simulated" ? "합성 시세 · 실시간 시장 가격 아님" : data.market?.source || "시세 대기";
  $("connection").textContent = data.connection.connected ? "엔진 연결됨" : "연결 확인 필요";
  $("connection").classList.toggle("offline", !data.connection.connected);
  $("halt").textContent = reasons[data.flags.halt_reason] || data.flags.halt_reason;
  $("status-dot").classList.toggle("running", running);
  $("updated").textContent = data.connection.last_tick ? `${clock(data.connection.last_tick)} KST` : "";
  $("resume-label").textContent = running ? "매매 중" : "매매 시작";
  document.querySelector('[data-action="resume"]').disabled = running || busy;
  document.querySelector('[data-action="pause"]').disabled = !running || busy;
  $("equity").textContent = fmt(Number(grid.allocated_margin) + Number(pnl.net_pnl || 0));
  $("capital").textContent = `배정 자본 ${fmt(grid.allocated_margin)}`;
  pnlText("netpnl", pnl.net_pnl);
  $("daily-pnl").textContent = `오늘 ${signed(pnl.daily_net_pnl || 0)}`;
  $("position").textContent = `${fmt(p.quantity, 3)} BTC`;
  $("entry-price").textContent = `평균 진입 ${Number(p.quantity) ? fmt(p.average_entry_price) : "—"}`;
  $("leverage").textContent = `${p.leverage}×`;
  $("margin").textContent = fmt(p.margin);
  $("exposure").textContent = `미체결 포함 노출 ${fmt(data.risk.details.total_if_filled || 0)}`;
  pnlText("realized", pnl.realized_pnl); pnlText("unrealized", pnl.unrealized_pnl);
  $("fees").textContent = fmt(pnl.fees_paid || 0, 4);
  $("funding").textContent = fmt(pnl.funding_paid || 0, 4);
  $("daily-limit").textContent = `${fmt(Math.max(0, -Number(pnl.daily_net_pnl || 0)))} / ${fmt(grid.daily_loss_limit)}`;
  $("total-limit").textContent = `${fmt(Math.max(0, -Number(pnl.net_pnl || 0)))} / ${fmt(grid.total_loss_limit)}`;
  $("loss-progress").value = Math.max(0, -Number(pnl.daily_net_pnl || 0)) / Number(grid.daily_loss_limit) * 100;
  $("total-progress").value = Math.max(0, -Number(pnl.net_pnl || 0)) / Number(grid.total_loss_limit) * 100;
  $("range").textContent = `${fmt(grid.lower_price, 0)} ~ ${fmt(grid.upper_price, 0)} · ${grid.grid_count}개 ${grid.grid_spacing === "geometric" ? "등비율" : "등간격"}`;
  const orders = data.orders.filter(order => openStates.has(order.status));
  const protection = orders.find(order => order.purpose === "protection_stop");
  $("stop").textContent = protection ? `보호 손절 ${fmt(protection.stop_price, 0)} · 활성` : `손절 ${fmt(grid.stop_loss_price, 0)} · ${Number(p.quantity) ? "등록 필요" : "포지션 없음"}`;
  $("risk-state").textContent = data.connection.error ? localError(data.connection.error) : data.risk.ok ? "노출 한도 이내 · 여유 증거금 10%" : "노출 한도 초과 · 신규 진입 제한";
  $("risk-state").classList.toggle("negative", !data.risk.ok || !!data.connection.error);
  $("order-count").textContent = orders.length;
  rows("orders", orders, o => [purposes[o.purpose] || o.purpose, side(o.side), fmt(o.price || o.stop_price), fmt(o.quantity, 3), fmt(o.filled_quantity, 3), { text: states[o.status] || o.status, className: "tag" }], "미체결 주문이 없습니다.");
  rows("fills", data.fills, f => [clock(f.timestamp), side(f.side), fmt(f.price), fmt(f.quantity, 3), fmt(f.fee, 4), signed(f.realized_pnl)], "아직 체결된 주문이 없습니다.");
  $("events").replaceChildren(...data.events.slice(0, 30).map(event => {
    const li = document.createElement("li"), t = document.createElement("time"), badge = document.createElement("span"), text = document.createElement("span");
    t.textContent = clock(event.timestamp); badge.textContent = event.level; badge.className = event.level === "ERROR" ? "negative" : "muted";
    text.textContent = event.message + (event.details.error ? `: ${event.details.error}` : ""); li.append(t, badge, text); return li;
  }));
  if (!dirty) fillForm(grid);
  $("footer-engine").textContent = data.engine === "local-paper" ? "Local paper engine · Hummingbot 런타임 미연결" : "Binance testnet REST";
  drawCharts();
}
function fillForm(grid) {
  for (const [name, value] of Object.entries(grid)) {
    const input = $("settings-form").elements.namedItem(name); if (input) input.value = value;
  }
  $("leverage-output").textContent = `${grid.leverage}×`;
  $("settings-state").textContent = "현재 적용 설정";
}
function formData() {
  if (!$("settings-form").reportValidity()) throw new Error("입력값의 범위를 확인해 주세요.");
  return Object.fromEntries(new FormData($("settings-form")));
}
async function preview() {
  const result = await api("/api/settings/preview", formData());
  const p = result.preview;
  $("preview-cap").textContent = fmt(p.effective_notional_cap);
  $("preview-gap").textContent = pct(p.costs.interval_rate);
  $("preview-cost").textContent = pct(p.costs.estimated_friction);
  $("preview-message").textContent = "거래소 주문 단위와 비용 검증 통과";
  rows("preview-orders", p.orders, o => [fmt(o.price), fmt(o.quantity, 3)], "");
  return result;
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try { latest = await api("/api/status"); render(latest); }
  catch (error) { $("connection").textContent = "엔진 연결 끊김"; $("connection").classList.add("offline"); notice("서버에 연결할 수 없습니다. 실행 터미널을 확인해 주세요.", true); }
  finally { refreshing = false; }
  if (activeView === "research" && Date.now() - analyticsAt > 15000) await loadAnalytics();
}
async function loadAnalytics() {
  analyticsAt = Date.now();
  try {
    const a = await api("/api/analytics");
    $("analytics-library").textContent = a.available ? `${a.library} ${a.version}` : "분석 패키지 미설치";
    $("return").textContent = pct(a.total_return); $("drawdown").textContent = pct(a.max_drawdown);
    $("sharpe").textContent = fmt(a.sharpe); $("volatility").textContent = pct(a.volatility);
    $("analytics-note").textContent = a.reason || `${a.note} 현재 ${a.daily_samples}개.`;
  } catch (error) { notice(error.message, true); }
}
function draw(canvas, points, levels = [], color = "#3079bd") {
  const box = canvas.getBoundingClientRect(); if (box.width < 1 || box.height < 1) return;
  const ratio = devicePixelRatio || 1, w = box.width, h = box.height;
  canvas.width = Math.round(w * ratio); canvas.height = Math.round(h * ratio);
  const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
  const left = 4, top = 12, right = 73, bottom = 26, cw = w - right - left, ch = h - top - bottom;
  const values = [...points.map(p => p.value), ...levels].filter(Number.isFinite);
  if (!values.length) { ctx.fillStyle = "#84938a"; ctx.font = "12px sans-serif"; ctx.fillText("시세 수신 대기", 16, h / 2); return; }
  const min = Math.min(...values), max = Math.max(...values), pad = Math.max((max - min) * 0.12, Math.abs(max) * 0.0003, 0.01);
  const low = min - pad, high = max + pad;
  const y = value => top + ch * (1 - (value - low) / (high - low));
  ctx.font = "10px -apple-system, sans-serif"; ctx.textBaseline = "middle";
  for (let i = 0; i < 5; i++) {
    const yy = top + ch * i / 4; ctx.strokeStyle = "#edf1ee"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(left, yy); ctx.lineTo(w - right + 4, yy); ctx.stroke();
    ctx.fillStyle = "#88958d"; ctx.fillText(fmt(high - (high - low) * i / 4, levels.length ? 0 : 2), w - right + 12, yy);
  }
  ctx.strokeStyle = "#b2cebc"; ctx.setLineDash([3, 5]);
  levels.forEach(value => { ctx.beginPath(); ctx.moveTo(left, y(value)); ctx.lineTo(w - right, y(value)); ctx.stroke(); }); ctx.setLineDash([]);
  if (points.length) {
    const x = i => left + cw * (points.length === 1 ? 0.5 : i / (points.length - 1));
    ctx.beginPath(); points.forEach((p, i) => i ? ctx.lineTo(x(i), y(p.value)) : ctx.moveTo(x(i), y(p.value)));
    ctx.strokeStyle = color; ctx.lineWidth = 1.8; ctx.stroke();
    const last = points.at(-1); ctx.beginPath(); ctx.arc(x(points.length - 1), y(last.value), 3, 0, Math.PI * 2); ctx.fillStyle = color; ctx.fill();
    ctx.fillStyle = "#88958d"; ctx.textBaseline = "alphabetic";
    ctx.fillText(clock(points[0].time).slice(0, 5), left, h - 4);
    const endLabel = clock(last.time).slice(0, 5); ctx.fillText(endLabel, w - right - ctx.measureText(endLabel).width, h - 4);
  }
}
function drawCharts() {
  if (!latest) return;
  draw($("price-chart"), latest.history.map(s => ({ value: Number(s.price), time: s.timestamp })), latest.grid_levels.map(Number));
  draw($("equity-chart"), latest.history.map(s => ({ value: Number(s.equity), time: s.timestamp })), [], "#087b5b");
}
async function confirmAction(action) {
  const dialog = $("confirm-dialog");
  $("confirm-title").textContent = action === "emergency-stop" ? "비상 정지할까요?" : "포지션을 정리할까요?";
  dialog.returnValue = "cancel"; dialog.showModal();
  return new Promise(resolve => dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true }));
}
async function control(action) {
  if (busy) return;
  if (["emergency-stop", "cancel-and-flatten"].includes(action) && !await confirmAction(action)) return;
  busy = true; document.querySelectorAll("[data-action]").forEach(b => b.disabled = true);
  try {
    await api(`/api/control/${action}`, {});
    notice({ pause: "진입을 중단하고 미체결 매수 주문을 취소했습니다.", resume: "매매를 시작했습니다.", "cancel-and-flatten": "주문 취소와 잔여 포지션 정리를 확인했습니다.", "emergency-stop": "비상 정지했습니다. 잔여 포지션이 없습니다." }[action]);
  } catch (error) { notice(error.message, true); }
  finally { busy = false; document.querySelectorAll("[data-action]").forEach(b => b.disabled = false); await refresh(); }
}
document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", async () => {
  activeView = button.dataset.view;
  document.querySelectorAll(".view").forEach(view => view.hidden = view.id !== `${activeView}-view`);
  document.querySelectorAll("[data-view]").forEach(tab => { tab.classList.toggle("active", tab === button); tab.setAttribute("aria-selected", String(tab === button)); });
  drawCharts();
  if (activeView === "research") await loadAnalytics();
  if (activeView === "settings" && latest) { try { await preview(); } catch (error) { notice(error.message, true); } }
}));
document.querySelectorAll("[data-table]").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll("[data-table]").forEach(tab => { tab.classList.toggle("selected", tab === button); $(`${tab.dataset.table}-table`).hidden = tab !== button; });
}));
document.querySelectorAll("[data-action]").forEach(button => button.addEventListener("click", () => control(button.dataset.action)));
$("settings-form").addEventListener("input", () => { dirty = true; $("settings-state").textContent = "저장하지 않은 변경 사항"; $("preview-message").textContent = "설정이 변경되었습니다. 다시 검증해 주세요."; $("leverage-output").textContent = `${$("settings-form").elements.leverage.value}×`; });
$("revert").addEventListener("click", () => { dirty = false; fillForm(latest.grid); preview().catch(e => notice(e.message, true)); });
$("preview").addEventListener("click", () => preview().catch(e => notice(e.message, true)));
$("settings-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try { await api("/api/settings", formData()); dirty = false; await refresh(); await preview(); notice("설정을 저장했습니다. 매매 시작 버튼으로 재개할 수 있습니다."); }
  catch (error) { notice(error.message, true); } finally { button.disabled = false; }
});
$("backtest").addEventListener("click", async () => {
  $("backtest").disabled = true; $("scenario-empty").hidden = false; $("scenario-empty").querySelector("span").textContent = "시나리오 분석 중…";
  try {
    const { report } = await api("/api/backtest", {});
    rows("scenarios", Object.entries(report.results), ([name, r]) => [{ range: "횡보", crash: "급락", uptrend: "지속 상승" }[name], signed(r.pnl.net_pnl), pct(r.analytics.max_drawdown), fmt(r.pnl.fees_paid, 4), fmt(r.pnl.funding_paid, 4), r.fill_count, reasons[r.flags.halt_reason]], "");
    $("scenario-results").hidden = false; $("scenario-empty").hidden = true;
    $("assumptions").replaceChildren(...report.assumptions.map(text => { const li = document.createElement("li"); li.textContent = text; return li; }));
  } catch (error) { notice(error.message, true); $("scenario-empty").querySelector("span").textContent = "분석 실패 · 다시 실행해 주세요."; }
  finally { $("backtest").disabled = false; }
});
$("refresh").addEventListener("click", refresh);
$("download").addEventListener("click", () => {
  if (!latest) return;
  const fields = ["timestamp", "side", "price", "quantity", "fee", "realized_pnl"];
  const csv = "\ufeff" + [fields, ...latest.fills.map(fill => fields.map(field => fill[field]))].map(row => row.join(",")).join("\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a"); link.href = url; link.download = "kgridbot-recent-fills.csv"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
new ResizeObserver(drawCharts).observe(document.querySelector("main"));
lucide.createIcons();
refresh();
setInterval(refresh, 2000);
