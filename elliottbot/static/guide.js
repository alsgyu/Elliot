"use strict";

// Read-only illustrations of execution.payoff, never an order planner.
function guidePayoff(price, stop, target, direction, fee, slip, filled = false) {
  const entry = filled ? price : price * (1 + direction * slip);
  const stopFill = stop * (1 - direction * slip), targetFill = target * (1 - direction * slip);
  const loss = (entry - stopFill) * direction + (entry + stopFill) * fee;
  const reward = (targetFill - entry) * direction - (entry + targetFill) * fee;
  return {loss, reward, rr:loss > 0 ? reward / loss : null,
    distance:(price - stop) * direction, costFraction:loss > 0 ? (loss - (price - stop) * direction) / loss : null};
}

function guideRound(value, tick, up) {
  const units = value / tick, nearest = Math.round(units);
  const stable = Math.abs(units - nearest) < 1e-8 ? nearest : units;
  return Number(((up ? Math.ceil(stable) : Math.floor(stable)) * tick).toPrecision(15));
}

function guideMath(candidate, settings, rules) {
  if (!candidate || ![1,-1].includes(candidate.direction)) return null;
  const {direction} = candidate, fee = Number(settings.fee_bps) / 10000;
  const slip = Number(settings.slippage_bps) / 10000, rr = Number(settings.min_rr);
  const tick = Number(rules?.tick), hasTick = Number.isFinite(tick) && tick > 0;
  if (![candidate.stop,candidate.target,rr].every(v => Number.isFinite(Number(v)) && Number(v) > 0)
      || ![fee,slip].every(v => Number.isFinite(v) && v >= 0 && v < 1)) return null;
  const stop = hasTick ? guideRound(Number(candidate.stop), tick, direction < 0) : Number(candidate.stop);
  const target = hasTick ? guideRound(Number(candidate.target), tick, direction < 0) : Number(candidate.target);
  if ((target - stop) * direction <= 0) return null;
  // Solve reward / loss >= min_rr for the pre-slippage entry quote.
  const raw = direction > 0
    ? (1-fee)*(1-slip)*(target+rr*stop)/((1+fee)*(1+slip)*(1+rr))
    : (1+fee)*(1+slip)*(target+rr*stop)/((1-fee)*(1-slip)*(1+rr));
  const precision = hasTick ? tick : .01;
  const boundary = guideRound(raw, precision, direction < 0);
  return {direction,fee,slip,rr,stop,target,boundary,tick:hasTick ? tick : null,
    exists:(boundary-stop)*direction > 0};
}

