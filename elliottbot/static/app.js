"use strict";
const $ = id => document.getElementById(id);
const fmt = (v, n = 2) => v == null || !Number.isFinite(Number(v)) ? "—" : Number(v).toLocaleString(window.elliottLocaleTag(), { minimumFractionDigits: n, maximumFractionDigits: n });
const stamp = ms => ms ? new Date(ms).toLocaleString(window.elliottLocaleTag(), { timeZone: "Asia/Seoul", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "—";
const names = { WAVE_3: "Impulse Wave 3 entry candidate", WAVE_5: "Impulse Wave 5 entry candidate", WAVE_C: "Zigzag Wave C entry candidate", IMPULSE_COMPLETE: "Five-wave impulse complete", ZIGZAG_COMPLETE: "Zigzag ABC complete", DIAGONAL_WATCH: "Diagonal alternative", TRIANGLE_WATCH: "Triangle alternative", FLAT_OR_COMPLEX_WATCH: "Flat or complex correction alternative" };
const tfNames = { "1w": "1W", "1d": "1D", "4h": "4H", "1h": "1H", "30m": "30m", "15m": "15m" };
const states = { SELECTED: "Primary count", CONFLICT: "Conflicting alternatives", NO_COUNT: "No confirmed count", COMPLEX_CORRECTION: "Complex correction · wait" };
let latest, activeTf = "4h", activeView = "analysis", dirty = false, modeDirty = false, busy = false, refreshing = false, report, polling = false;

function notice(text, error = false) { $("notice").textContent = text; $("notice").classList.toggle("error", error); $("notice").hidden = false; }
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {signal:AbortSignal.timeout(30000)} : { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": latest?.csrf || "" }, body: JSON.stringify(body), signal: AbortSignal.timeout(120000) });
  const data = await r.json();
  if (!r.ok || data.error) throw Error(data.error || "요청 처리 실패");
  return data;
}
function color(el, v) { el.classList.toggle("positive", Number(v) > 0); el.classList.toggle("negative", Number(v) < 0); }
function textCell(row, value, className = "") { const td = document.createElement("td"); td.textContent = value; td.className = className; row.append(td); return td; }
function empty(id, text, n = 7) { const tr = document.createElement("tr"), td = textCell(tr, text, "empty"); td.colSpan = n; $(id).replaceChildren(tr); }
function detail(data) { $("detail-json").textContent = JSON.stringify(data, null, 2); $("details").showModal(); }
function detailButton(data) { const b = document.createElement("button"); b.className = "icon"; b.title = "근거 상세"; b.setAttribute("aria-label", "근거 상세"); const i = document.createElement("i"); i.dataset.lucide = "file-search"; b.append(i); b.addEventListener("click", () => detail(data)); return b; }

function render() {
  if (!latest) return;
  const a = latest.account, d = latest.decision, p = a.position;
  $("mode").textContent = latest.sample ? "SAMPLE" : { paper: "PAPER", testnet: "DEMO", live: "LIVE" }[latest.mode];
  $("mode").classList.toggle("live", latest.mode === "live");
  $("connection").textContent = latest.last_update && Date.now() / 1000 - latest.last_update < 40 ? "시세 연결됨" : "시세 대기";
  $("price").textContent = fmt(latest.quote?.price);
  $("source").textContent = latest.quote?.source || "공개 선물 시세 연결 대기";
  $("updated").textContent = `${stamp(latest.last_update * 1000)} KST`;
  $("engine-state").textContent = a.running ? "자동매매 가동" : a.halt ? `정지 · ${a.halt}` : "신규 진입 중단";
  $("equity").textContent = fmt(latest.equity); $("net").textContent = fmt(latest.net); color($("net"), latest.net);
  $("position").textContent = p ? `${p.direction > 0 ? "LONG" : "SHORT"} ${fmt(p.quantity, 3)}` : "없음";
  $("leverage").textContent = `${latest.settings.leverage}×`;
  $("action").textContent = d?.action || "WAIT";
  $("action").className = `decision-badge ${(d?.action || "wait").toLowerCase()}`;
  $("reason").textContent = latest.error || latest.entry_gate || d?.reason || "마감된 캔들 수신 대기";
  $("risk-status").textContent = latest.settings.strategy_mode ? `손실 한도 잔여 ${fmt(latest.remaining_risk)} USDT · 오늘 진입 ${a.risk?.entries || 0}/${latest.settings.max_entries_per_day}회` : "이 서버는 이전 엔진 실행 중 · 새 전략과 위험 통제는 아직 미적용";
  const active=latest.settings.strategy_mode==="active", scenarioMode=["multi","active"].includes(latest.settings.strategy_mode);
  document.querySelector('[data-tf="30m"]').title = active ? "진입 후보 · 15분봉 내부 구조·돌파 확인 필요" : scenarioMode ? "1시간봉 내부 구조·돌파 확인" : "참고 전용 · 자동매매 판단 제외";
  document.querySelector('[data-tf="15m"]').title = active ? "30분봉 내부 구조·돌파 확인 · 독립 진입 없음" : "참고 전용 · 자동매매 판단 제외";
  document.querySelector('[data-tf="15m"]').hidden = !latest.limits?.chart_timeframes?.includes("15m");
  $("decision-meta").textContent = d ? `${stamp(d.asof)} 마감봉 · ${d.version} · ${latest.settings.research_mode ? "모의 연구 · 실거래 차단" : "시장가 진입"} · 승률 미산정` : "규칙 적합도 기반 · 승률 미산정";
  $("research-review").hidden=!latest.settings.research_mode;
  const review=latest.research_review;
  $("research-review").textContent=review ? `모의 연구 · ${review.days}일 / 완료 ${review.closed_trades}회 / 완료 거래 순손익 합계 ${fmt(review.closed_net)} USDT / 동일 거래 비용 2배 ${fmt(review.fixed_trade_stress_net)} USDT · 구간별 자본 초기화 · 수익성 미검증 · 실거래 차단` : "모의 연구 · 수익성 미검증 · 실제 주문 전송 차단";
  const selection = d?.selection;
  $("stop").textContent = fmt(p?.stop || selection?.stop); $("target").textContent = fmt(p?.target || selection?.target);
  $("rr").textContent = p ? `${fmt(p.rr)} R` : d?.rr ? `${fmt(d.rr)} R` : "—";
  $("score").textContent = selection ? `${fmt(selection.score, 1)} / 100` : "—";
  const protectedNow=p?.protection_checked_at && Date.now()/1000-p.protection_checked_at<40;
  $("protection").textContent = a.pending ? "주문 결과 대조 필요" : p ? latest.mode === "paper" ? "모의 손절·익절 감시" : protectedNow ? `손절 ${p.stop_status} / 익절 ${p.target_status}` : "거래소 보호주문 확인 필요" : "포지션 없음";
  const checkTitles = {P00:"마감 데이터 최신성",P01:"4시간봉 3·5파 후보와 내부 구조",P02:"주봉 반대·충돌 없음",P03:"일봉·4시간봉 방향 일치",P04:"4시간봉 적합도 기준",P05:"1시간봉 같은 방향 내부 구조",P06:"1시간봉 종가 돌파",P07:"진입 방향 허용",P08:"비용 후 손익비 기준"};
  if (scenarioMode) {
    for (const key of Object.keys(checkTitles)) delete checkTitles[key];
    for (const check of d?.checks || []) checkTitles[check.id] = check.detail;
  }
  const checks = Object.entries(checkTitles).map(([id,title]) => { const check=d?.checks.find(c=>c.id===id), li = document.createElement("li"), icon = document.createElement("span"), text = document.createElement("span"); icon.className = `check-symbol ${check?.passed ? "" : "fail"}`; icon.textContent = check?.passed ? "✓" : check ? "○" : "·"; text.textContent = `${title} · ${check?.passed ? "통과" : check ? "미통과" : "앞 단계 대기"}`; if(check)li.title=check.detail;else li.className="muted"; li.append(icon, text); return li; });
  $("checks").replaceChildren(...checks);
  $("timeframes").replaceChildren(...(active?["1w","1d","4h","1h","30m","15m"]:["1w","1d","4h","1h"]).map(tf => {
    const frame = d?.frames[tf], el = document.createElement("div"); el.className = "tf";
    const h = document.createElement("h3"), strong = document.createElement("strong"), state = document.createElement("p"), fit = document.createElement("small");
    const candidate = frame?.candidates?.find(c => c.id === frame.selected) || frame?.candidates?.[0];
    h.textContent = tfNames[tf];
    strong.textContent = frame?.bias ? frame.bias > 0 ? "예상 LONG" : "예상 SHORT" : "중립 / 관망";
    color(strong, frame?.bias || 0);
    state.textContent = states[frame?.state] || "분석 대기";
    fit.textContent = candidate ? `${frame?.selected ? "Selected" : "Top candidate"} fit ${fmt(candidate.score, 1)} / 100` : "No rule-fitting candidate";
    el.append(h, strong, state, fit); return el;
  }));
  document.querySelector("#validation-view>.section-head .muted").textContent = `단일 재생 240개 ${active ? "15분" : scenarioMode ? "30분" : "1시간"}봉 · 전략 비교 480개 ${latest.limits?.strategy_modes?.includes("active")?"15분":"30분"}봉`;
  renderScenarios(); renderCandidates(); renderWaveEvidence(); renderJournal();
  renderSettingsSupport();
  if (!dirty) fillSettings();
  $("journal-path").textContent = latest.journal_path;
  $("key-state").textContent = latest.mode === "paper" ? "API 키 불필요" : latest.credentials_saved ? "이 모드의 키가 로컬에 저장됨" : "이 모드의 API 키 미등록";
  if (!modeDirty) for (const el of $("mode-form").elements.mode) el.checked = el.value === latest.mode;
  for (const el of $("credentials-form").querySelectorAll("input,button")) el.disabled = latest.mode === "paper" || busy;
  $("footer-mode").textContent = latest.mode === "live" ? "실계좌 · 실제 자금 사용" : latest.mode === "testnet" ? "Binance 데모 · 가상 자금" : latest.sample ? "합성 재생 · 실제 자금 미사용" : "실시간 시세 · 모의 체결 · 실제 자금 미사용";
  document.querySelectorAll("[data-action]").forEach(b => b.disabled = busy || (b.dataset.action === "start" && a.running) || (b.dataset.action === "pause" && !a.running));
  renderPreflight(latest.preflight);
  draw(); loadChart(); lucide.createIcons();
}
function renderCandidates() {
  const f = chartFrame(), shown = visibleCandidate();
  const threshold=latest.settings.research_mode ? latest.settings.research_min_score : latest.settings.min_score;
  $("candidate-context").textContent = `${tfNames[activeTf]} · 관측 방향 / 이후 예상 방향`;
  if (!f?.candidates?.length) empty("candidates", "규칙에 맞는 확정 카운트가 없습니다.");
  else $("candidates").replaceChildren(...f.candidates.map(c => {
    const tr = document.createElement("tr"); tr.classList.toggle("shown-count",shown?.id===c.id); textCell(tr, names[c.pattern] || c.pattern); textCell(tr, `${c.observed_direction_name || "미확정"} / 예상 ${c.direction_name}`, c.direction > 0 ? "positive" : c.direction < 0 ? "negative" : "muted"); textCell(tr, `${fmt(c.score, 1)} / 기준 ${fmt(threshold,0)}`); textCell(tr, c.subdivisions.length && c.subdivisions.every(s => s.confirmed) ? "확인" : "미확정"); textCell(tr, c.invalidation ? fmt(c.invalidation) : "—");
    const reference=isReferenceChart() || !["WAVE_3","WAVE_5","WAVE_C"].includes(c.pattern);
    const researchSelected=latest.settings.research_mode && latest.decision?.selection?.id===c.id;
    textCell(tr, `${f.selected === c.id ? "우선" : "대안"} · ${reference ? "참고 전용" : researchSelected ? "모의 연구 진입" : c.eligible ? "추가 진입 검사 필요" : "구조 미확인"}`);
    const actions=textCell(tr, ""), b=document.createElement("button"), i=document.createElement("i"); b.className="icon";b.title="Show candidate on chart";b.setAttribute("aria-label",`Show ${names[c.pattern] || "wave candidate"} on chart`);b.setAttribute("aria-pressed",String(shown?.id===c.id));i.dataset.lucide="eye";b.append(i);b.addEventListener("click",()=>{chartCount=c.id;renderCandidates();renderWaveEvidence();draw();lucide.createIcons();});actions.append(b,detailButton(c)); return tr;
  }));
  const failed = f?.rejected.flatMap(r => r.rules.filter(c => !c.passed).map(c => `${names[r.pattern] || r.pattern}: ${t(c.detail)}`)) || [];
  $("rejected").textContent = failed.length ? `Rejected hypotheses · ${[...new Set(failed)].join(" / ")}` : "";
}
function renderJournal() {
  const items = latest.events;
  if (!items.length) { empty("journal", "아직 거래·운영 기록이 없습니다."); return; }
  $("journal").replaceChildren(...items.map(e => {
    const tr = document.createElement("tr"), d = e.data;
    textCell(tr, stamp(e.time * 1000)); textCell(tr, e.kind); textCell(tr, d.direction || "—"); textCell(tr, fmt(d.price)); textCell(tr, fmt(d.net));
    const reason = d.reason || d.decision?.reason || d.action || "설정·운영 기록";
    const td = textCell(tr, reason.length > 45 ? reason.slice(0, 45) + "…" : reason); td.title = reason;
    const actions = textCell(tr, ""); actions.append(detailButton(d));
    if (["ENTRY", "EXIT", "RECOVERY", "ORDER_UNKNOWN"].includes(e.kind)) { const a = document.createElement("a"); a.href = `/journal/${e.id}`; a.textContent = "MD"; a.className = "document-link"; actions.append(a); }
    return tr;
  }));
}
function renderPreflight(report_) {
  if (!report_) { $("preflight").replaceChildren(); const p = document.createElement("p"); p.className = "muted"; p.textContent = "계정 검사 대기"; $("preflight").append(p); return; }
  $("preflight").replaceChildren(...report_.checks.map(c => { const row = document.createElement("div"), box = document.createElement("div"), name = document.createElement("strong"), small = document.createElement("small"), status = document.createElement("span"); row.className = "check-item"; name.textContent = c.name; small.textContent = c.detail; status.textContent = c.ok ? "통과" : "미통과"; status.className = c.ok ? "positive" : "negative"; box.append(name, small); row.append(box, status); return row; }));
}
function fillSettings() { for (const [k,v] of Object.entries(latest.settings)) { const input = $("settings-form").elements.namedItem(k); if (!input) continue; if (input.type === "checkbox") input.checked = v; else input.value = v; } $("leverage-output").textContent = `${latest.settings.leverage}×`; $("settings-state").textContent = latest.settings.strategy_mode ? "현재 적용 설정" : "기존 엔진 · 배정 자본 등 기존 설정 수정 가능"; }

function renderSettingsSupport() {
  const legacy = !Object.hasOwn(latest.settings, "strategy_mode");
  const leverage = $("settings-form").elements.leverage;
  leverage.min = latest.limits?.leverage_min ?? 1;
  leverage.max = latest.limits?.leverage_max ?? 5;
  leverage.title = `${leverage.min}–${leverage.max}× · Server-supported range`;
  for (const input of $("settings-form").querySelectorAll("input[name],select[name]")) {
    const supported = Object.hasOwn(latest.settings, input.name);
    input.disabled = !supported;
    input.closest("label").hidden = !supported;
  }
  if (legacy && !dirty) $("settings-state").textContent = "기존 엔진 · 배정 자본 등 기존 설정 수정 가능";
  $("compare-strategies").hidden = legacy;
  const activeSupported=latest.limits?.strategy_modes?.includes("active")===true;
  $("active-preset").hidden=!activeSupported;
  const activeOption=$("settings-form").elements.strategy_mode.querySelector('[value="active"]');
  activeOption.disabled=!activeSupported;activeOption.hidden=!activeSupported;
  $("research-preset").hidden=!Object.hasOwn(latest.settings,"research_mode");
  $("research-preset").disabled=latest.mode!=="paper";
  $("settings-form").elements.research_mode.disabled=!Object.hasOwn(latest.settings,"research_mode") || latest.mode!=="paper";
}

function settingsPayload(form) {
  const data = {};
  for (const [key, value] of new FormData(form)) {
    if (Object.hasOwn(latest.settings, key)) data[key] = value;
  }
  if (Object.hasOwn(latest.settings, "allow_short")) data.allow_short = form.elements.allow_short.checked;
  if (Object.hasOwn(latest.settings, "research_mode")) data.research_mode = form.elements.research_mode.checked;
  return data;
}

function setupRiskControls() {
  const review=document.createElement("div");review.id="research-review";review.className="risk-note";review.hidden=true;
  document.querySelector(".decision-band").after(review);
  const grid = $("settings-form").querySelector(".form-grid"), label = document.createElement("label"), select = document.createElement("select");
  label.textContent = "파동 전략"; label.className = "strategy-picker"; select.name = "strategy_mode";
  select.append(new Option("기존 엄격 · 4시간 3·5파", "strict"), new Option("다중 후보 · 4시간·1시간·C파 (검증 중)", "multi"), new Option("단기 적극형 · 30분 진입 + 15분 확인 (검증 중)", "active"));
  label.append(select); grid.prepend(label);
  const researchLabel=document.createElement("label"), research=document.createElement("input");
  research.type="checkbox";research.name="research_mode";
  researchLabel.className="checkbox";
  researchLabel.append(research,"잠정 파동 진입 연구 · 모의 전용");grid.after(researchLabel);
  const thresholdLabel=document.createElement("label"), threshold=document.createElement("input");
  thresholdLabel.textContent="모의 연구 적합도 기준";
  Object.assign(threshold,{type:"number",name:"research_min_score",min:55,max:100,step:1,required:true});
  thresholdLabel.append(threshold);grid.append(thresholdLabel);
  const riskLabel=document.createElement("label"), riskInput=document.createElement("input");
  riskLabel.textContent="모의 연구 위험 배수 · 최대 0.5";
  Object.assign(riskInput,{type:"number",name:"research_risk_multiplier",min:.05,max:.5,step:.025,required:true});
  riskLabel.append(riskInput);grid.append(riskLabel);
  const controls = [["max_entries_per_day","하루 신규 진입 한도 · 회",1,20,1], ["cooldown_minutes","청산 후 대기 · 분",15,1440,1], ["loss_streak_limit","연속 손실 휴식 기준 · 회",1,10,1], ["loss_pause_minutes","연속 손실 후 휴식 · 분",60,10080,1], ["peak_drawdown_percent","자산 고점 대비 손실 한도 · %",.5,20,.5], ["max_entry_drift_r","다중·단기 추격 거리 한도 · R",.05,.5,.05], ["max_cost_fraction","다중·단기 비용 / 손절 위험 한도",.05,.4,.05], ["max_stop_percent","단기 전략 최대 구조 손절 거리 · %",.1,5,.1]];
  for (const [name,text,min,max,step] of controls) {
    const l=document.createElement("label"), input=document.createElement("input"); l.textContent=text;
    Object.assign(input,{name,type:"number",min,max,step,required:true}); l.append(input); grid.append(l);
  }
  const preset=document.createElement("button"), icon=document.createElement("i");icon.dataset.lucide="timer";
  preset.id="active-preset";preset.type="button";preset.hidden=true;
  preset.append(icon,"단기 기본값 · 최대 8회 / 대기 15분");
  preset.addEventListener("click",()=>{
    const form=$("settings-form");
    for(const [name,value] of Object.entries({strategy_mode:"active",max_entries_per_day:8,cooldown_minutes:15,max_stop_percent:1}))form.elements.namedItem(name).value=value;
    dirty=true;$("settings-state").textContent="저장 전 · 단기 적극형 / 최대 8회 / 대기 15분 / 손절 거리 상한 1% · 자본·레버리지·손실 한도 유지";
  });
  grid.before(preset);
  const experiment=document.createElement("button"), flask=document.createElement("i");flask.dataset.lucide="flask-conical";
  experiment.id="research-preset";experiment.type="button";experiment.hidden=true;
  experiment.append(flask,"파동 연구 · 모의 전용");
  experiment.addEventListener("click",()=>{
    if(latest.mode!=="paper")return;
    preset.click();$("settings-form").elements.research_mode.checked=true;
    $("settings-state").textContent="저장 전 · 잠정 파동 연구 / 모의 전용 / 위험 배수 최대 50% · 최소 수량 미달 시 관망";
  });grid.before(experiment);
}
function setupComparison() {
  const b=document.createElement("button"), i=document.createElement("i"); i.dataset.lucide="columns-3";
  b.id="compare-strategies"; b.append(i,"동일 기간 전략 비교"); $("backtest").before(b);
  b.addEventListener("click",()=>task(b,async()=>{await api("/api/backtest",{compare:true});await pollBacktest();}));
  const box=document.createElement("div");box.className="table-scroll";box.id="comparison-results";$("bt-state").after(box);
}
function renderComparison(data) {
  const box=$("comparison-results");box.replaceChildren(); if(!data.comparison)return;
  const table=document.createElement("table"), head=table.createTHead().insertRow();
  for(const text of ["전략","순수익률","최대 낙폭","완료 거래","승률","평균 거래 손익"]) {const th=document.createElement("th");th.textContent=text;head.append(th);}
  const body=table.createTBody();for(const [key,r] of Object.entries(data.comparison)){
    const tr=body.insertRow();for(const text of [{baseline:"기존 엄격",multi:"다중 후보",multi_stress:"다중 후보 · 비용 2배",active:"단기 적극형",active_stress:"단기 적극형 · 비용 2배",research:"모의 연구",research_stress:"모의 연구 · 비용 2배"}[key] || key,`${fmt(r.return*100)}%`,`${fmt(r.max_drawdown*100)}%`,r.closed_trades,r.win_rate==null?"미산정":`${fmt(r.win_rate*100)}%`,fmt(r.average_trade)])textCell(tr,text);
  }box.append(table);$("bt-state").textContent=data.verdict;
  const reasons=Object.entries((data.comparison.research || data.comparison.active || data.comparison.multi).wait_reasons || {}).sort((a,b)=>b[1]-a[1]);
  for(const [reason,count] of reasons.slice(0,4)){const p=document.createElement("p");p.className="muted";p.textContent=`${data.comparison.research?"모의 연구":data.comparison.active?"단기 적극형":"다중 후보"} 관망 ${count}회 · ${reason}`;box.append(p);}
}
function renderScenarios() {
  const rows=latest.decision?.scenarios || []; $("scenario-section").hidden=!["multi","active"].includes(latest.settings.strategy_mode);
  if(!rows.length) {empty("scenarios","유효한 선행 파동 후보 대기",8);return;}
  $("scenarios").replaceChildren(...[...rows].sort((a,b)=>Number(b.passed)-Number(a.passed)||b.priority-a.priority).map(r=>{
    const tr=document.createElement("tr"); textCell(tr,tfNames[r.timeframe]);textCell(tr,names[r.pattern]);textCell(tr,r.direction>0?"LONG":"SHORT");
    textCell(tr,fmt(r.priority,1));textCell(tr,`${fmt(r.rr)} R`);textCell(tr,`${fmt(r.risk_multiplier*100,0)}%`);
    textCell(tr,latest.decision?.selection?.id===r.id?"최종 선택":r.passed?"조건 통과 · 대안":r.checks.filter(c=>!c.passed).map(c=>c.detail).join(" · ")||"대기");
    textCell(tr,"").append(detailButton(r));return tr;
  }));
}
async function refresh() { if (refreshing) return; refreshing = true; try { latest = await api("/api/status"); render(); } catch(e) { notice("서버 연결 실패: " + e.message, true); } finally { refreshing = false; } }
async function task(button, fn) { button.disabled = true; try { await fn(); } catch(e) { notice(e.message, true); } finally { button.disabled = false; await refresh(); } }
async function confirm(title, text, live = false) {
  $("confirm-title").textContent = title; $("confirm-text").textContent = text; $("live-confirm").hidden = !live; $("live-phrase").value = ""; $("risk-accept").checked = false;
  $("confirm").returnValue = "cancel"; $("confirm").showModal();
  return new Promise(resolve => $("confirm").addEventListener("close", () => resolve($("confirm").returnValue === "yes"), { once: true }));
}
async function control(action) {
  if (busy) return;
  if (action === "start" && latest.mode !== "paper" && !await confirm(latest.mode === "live" ? "실계좌 자동매매 시작" : "데모 자동매매 시작", `BTCUSDT / ${latest.settings.leverage}배 / 배정 ${fmt(latest.settings.capital)} USDT. 조건 충족 시 시장가 진입과 거래소 보호 주문을 실행합니다.`, latest.mode === "live")) return;
  if (["flatten","emergency"].includes(action) && !await confirm(action === "emergency" ? "비상 정지" : "포지션 정리", "신규 진입을 중단하고 소유 포지션을 시장가로 정리합니다. 체결 비용과 슬리피지가 발생할 수 있습니다.")) return;
  busy = true; render();
  try { await api(`/api/control/${action}`, { confirmation: $("live-phrase").value, risk_accepted: $("risk-accept").checked }); notice({ start:"자동매매가 가동되었습니다. 조건이 충족되지 않으면 관망합니다.", pause:"신규 진입을 중단했습니다. 기존 포지션 보호는 유지합니다.", flatten:"소유 포지션 정리를 확인했습니다.", emergency:"비상 정지를 완료했습니다." }[action]); }
  catch(e) { notice(e.message, true); } finally { busy = false; await refresh(); }
}

function context(canvas) { const r = canvas.getBoundingClientRect(); if (r.width < 1) return null; const ratio = devicePixelRatio || 1; canvas.width = Math.round(r.width * ratio); canvas.height = Math.round(r.height * ratio); const c = canvas.getContext("2d"); c.scale(ratio, ratio); return { c, w:r.width, h:r.height }; }
function drawEquity(){ if(!report||activeView!=="validation")return;const ctx=context($("equity-chart"));if(!ctx)return;const{c,w,h}=ctx, ps=report.curve,vs=ps.map(p=>p.equity),low=Math.min(...vs)-.1,high=Math.max(...vs)+.1;c.strokeStyle="#e4eae6";c.lineWidth=1;for(let i=0;i<5;i++){c.beginPath();c.moveTo(0,15+i*(h-40)/4);c.lineTo(w-60,15+i*(h-40)/4);c.stroke();}c.strokeStyle="#087d5f";c.lineWidth=2;c.beginPath();ps.forEach((p,i)=>{const x=i/Math.max(1,ps.length-1)*(w-65),y=15+(h-40)*(1-(p.equity-low)/(high-low));i?c.lineTo(x,y):c.moveTo(x,y);});c.stroke();c.fillStyle="#758079";c.font="10px sans-serif";c.fillText(fmt(vs.at(-1)),w-56,h/2);}
async function pollBacktest(){ if(polling)return;polling=true;try{const j=await api("/api/backtest");$("backtest").disabled=j.running;$("compare-strategies").disabled=j.running;$("bt-state").textContent=j.running?"시간순 검증 실행 중":j.error||j.report?j.error||"검증 완료 · 통계적 승률 보정 미실시":"검증 대기";if(j.report){const key=latest.settings.research_mode?"research":latest.settings.strategy_mode==="strict"?"baseline":latest.settings.strategy_mode;report=j.report.comparison?.[key] || j.report.comparison?.multi || j.report;renderComparison(j.report);$("bt-net").textContent=fmt(report.net);color($("bt-net"),report.net);$("bt-dd").textContent=`${fmt(report.max_drawdown*100)}%`;$("bt-count").textContent=report.closed_trades;$("bt-bars").textContent=`${report.bars}봉`;$("assumptions").replaceChildren(...[...report.assumptions,...(j.report.notes||[])].map(t=>{const li=document.createElement("li");li.textContent=t;return li;}));drawEquity();}}catch(e){notice(e.message,true);}finally{polling=false;}}

document.querySelectorAll("[data-view]").forEach(b=>b.addEventListener("click",()=>{activeView=b.dataset.view;document.querySelectorAll(".view").forEach(v=>v.hidden=v.id!==`${activeView}-view`);document.querySelectorAll("[data-view]").forEach(tab=>{tab.classList.toggle("active",tab===b);tab.setAttribute("aria-selected",String(tab===b));});draw();loadChart();drawEquity();if(activeView==="validation")pollBacktest();}));
document.querySelectorAll("[data-tf]").forEach(b=>b.addEventListener("click",()=>{activeTf=b.dataset.tf;chartCount="auto";chartAnchor=null;chartHover=null;document.querySelectorAll("[data-tf]").forEach(t=>{t.classList.toggle("selected",t===b);t.setAttribute("aria-pressed",String(t===b));});if(latest){renderCandidates();renderWaveEvidence();draw();loadChart();lucide.createIcons();}}));
document.querySelectorAll("[data-action]").forEach(b=>b.addEventListener("click",()=>control(b.dataset.action)));
$("settings-form").addEventListener("input",()=>{dirty=true;$("settings-state").textContent="저장하지 않은 변경";$("leverage-output").textContent=`${$("settings-form").elements.leverage.value}×`;} );
$("settings-form").addEventListener("submit",e=>{e.preventDefault();task(e.submitter,async()=>{await api("/api/settings",settingsPayload(e.target));dirty=false;notice("위험 설정을 저장했습니다. 거래소 계정 검사 결과는 다시 검사해야 갱신됩니다.");});});
$("revert").addEventListener("click",()=>{dirty=false;fillSettings();});
$("mode-form").addEventListener("change",()=>{modeDirty=true;});
$("mode-form").addEventListener("submit",e=>{e.preventDefault();task(e.submitter,async()=>{await api("/api/mode",{mode:e.target.elements.mode.value});dirty=false;modeDirty=false;notice("모드를 전환했습니다. 자동매매는 중단 상태입니다.");});});
$("credentials-form").addEventListener("submit",e=>{e.preventDefault();task(e.submitter,async()=>{try{await api("/api/credentials",Object.fromEntries(new FormData(e.target)));notice("키를 로컬 비밀 파일에 저장했습니다.");}finally{e.target.reset();}});});
$("check").addEventListener("click",e=>task(e.currentTarget,async()=>{const r=await api("/api/check",{});notice(r.ok?"계정 검사를 통과했습니다.":"계정 검사에서 미통과 항목이 있습니다.",!r.ok);}));
$("configure").addEventListener("click",async e=>{const b=e.currentTarget;if(await confirm("거래소 설정 변경",`BTCUSDT를 격리 증거금과 ${latest.settings.leverage}배로 설정합니다. 포지션·미체결 주문이 있으면 변경하지 않습니다.`))task(b,async()=>{await api("/api/configure",{confirmed:true});notice("거래소 설정을 적용했습니다. 계정 검사를 다시 실행하세요.");});});
$("recover").addEventListener("click",e=>task(e.currentTarget,async()=>{await api("/api/recover",{});notice("거래소 주문 대조를 완료했습니다. 자동매매는 중단 상태입니다.");}));
$("backtest").addEventListener("click",e=>task(e.currentTarget,async()=>{await api("/api/backtest",{});await pollBacktest();}));
$("refresh").addEventListener("click",refresh);$("close-details").addEventListener("click",()=>$("details").close());
new ResizeObserver(()=>{draw();drawEquity();}).observe(document.querySelector("main"));
setupRiskControls();setupComparison();setupChart();lucide.createIcons();refresh();setInterval(()=>{refresh();if(activeView==="validation")pollBacktest();},3000);
document.addEventListener("localechange",()=>{if(latest)render();});
