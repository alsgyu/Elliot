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
  if (activeTf === "15m") role = active ? "15분봉은 30분봉의 내부 구조·새 종가 돌파 확인용이며 독립 진입하지 않습니다." : "15분봉은 참고 전용이며 현재 전략의 주문 판단에는 쓰지 않습니다.";
  else if (activeTf === "30m") role = active ? "단기 적극형에서 30분봉은 진입 후보입니다. 15분봉 구조·새 종가 돌파, 비용·손절 거리·위험 한도를 통과해야 합니다." : scenarioMode ? "30분봉은 1시간봉의 구조·돌파 확인용이며 독립 진입하지 않습니다." : "30분봉은 참고 전용이며 현재 전략의 주문 판단에는 쓰지 않습니다.";
  else if (["1w","1d"].includes(activeTf)) role = "이 시간봉은 상위 파동의 맥락 확인용이며 단독 주문 신호가 아닙니다.";
  else if (activeTf === "1h" && !scenarioMode) role = "현재 엄격 전략에서 1시간봉은 4시간봉의 진입을 확인하는 용도입니다.";
  else role = "구조, 상위 방향, 하위 종가 돌파, 비용과 위험 한도를 모두 통과해야 진입을 검토합니다.";
  if(settings.research_mode)role="모의 연구: 최소 1개 내부 분할과 가격 규칙, 신규 돌파 또는 조정 끝 확인 종가, 비용·위험 한도를 검사합니다. 실거래로 전송하지 않습니다.";
  const qualification = !c ? "파동 후보 없음." : settings.research_mode && selected ? "모의 연구 후보입니다. 잠정 카운트는 완전한 5-3 구조 확인과 다릅니다." : !c.eligible ? "이 후보는 단독 자동 진입 대상이 아닙니다." : "구조 자격 충족은 주문 허가와 다릅니다.";
  const signal = selected && ["LONG","SHORT"].includes(d?.action) ? "표시 후보가 최종 신호에 선택됐지만 체결 완료를 뜻하지 않습니다." : "표시 후보의 최종 진입 신호는 아직 없습니다.";
  const operation = latest?.error || latest?.entry_gate || (latest?.account?.running ? "자동매매 가동 중" : "신규 진입 중단 상태");
  $("guide-state").textContent = `${stale ? "자료 지연 또는 분석 시점 불일치 가능: 아래 가격은 과거 참고값입니다. " : ""}${qualification} ${role} ${signal} 전체 결정: ${d?.action || "WAIT"} · ${d?.reason || "분석 대기"}. 실행 상태: ${operation}.`;
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
      [`손절 거리만 본 하한 · ${stop} 초과`,"롱의 진입가는 손절보다 높아야 합니다. 이 바로 위가 안전한 매수 가격이라는 뜻은 아닙니다. 너무 가까우면 거래 비용 비중이 커집니다."],
      [`손익비만 본 상한 · ${bound} 이하`,`가정 비용을 반영해 이보다 높게 진입하면 목표가 그대로일 때 최소 ${fmt(math.rr,2)} R을 만족하지 못합니다. 목표까지 아직 여유가 있어도 추격 가격일 수 있습니다.`]
    ] : [
      [`손익비만 본 하한 · ${bound} 이상`,`숏은 더 낮게 추격할수록 목표까지 남은 이익이 줄어듭니다. 이보다 낮은 진입가는 가정 비용 후 최소 ${fmt(math.rr,2)} R 미달입니다.`],
      [`손절 거리만 본 상한 · ${stop} 미만`,"숏의 진입가는 손절보다 낮아야 합니다. 이 바로 아래가 안전한 매도 가격이라는 뜻은 아니며, 너무 가까우면 비용 비중이 커집니다."]
    ] : [["손익비 참고 구간 없음","이 손절·목표와 비용 가정으로 최소 손익비를 만족하는 진입 가격이 없습니다."]]);
    $("guide-costs").textContent = `${fmt(math.rr,2)} R은 손절 시 예상 손실 1에 대해 목표 시 예상 순이익이 ${fmt(math.rr,2)}라는 비율이지 승률이 아닙니다. 설정된 편도 수수료 ${fmt(settings.fee_bps,1)} bps와 슬리피지 ${fmt(settings.slippage_bps,1)} bps를 진입·청산에 반영한 예시입니다. 1 bps = 0.01%. ${math.tick ? `손절 ${priceText(math.stop)}, 목표 ${priceText(math.target)}는 호가 단위로 보정했고 진입 경계도 안쪽으로 맞췄습니다.` : "호가 단위 미수신: 경계는 소수 둘째 자리 안쪽으로 표시합니다."} 실제 계정 수수료가 더 높으면 실행 범위는 좁아질 수 있습니다. 펀딩비와 급변 비용은 이 계산에 포함하지 않습니다.`;
  } else {
    guideRows("guide-bounds", [["진입 가격 미산정","방향·손절·목표 또는 비용 설정이 부족하거나 유효하지 않습니다. 임의의 매수·매도 범위를 만들지 않습니다."]]);
    $("guide-costs").textContent = "손익비 R은 목표 체결 시 예상 순이익을 손절 체결 시 예상 순손실로 나눈 비율이며 성공 확률이 아닙니다.";
  }
  if (trigger?.price>0) {
    const tf = trigger.timeframe || "1h";
    const expires = scenarioMode && trigger.bar_end ? trigger.bar_end + ({"15m":900000,"30m":1800000,"1h":3600000}[tf] || 0) : null;
    const expired = expires && (latest.sample ? Number(latest.quote?.time) : Date.now()) >= expires;
    $("guide-trigger").textContent = `${tfNames[tf] || tf} 종가 돌파 기준 ${fmt(trigger.price)}: 마감 종가가 이 가격 ${long ? "위" : "아래"}여야 합니다. 이 선은 지정가 주문 가격이나 호가의 직접 ${long ? "하한" : "상한"}이 아닙니다. 실제 진입 기준은 조건 통과 당시 ${long ? "매도호가(ask)" : "매수호가(bid)"}이고 시장가 체결가는 달라질 수 있습니다.${scenarioMode ? ` 다중·단기 전략은 새 돌파, 신호 만료, 신호 종가 대비 ${fmt(settings.max_entry_drift_r,2)} R 추격 거리도 따로 검사합니다.` : ""}${expires ? ` 신호 만료 ${chartStamp(expires)} KST${expired ? " · 이미 만료되어 새 신호가 필요합니다." : " · 이 시각 전이라도 다른 조건을 모두 통과해야 합니다."}` : ""}`;
  } else $("guide-trigger").textContent = "현재 표시 후보와 연결된 종가 돌파 기준이 아직 없습니다. 따라서 실제 진입의 돌파 하한·상한은 미확정입니다. 위의 가격 계산만으로 지금 매수·매도할 수 있다고 해석하지 마세요.";
  $("guide-range-caution").textContent = `위 범위는 '손절 거리 + 손익비'만의 진입 전 참고 구간이며 주문 가능 구간이 아닙니다. ${filledMode?"이미 체결 모드의 수동 포지션 계산과 달리, 위 상한·하한은 진입 슬리피지도 포함한 가정입니다. ":""}${scenarioMode ? `비용 비중 한도 ${fmt(Number(settings.max_cost_fraction)*100,0)}%, 신호 만료와 추격 제한도 별도입니다. ` : ""}${active?`단기 전략의 구조 손절 거리 상한은 ${fmt(settings.max_stop_percent)}%이며 초과 시 진입을 제외합니다. `:""}방향 허용, 내부 파동, 시간봉 충돌, 계정 권한, 스프레드, 수량·위험 한도를 통과해야 하며, 레버리지를 높여도 이 파동의 손절·목표가 유리하게 바뀌지는 않습니다.`;
  renderGuideCalculation(math,stale);
  renderManualReview(c,data,stale);
  const steps = c && math ? [
    `먼저 지금 보는 ${tfNames[activeTf]}의 표시 후보가 무엇인지 확인합니다. 관측 ${c.observed_direction_name || "미확정"}은 이미 그려진 파동의 방향이고 예상 ${c.direction_name}은 그 뒤의 가정입니다. 다른 후보의 손절·목표와 섞지 않습니다.`,
    `${long ? "가격이 내려와" : "가격이 올라가"} 후보 손절 ${fmt(c.stop)}에 닿으면 이 끝점에서 시작한다는 매매 가정을 재검토합니다. ${c.stop===c.invalidation ? "이 후보는 무효화 기준도 같은 가격입니다." : `별도의 무효화 ${fmt(c.invalidation)}는 위에서 설명한 더 큰 카운트의 경계입니다. 손절을 그쪽으로 임의로 넓히라는 뜻이 아닙니다.`} 파동 엔진은 마감봉의 고가·저가 침범도 보므로 종가만 회복했다고 기존 후보가 유지되는 것은 아닙니다.`,
    `손절과 목표 사이에 있다는 사실만으로 진입 조건이 충족되지는 않습니다. ${long ? "상한보다 비싸게 사면" : "하한보다 낮게 숏을 잡으면"} 손익비가 부족할 수 있고, 손절에 너무 가까우면 비용 비중이 커집니다. 하위 봉의 구조와 종가 돌파가 먼저 필요합니다.`,
    `목표 ${fmt(c.target)}에 도달해도 여기서 반드시 반전하거나 다음 반대 포지션을 잡아야 한다는 뜻은 아닙니다. 후보 목표는 예상 지점이고 실제 익절은 보유 포지션의 주문·체결 기록으로 확인해야 합니다.`
  ] : ["현재는 유효한 가격 기준이 없어 기다리는 상태입니다. 번호 없는 스윙이나 대안 이름만으로 진입가를 만들지 않습니다.","파란 피벗 연결은 관측된 움직임, 금색 점선은 이후의 미확정 가격 경로입니다. 어느 선도 수익이나 미래 방향을 보장하지 않습니다."];
  $("guide-steps").replaceChildren(...steps.map(text=>{const li=document.createElement("li");li.textContent=text;return li;}));
}

function renderGuideCalculation(math,stale) {
  const box=$("guide-calculation"), input=$("guide-entry");
  const direction=manualDirection(), filled=document.querySelector('[name="manual-entry-kind"]:checked').value==="filled";
  input.placeholder=filled?"평균 체결가 입력":"가정 진입가 입력";
  if (!math) {input.placeholder="계산할 후보 없음";box.textContent="진입가 예시 계산 대기";return;}
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