function guideExplanation(c) {
  const ps = c.pivots || [], p = i => fmt(ps[i]?.price), len = i => Math.abs(ps[i].price-ps[i-1].price);
  const long = c.direction > 0, move = long ? "상승" : "하락", sign = long ? "+" : "−";
  const broken = long ? "이하" : "이상", extreme = long ? "저점" : "고점";
  const invalid = `${fmt(c.invalidation)} ${broken}`, stop = `${fmt(c.stop)} ${broken}`;
  const common = {hypothesis:"위치와 세부 구조가 아직 확정되지 않은 대안입니다.",
    invalidation:"이 후보에는 수치로 확정한 무효화 기준이 없습니다. 0은 실제 가격이 아니라 미산정 표시입니다.",
    stop:"자동 진입용 손절 가격이 산정되지 않았습니다.", target:"자동 진입용 목표 가격이 산정되지 않았습니다."};
  if (c.pattern === "WAVE_3" && ps.length >= 3) return {
    hypothesis:`0번 ${p(0)}에서 1번 ${p(1)}까지를 1파, 2번 ${p(2)}까지를 되돌림으로 보고, 2파가 끝나 다음 3파가 ${move}한다는 가정입니다. 아직 3파 완료를 확인한 것이 아닙니다.`,
    invalidation:`${invalid}에 닿으면 2파가 1파 시작점까지 되돌아갑니다. 이 시작점에 붙인 1–2파 카운트는 성립하지 않아 다시 세어야 합니다. 시장의 모든 ${move} 가능성이 사라진다는 뜻은 아닙니다.`,
    stop:`${stop}에 닿으면 '2파가 ${p(2)}에서 끝났다'는 진입 가정을 버리는 기준입니다. 1파 시작점은 지켜도 2파 끝점이 달라질 수 있어 무효화선보다 먼저 손절 기준에 닿을 수 있습니다. 무효화선까지 버티라는 뜻이 아닙니다.`,
    target:`2파 끝점 ${p(2)} ${sign} 1파 길이 ${fmt(len(1))} × 1.618 = ${fmt(c.target)}. 프로그램의 3파 목표 추정 정책이며 필수 도달 가격도 매수·매도 진입 상한도 아닙니다.`};
  if (c.pattern === "WAVE_5" && ps.length >= 5) return {
    hypothesis:`1–2–3–4파가 형성됐고 4파가 ${p(4)}에서 끝나 다음 5파가 ${move}한다는 가정입니다.`,
    invalidation:`${invalid}는 1파 끝점입니다. 4파가 1파 가격 영역과 겹치면 이 프로그램의 일반 추진파 카운트를 유지하지 않습니다. 대각선 등 다른 카운트의 가능성까지 부정하지는 않습니다.`,
    stop:`${stop}에 닿으면 '${p(4)}에서 4파가 끝났다'는 진입 가정을 버립니다. 일반 추진파 전체의 겹침 경계와 이 매매의 손절은 서로 다른 기준입니다.`,
    target:`4파 끝점 ${p(4)} ${sign} 투영 길이 ${fmt(Math.min(len(1),len(3)))} = ${fmt(c.target)}. 보통 1파 길이를 쓰되, 3파가 1파보다 짧으면 3파 길이까지만 투영합니다. 실제 5파의 끝은 보장되지 않습니다.`};
  if (c.pattern === "WAVE_C" && ps.length >= 3) return {
    hypothesis:`상위 추진파 완료 위치의 증거를 바탕으로 A파 ${p(0)} → ${p(1)}, B파 종료 ${p(2)}를 가정하고 C파의 ${move}을 검토합니다. 단순한 1–2파 모양만으로 C파라고 정하지 않습니다.`,
    invalidation:`${invalid}는 A파 시작점입니다. B파가 여기까지 되돌아가면 이 프로그램이 사용하는 지그재그 A–B 해석을 다시 검토해야 합니다.`,
    stop:`${stop}에 닿으면 'B파가 ${p(2)}에서 끝났고 C파가 시작됐다'는 진입 가정이 깨집니다.`,
    target:`B파 끝점 ${p(2)} ${sign} A파 길이 ${fmt(len(1))} × 1.0 = ${fmt(c.target)}. A와 C의 길이가 같다는 프로그램의 목표 가정이며 필수 규칙이 아닙니다.`};
  if (c.pattern === "ZIGZAG_COMPLETE" && ps.length >= 4) return {
    hypothesis:`${p(0)} → A ${p(1)} → B ${p(2)} → C ${p(3)}를 ABC 조정으로 해석하고, C가 끝나 반대쪽으로 ${move}할 가능성을 보는 후보입니다. '완료'라는 이름은 완료 가설이지 확정된 반전 신호가 아닙니다.`,
    invalidation:`${invalid}는 C 끝점입니다. 그 ${extreme}을 다시 시험하면 'C가 여기서 끝났다'는 가정을 철회합니다. ABC 전체가 불가능하다는 뜻은 아니며 C 연장이나 다른 카운트가 가능합니다.`,
    stop:`${stop}로 무효화선과 같은 값입니다. C 완료 가설의 실패 기준을 함께 표시한 것으로, 현재 엔진에서는 이 완료 후보만으로 신규 진입하지 않습니다.`,
    target:`ABC 시작점 ${p(0)}까지 되돌아온다고 가정한 참고 목표 ${fmt(c.target)}입니다. 시작점 복귀는 엘리엇 파동의 필수 규칙이 아니며 반드시 도달하지 않습니다.`};
  if (c.pattern === "IMPULSE_COMPLETE" && ps.length >= 6) return {
    hypothesis:`5파 끝점 ${p(5)}에서 추진 5개 파동이 완료됐고 반대쪽으로 ${move}하는 조정이 시작된다는 가설입니다. 반전 확인이나 즉시 주문을 뜻하지 않습니다.`,
    invalidation:`${invalid}는 5파 끝점입니다. 이 ${extreme}을 다시 시험하면 '추진파가 이미 끝났다'는 가정을 철회합니다. 기존 1–5 구조 전부가 무조건 틀렸다는 뜻이 아니라 5파 연장 등 재해석이 필요합니다.`,
    stop:`${stop}로 완료 가설의 무효화선과 같습니다. 이 완료 후보는 단독 자동 진입 대상이 아닙니다.`,
    target:`이전 4파 끝점 ${p(4)}으로 되돌아간다는 참고 목표 ${fmt(c.target)}입니다. 확정된 조정의 끝이나 필수 도달 가격이 아닙니다.`};
  return common;
}

function guideRows(id, rows) {
  $(id).replaceChildren(...rows.map(([title,description,className]) => {
    const row = document.createElement("div"), dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = title; dd.textContent = description; row.className = className || ""; row.append(dt,dd); return row;
  }));
}

let guideCandidateKey = "";
function renderChartGuide() {
  const data = chartSnapshot(), c = visibleCandidate(), d = latest?.decision, settings = latest?.settings || {};
  const scenarioMode=["multi","active"].includes(settings.strategy_mode), active=settings.strategy_mode==="active";
  const key = `${latest?.mode}:${latest?.sample}:${activeTf}:${c?.id || "none"}`;
  if (guideCandidateKey !== key) {
    resetManualInputs(true);
    guideCandidateKey = key;
  }
  const filledMode=document.querySelector('[name="manual-entry-kind"]:checked').value==="filled";
  $("guide-entry-title").textContent=filledMode?"실제 체결가 · USDT":"가정 진입가 · USDT";
  $("guide-entry-note").textContent=filledMode?"분할 체결 시 평균가 · 직접 입력":"수동 검토는 입력값 기준";
  $("guide-context").textContent = `${tfNames[activeTf]} · 분석 ${chartStamp(data.analysis_asof)} KST`;
  const stale = !latest?.sample && (!latest?.last_update || Date.now()/1000-latest.last_update>40
    || !!data.warning || !data.fetched_at || Date.now()/1000-data.fetched_at>45
    || !data.analysis_asof || Date.now()-data.analysis_asof>data.interval+120000);
  const selected = !!c && d?.selection?.id === c.id && d.selection.pattern === c.pattern && d.selection.timeframe === activeTf;
  const scenario = d?.scenarios?.find(r => r.id===c?.id && r.timeframe===activeTf && r.pattern===c?.pattern);
  const trigger = selected ? d.trigger : scenario?.trigger;
  let role;
  if (activeTf === "15m") role = active ? "15m confirms 30m internal structure and a fresh closing breakout; it never enters independently." : "15m is reference-only and does not affect order decisions in this strategy.";
  else if (activeTf === "30m") role = active ? "In the short-term profile, 30m is an entry candidate. The 15m structure, a fresh close breakout, costs, stop distance, and risk limits must also pass." : scenarioMode ? "30m confirms 1H structure and breakouts; it never enters independently." : "30m is reference-only and does not affect order decisions in this strategy.";
  else if (["1w","1d"].includes(activeTf)) role = "This timeframe provides higher-degree context; it is not a standalone order signal.";
  else if (activeTf === "1h" && !scenarioMode) role = "In the strict profile, 1H confirms a 4H entry.";
  else role = "Structure, higher-timeframe direction, a lower-timeframe close breakout, costs, and risk limits must all pass before an entry is considered.";
  if(settings.research_mode)role="Paper research checks at least one internal subdivision, price rules, a fresh breakout or confirmed correction endpoint, and cost and risk limits. It never sends live orders.";
  const qualification = !c ? "No wave candidate is available." : settings.research_mode && selected ? "This paper-research candidate does not confirm a complete 5-3 structure." : !c.eligible ? "This candidate is not eligible for automated entry on its own." : "Meeting the structure requirements does not authorize an order.";
  const signal = selected && ["LONG","SHORT"].includes(d?.action) ? "The displayed candidate is selected as the final signal, but this does not mean an order has filled." : "The displayed candidate has no final entry signal yet.";
  const operation = latest?.error || latest?.entry_gate || (latest?.account?.running ? "Automated trading active" : "New entries paused");
  $("guide-state").textContent = `${stale ? "Data may be delayed or out of sync; prices below may be historical references. " : ""}${qualification} ${role} ${signal} Overall decision: ${d?.action || "WAIT"} · ${t(d?.reason || "분석 대기")}. Execution status: ${t(operation)}.`;
  const explanation = c ? guideExplanation(c) : null;
  $("guide-summary").textContent = c ? `${names[c.pattern] || c.pattern} · 관측 ${c.observed_direction_name || "미확정"} / 이후 예상 ${c.direction_name}. ${explanation.hypothesis}` : "카운트는 고점·저점에 파동 번호를 붙인 하나의 해석입니다. 현재는 설명할 후보가 없어 진입 가격, 무효화·손절·목표를 산정하지 않습니다. 회색 스윙만으로 거래 방향을 정할 수 없습니다.";
  guideRows("guide-levels", c ? [
    [`카운트 무효화 · ${c.invalidation>0 ? fmt(c.invalidation)+" USDT" : "미산정"}`,explanation.invalidation,"guide-invalidation"],
    [`후보 손절 · ${c.stop>0 ? fmt(c.stop)+" USDT" : "미산정"}`,explanation.stop,"guide-stop"],
    [`후보 목표 · ${c.target>0 ? fmt(c.target)+" USDT" : "미산정"}`,explanation.target,"guide-target"]
  ] : []);
  const math = guideMath(c,settings,latest?.quote?.rules), long = c?.direction > 0;
  $("guide-entry").disabled = !math;
  const digits = Math.min(8,Math.max(2,math?.tick ? Math.ceil(-Math.log10(math.tick)) : 2));
  const priceText = v => fmt(v,digits);
  if (math) {
    const bound = priceText(math.boundary), stop = priceText(math.stop);
    guideRows("guide-bounds", math.exists ? long ? [
      [`Lower bound from stop distance only · above ${stop}`,"For a long, entry must be above the stop. A price just above it is not necessarily a safe buy; entering too close makes trading costs large relative to risk."],
      [`Upper bound from reward/risk only · ${bound} or below`,`After assumed costs, an entry above this level falls short of the ${fmt(math.rr,2)} R minimum if the target is unchanged. It may still be a chase even when the target has room.`]
    ] : [
      [`Lower bound from reward/risk only · ${bound} or above`,`For a short, chasing lower leaves less potential profit before the target. An entry below this level falls short of the ${fmt(math.rr,2)} R minimum after assumed costs.`],
      [`Upper bound from stop distance only · below ${stop}`,"For a short, entry must be below the stop. A price just below it is not necessarily a safe short; entering too close makes costs a larger share of risk."]
    ] : [["No entry range meets the minimum reward/risk","With these stop, target, and cost assumptions, no entry price meets the minimum reward/risk."]]);
    $("guide-costs").textContent = `${fmt(math.rr,2)} R은 손절 시 예상 손실 1에 대해 목표 시 예상 순이익이 ${fmt(math.rr,2)}라는 비율이지 승률이 아닙니다. 설정된 편도 수수료 ${fmt(settings.fee_bps,1)} bps와 슬리피지 ${fmt(settings.slippage_bps,1)} bps를 진입·청산에 반영한 예시입니다. 1 bps = 0.01%. ${math.tick ? `손절 ${priceText(math.stop)}, 목표 ${priceText(math.target)}는 호가 단위로 보정했고 진입 경계도 안쪽으로 맞췄습니다.` : "호가 단위 미수신: 경계는 소수 둘째 자리 안쪽으로 표시합니다."} 실제 계정 수수료가 더 높으면 실행 범위는 좁아질 수 있습니다. 펀딩비와 급변 비용은 이 계산에 포함하지 않습니다.`;
  } else {
    guideRows("guide-bounds", [["진입 가격 미산정","방향·손절·목표 또는 비용 설정이 부족하거나 유효하지 않습니다. 임의의 매수·매도 범위를 만들지 않습니다."]]);
    $("guide-costs").textContent = "손익비 R은 목표 체결 시 예상 순이익을 손절 체결 시 예상 순손실로 나눈 비율이며 성공 확률이 아닙니다.";
  }
  if (trigger?.price>0) {
    const tf = trigger.timeframe || "1h";
    const expires = scenarioMode && trigger.bar_end ? trigger.bar_end + ({"15m":900000,"30m":1800000,"1h":3600000}[tf] || 0) : null;
    const expired = expires && (latest.sample ? Number(latest.quote?.time) : Date.now()) >= expires;
    const timeframe = {"15m":"15m","30m":"30m","1h":"1H","4h":"4H","1d":"1D","1w":"1W"}[tf] || tf;
    $("guide-trigger").textContent = `${timeframe} closing breakout threshold ${fmt(trigger.price)}: the candle must close ${long ? "above" : "below"} this level. This is not a limit-order price or a direct ${long ? "lower" : "upper"} bound. The entry reference is the ${long ? "ask" : "bid"} at confirmation; market fills may differ.${scenarioMode ? ` Multi-timeframe profiles also check for a fresh breakout, signal expiry, and entry drift within ${fmt(settings.max_entry_drift_r,2)} R of the signal close.` : ""}${expires ? ` Signal expires ${chartStamp(expires)} KST${expired ? " · expired; a fresh signal is required." : " · all other conditions must pass before expiry."}` : ""}`;
  } else $("guide-trigger").textContent = "No closing-breakout level is linked to the displayed candidate, so breakout entry bounds are unconfirmed. Do not treat the price calculations above as permission to trade now.";
  $("guide-range-caution").textContent = `위 범위는 '손절 거리 + 손익비'만의 진입 전 참고 구간이며 주문 가능 구간이 아닙니다. ${filledMode?"이미 체결 모드의 수동 포지션 계산과 달리, 위 상한·하한은 진입 슬리피지도 포함한 가정입니다. ":""}${scenarioMode ? `비용 비중 한도 ${fmt(Number(settings.max_cost_fraction)*100,0)}%, 신호 만료와 추격 제한도 별도입니다. ` : ""}${active?`단기 전략의 구조 손절 거리 상한은 ${fmt(settings.max_stop_percent)}%이며 초과 시 진입을 제외합니다. `:""}방향 허용, 내부 파동, 시간봉 충돌, 계정 권한, 스프레드, 수량·위험 한도를 통과해야 하며, 레버리지를 높여도 이 파동의 손절·목표가 유리하게 바뀌지는 않습니다.`;
  renderGuideCalculation(math,stale);
  renderManualReview(c,data,stale);
  const steps = c && math ? [
    `Identify the candidate shown on the ${{"15m":"15m","30m":"30m","1h":"1H","4h":"4H","1d":"1D","1w":"1W"}[activeTf]} chart. The observed ${c.observed_direction_name === "상승" ? "bullish" : c.observed_direction_name === "하락" ? "bearish" : "unconfirmed"} move describes the wave already drawn; projected ${c.direction > 0 ? "LONG" : "SHORT"} is a hypothesis about what may follow. Do not mix levels from different candidates.`,
    `${long ? "If price falls to" : "If price rises to"} the candidate stop at ${fmt(c.stop)}, reassess the trade hypothesis anchored at this endpoint. ${c.stop===c.invalidation ? "The count invalidation is at the same price." : `The separate count invalidation at ${fmt(c.invalidation)} marks a broader boundary; it is not an instruction to widen the stop.`} The wave engine checks highs and lows of closed candles, so a closing-price recovery alone does not preserve the candidate.`,
    `Being between the stop and target does not satisfy entry conditions. ${long ? "Buying above the upper bound may leave insufficient reward/risk" : "Shorting below the lower bound may leave insufficient reward/risk"}, and entering too close to the stop increases costs relative to risk. Lower-timeframe structure and a closing breakout are required first.`,
    `Reaching the target at ${fmt(c.target)} does not require a reversal or an immediate position in the opposite direction. The candidate target is a projection; verify any take-profit through the open position's orders and fills.`
  ] : ["There is no valid price reference yet. Do not derive an entry from unlabeled swings or an alternative count name alone.","The blue line connects observed pivots; the dashed gold line shows an unconfirmed path after the latest pivot. Neither line guarantees profit or future direction."];
  $("guide-steps").replaceChildren(...steps.map(text=>{const li=document.createElement("li");li.textContent=text;return li;}));
}

function renderGuideCalculation(math,stale) {
  const box=$("guide-calculation"), input=$("guide-entry");
  const direction=manualDirection(), filled=document.querySelector('[name="manual-entry-kind"]:checked').value==="filled";
  input.placeholder=filled?"Enter average fill price":"Enter planned entry price";
  if (!math) {input.placeholder="No candidate to calculate";box.textContent="Waiting to calculate entry-price examples";return;}
  if (direction===null) {box.textContent="내 포지션 방향을 먼저 선택해야 합니다. 체결가만으로 롱·숏을 판단하지 않습니다.";return;}
  if (direction!==math.direction) {
    box.textContent="수동 방향이 표시 후보의 예상 방향과 다릅니다. 이 후보의 손절·목표를 반대로 뒤집어 계산하지 않습니다.";return;
  }
  const manual=input.value!=="", quote=Number(latest.quote?.[direction>0 ? "ask" : "bid"]);
  if (filled && !manual) {box.textContent="이미 체결 모드에는 실제 체결가 입력이 필요합니다. 현재 호가를 체결가로 간주하지 않습니다.";return;}
  const price=manual ? Number(input.value) : quote;
  if(input.validity.badInput || !Number.isFinite(price) || price<=0) {box.textContent="양수인 가정 진입가가 필요합니다. 현재 방향별 호가가 없으면 최근 종가로 대체하지 않습니다.";return;}
  const result=guidePayoff(price,math.stop,math.target,math.direction,math.fee,math.slip,filled);
  const source=manual ? filled ? "직접 입력한 체결가" : "직접 입력한 가정 진입가" : `${latest.sample ? "합성" : stale ? "지연된" : "조회 시점"} ${math.direction>0 ? "매도호가" : "매수호가"}`;
  const initial=`입력 방향 ${direction>0?"LONG(롱)":"SHORT(숏)"} · ${source} ${fmt(price)} USDT 기준. ${filled ? "거래소 체결 조회값은 아니며 진입 슬리피지는 재차 더하지 않습니다." : "실제 체결가가 아닙니다."} 아래 값은 1 BTC당 계산입니다. `;
  if(result.distance<=0 && !filled) {box.textContent=initial+"진입가가 손절의 잘못된 쪽에 있거나 같은 가격입니다. 유효한 손절 거리와 진입 손익비를 계산할 수 없습니다.";return;}
  if(filled && result.loss<=0) {box.textContent=initial+`손절선 청산 예상 순손익 ${fmt(-result.loss)} USDT, 목표 예상 순손익 ${fmt(result.reward)} USDT. 비용 가정상 본전·이익 보호라 손실 대비 R은 미산정입니다. 기존 손절 주문을 옮기는 지시도, 손실이 없다는 보장도 아닙니다.`;return;}
  const passed=result.rr>=math.rr;
  box.textContent=initial+`비용 후 손절 예상 손실 ${fmt(result.loss)} USDT, 목표 예상 순손익 ${fmt(result.reward)} USDT, 손익비 ${fmt(result.rr)} R / 최소 ${fmt(math.rr)} R → ${passed ? "손익비 계산만 충족" : "손익비 미달"}. `
    +(["multi","active"].includes(latest.settings.strategy_mode) ? `비용 비중 ${fmt(result.costFraction*100,1)}% / 한도 ${fmt(latest.settings.max_cost_fraction*100,0)}% → ${result.costFraction<=latest.settings.max_cost_fraction ? "비용 계산만 충족" : "비용 비중 초과"}. ` : "")
    +"실제 수량을 곱해야 포지션 손익이 됩니다. 이 입력은 주문·설정·보유 포지션을 변경하지 않습니다.";
}
