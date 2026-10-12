"use strict";

// Ratios are fixed illustration policies, not fitted probabilities or order instructions.
function manualTargets(candidate, tick) {
  const c=candidate, ps=c?.pivots || [], d=c?.direction, rows=[];
  if (![1,-1].includes(d) || !ps.every(p=>Number.isFinite(p.price) && p.price>0)) return [];
  const price=i=>ps[i].price, length=i=>Math.abs(price(i)-price(i-1));
  const sign=d>0 ? "+" : "−";
  const fifthCap=c.pattern==="WAVE_5" && ps.length===5 && length(3)<length(1) ? length(3) : null;
  const add=(name, value, formula, primary=false)=>{
    if (!Number.isFinite(value) || value<=0) return;
    if(fifthCap!=null && (value-price(4))*d>fifthCap+1e-8)return;
    const rounded=tick ? guideRound(value,tick,d<0) : value;
    const existing=rows.find(r=>Math.abs(r.price-rounded)<1e-8);
    if(existing){existing.primary ||= primary;existing.name+=` / ${name}`;return;}
    rows.push({name,price:rounded,formula,primary});
  };
  if(c.pattern==="WAVE_3" && ps.length===3) {
    for(const ratio of [1,1.618,2.618])add(`${ratio}배 ${ratio===2.618?"연장 가정":"투영"}`,price(2)+d*length(1)*ratio,
      `2파 끝 ${fmt(price(2))} ${sign} 1파 길이 ${fmt(length(1))} × ${ratio}`);
  } else if(c.pattern==="WAVE_5" && ps.length===5) {
    for(const ratio of [.618,1]) {
      const projected=length(1)*ratio;
      if(length(3)<length(1) && projected>length(3)+1e-8)continue;
      add(`${ratio}배 투영`,price(4)+d*projected,`4파 끝 ${fmt(price(4))} ${sign} 1파 길이 ${fmt(length(1))} × ${ratio}`);
    }
  } else if(c.pattern==="WAVE_C" && ps.length===3) {
    for(const ratio of [.618,1,1.618])add(`A파 ${ratio}배 투영`,price(2)+d*length(1)*ratio,
      `B파 끝 ${fmt(price(2))} ${sign} A파 길이 ${fmt(length(1))} × ${ratio}`);
  } else if(["IMPULSE_COMPLETE","ZIGZAG_COMPLETE"].includes(c.pattern)
      && ps.length===(c.pattern==="IMPULSE_COMPLETE"?6:4)) {
    const end=ps.at(-1).price, span=Math.abs(end-price(0));
    for(const ratio of [.382,.5,.618])add(`${ratio*100}% 되돌림${ratio===.5?" (관행 비율)":""}`,end+d*span*ratio,
      `완료 가설 끝점 ${fmt(end)} ${sign} 전체 순이동 ${fmt(span)} × ${ratio}`);
  } else return [];
  add("기존 파동 목표",c.target,"자동 엔진의 기존 후보 목표와 같은 기준. 최적값으로 검증된 것은 아닙니다.",true);
  return rows.filter(r=>(r.price-c.stop)*d>0).sort((a,b)=>(a.price-b.price)*d)
    .map((r,i)=>({...r,label:`T${i+1}`}));
}

function manualEvaluate(candidate, entry, options, settings, rules, context={}) {
  const math=guideMath(candidate,settings,rules);
  const result={entry,math,targets:[],issues:[],warnings:[],showLines:false};
  const fail=text=>({...result,error:text});
  if(!math)return fail("방향과 손절·목표가 확인된 파동 후보가 필요합니다.");
  const direction=Number(options.direction);
  if(![1,-1].includes(direction))return fail("내 포지션 방향을 LONG(롱) 또는 SHORT(숏)으로 직접 선택해야 합니다. 체결가나 파동 후보로 보유 방향을 추정하지 않습니다.");
  if(direction!==candidate.direction)return fail(`입력 ${direction>0?"LONG(롱)":"SHORT(숏)"}과 참고 파동의 예상 ${candidate.direction>0?"LONG(롱)":"SHORT(숏)"}은 반대입니다. 같은 방향의 파동 후보가 필요하며 손절·목표를 반대로 뒤집지 않습니다.`);
  if(!Number.isFinite(entry) || entry<=0)return fail("양수인 진입가를 입력해야 수동 검토가 가능합니다.");
  const quantity=options.quantity==null ? 1 : Number(options.quantity);
  if(!Number.isFinite(quantity) || quantity<=0)return fail("수량은 양수인 BTC 단위여야 합니다. 빈칸은 1 BTC당 계산입니다.");
  const levels=manualTargets(candidate,math.tick);
  if(!levels.length)return fail("이 관망 구조에는 검증할 피보나치 목표 정책이 없습니다.");
  const filled=options.filled===true, d=math.direction;
  const last=candidate.pivots.at(-1), candles=context.candles || [];
  const bars=[...candles,...(context.forming?[{...context.forming,forming:true}]:[])];
  const tail=bars.filter(b=>b.time>last.confirmed_at);
  const current=Number(bars.at(-1)?.close);
  const covered=!!candles.length && candles[0].time<=last.confirmed_at && candles.at(-1).end>=last.confirmed_at;
  const crossed=(bar,value,stop=false)=>stop ? (d>0?bar.low<=value:bar.high>=value) : (d>0?bar.high>=value:bar.low<=value);
  const stopTouch=tail.find(b=>crossed(b,math.stop,true));
  const currentStop=Number.isFinite(current) && (current-math.stop)*d<=0;
  const base=guidePayoff(entry,math.stop,math.target,d,math.fee,math.slip,filled);
  if(!Number.isFinite(base.loss*quantity) || levels.some(t=>!Number.isFinite(guidePayoff(entry,math.stop,t.price,d,math.fee,math.slip,filled).reward*quantity)))return fail("입력 가격·수량이 계산 가능한 범위를 넘었습니다.");
  const modeledEntry=filled ? entry : entry*(1+d*math.slip);
  const breakEven=d>0 ? modeledEntry*(1+math.fee)/((1-math.fee)*(1-math.slip))
    : modeledEntry*(1-math.fee)/((1+math.fee)*(1+math.slip));
  Object.assign(result,{direction,quantity,perUnit:options.quantity==null,filled,stop:math.stop,
    invalidation:candidate.invalidation,breakEven,loss:base.loss*quantity,distance:base.distance,
    ageBars:Number.isFinite(context.interval) && context.interval>0
      ? Math.max(0,Math.floor(((context.analysis_asof || candles.at(-1)?.end || last.confirmed_at)-last.confirmed_at)/context.interval)) : null});
  if(context.stale)result.issues.push("자료가 지연되어 현재 가격 기준 판단을 보류합니다.");
  if(!Number.isFinite(current) || current<=0)result.issues.push("현재 차트 가격 자료가 없어 손절·목표의 현재 위치를 확인하지 못했습니다.");
  if(!filled && (base.distance<=0 || base.loss<=0))result.issues.push("진입가가 손절의 잘못된 쪽에 있거나 같은 가격입니다.");
  if(stopTouch || currentStop)result.issues.push("피벗 확인 뒤 손절 경계 침범이 관측됐습니다. 이 끝점의 진입 가정을 다시 검토해야 합니다.");
  if(!covered)result.warnings.push("피벗 확인 이후 전체 이력을 확보하지 못했습니다. 목표 접촉 여부는 조회된 구간만 검사했습니다.");
  const proof=candidate.subdivisions || [], proven=proof.filter(s=>s.confirmed).length;
  if(!proof.length || proven<proof.length)result.warnings.push(`${proof.length?`내부 파동 확인 ${proven}/${proof.length}`:"내부 파동 미검증"} · 미확인 해석의 수학적 가격 예시이며 매매 신호가 아닙니다.`);
  if(context.conflict)result.warnings.push("같은 시간봉에 경쟁 카운트가 충돌합니다. 표시 후보 하나의 방향을 확정된 전망으로 볼 수 없습니다.");
  if(context.opposed)result.warnings.push("다른 시간봉에 반대 방향 후보가 있습니다. 이번 계산은 그 충돌을 해결하거나 전체 시간봉의 합의로 간주하지 않습니다.");
  if(result.ageBars!=null)result.warnings.push(`마지막 피벗 확인 이후 ${result.ageBars}개 ${candidate.timeframe} 마감봉 경과. 최근 시세 수신 시각과 파동 기준점 시각은 다릅니다.`);
  if(filled)result.warnings.push("입력한 체결가에 진입 슬리피지를 다시 더하지 않았습니다. 수수료는 여전히 설정 가정이며, 손익비는 진입부터 청산까지의 총거래 예시이지 지금 유지·청산 중 어느 쪽이 유리한지의 비교가 아닙니다.");
  if(filled && base.loss<=0)result.warnings.push("손절선에서의 청산도 비용 가정상 본전·이익 보호에 해당합니다. 손실을 분모로 한 R은 미산정이며, 급변 시에도 손실이 없다는 보장은 아닙니다.");
  if(!math.tick)result.warnings.push("거래소 호가 단위 미수신: 계산값을 실제 주문 가격으로 그대로 사용할 수 없습니다.");
  if(candidate.pattern==="WAVE_5")result.warnings.push("5파 투영이 기존 3파 끝점을 넘지 않으면 중간 익절 참고값일 뿐 5파 완료를 뜻하지 않습니다. 3파가 1파보다 짧으면 5파 투영 길이를 3파 이하로 제한합니다.");
  if(candidate.pattern==="WAVE_C")result.warnings.push("C파 투영이 A파 끝점을 넘지 않으면 중간 익절 참고값이며 지그재그 C파 완료를 뜻하지 않습니다.");
  result.targets=levels.map(level=>{
    const p=guidePayoff(entry,math.stop,level.price,d,math.fee,math.slip,filled);
    const touch=tail.find(b=>crossed(b,level.price));
    const behind=(level.price-entry)*d<=0;
    const passedNow=Number.isFinite(current) && (current-level.price)*d>=0;
    let status="손익비 계산만 충족";
    if(behind)status="진입가 기준 이익 방향 아님";
    else if(passedNow)status="현재 차트 가격이 목표에 도달·통과";
    else if(touch)status="피벗 확인 뒤 도달 이력 · 진입 전후 불명";
    else if(!filled && base.distance<=0)status="손절 위치 오류";
    else if(p.reward<=0)status="비용 후 수익 없음";
    else if(filled && base.loss<=0)status="본전·이익 보호 가정 / R 미산정";
    else if(p.rr<math.rr)status="최소 손익비 미달";
    const pricePass=!behind && !passedNow && !touch && (filled || base.distance>0) && p.reward>0 && ((filled && base.loss<=0) || p.rr>=math.rr);
    return {...level,reward:p.reward*quantity,rr:(filled || base.distance>0) && base.loss>0?p.rr:null,
      status,touchedAt:touch?.time || null,touchedForming:!!touch?.forming,pricePass};
  });
  const primary=result.targets.find(t=>t.primary);
  if(!primary || !primary.pricePass)result.issues.push(`기본 목표 ${primary?.label || "미산정"}: ${primary?.status || "계산 불가"}. 먼 목표를 자동 선택해 손익비를 맞추지 않습니다.`);
  result.showLines=!context.stale && Number.isFinite(current) && current>0 && !stopTouch && !currentStop && (filled || base.distance>0)
    && result.targets.some(t=>(t.price-entry)*d>0);
  return result;
}

let manualReview=null;
function manualDirection() {
  const selected=document.querySelector('[name="manual-direction"]:checked');
  return selected ? Number(selected.value) : null;
}
function resetManualInputs(resetMode=false) {
  $("guide-entry").value="";$("manual-quantity").value="";
  for(const input of document.querySelectorAll('[name="manual-direction"]'))input.checked=false;
  if(resetMode)document.querySelector('[name="manual-entry-kind"][value="planned"]').checked=true;
  manualReview=null;
}
function renderManualReview(candidate,data,stale) {
  const input=$("guide-entry"), quantity=$("manual-quantity"), settings=latest.settings;
  const direction=manualDirection(), side=direction===1?"LONG(롱)":direction===-1?"SHORT(숏)":"방향 미선택";
  const filled=document.querySelector('[name="manual-entry-kind"]:checked').value==="filled";
  $("manual-context").textContent="직접 입력 · 거래소 포지션 조회 아님";
  $("manual-reference-name").textContent=candidate?`${tfNames[activeTf]} · ${names[candidate.pattern] || candidate.pattern}`:"후보 없음";
  $("manual-reference-side").textContent=candidate?.direction===1?"파동 예상 LONG(롱)":candidate?.direction===-1?"파동 예상 SHORT(숏)":"예상 방향 미확정";
  $("manual-side-note").textContent=direction===null?"방향 미선택 · 자동 추정 안 함":`${filled?"내 체결 포지션":"내 진입 가정"} · ${side}`;
  const price=input.value && !input.validity.badInput && Number(input.value)>0?`${fmt(Number(input.value))} USDT`:"입력 대기";
  $("manual-position-summary").textContent=`${filled?"입력한 체결 포지션":"진입 전 가정"}: ${side} · ${filled?"체결가":"진입가"} ${price}`;
  $("manual-profit-heading").textContent=quantity.value===""?"목표 순손익 · USDT/BTC":"목표 순손익 · USDT";
  manualReview=null;$("manual-metrics").replaceChildren();$("manual-warnings").replaceChildren();
  if(direction===null || !candidate || (!input.value && !input.validity.badInput && direction===candidate.direction)) {
    $("manual-summary").textContent=direction===null
      ? "방향 선택 대기 · 내 포지션의 LONG(롱) 또는 SHORT(숏)을 선택해야 합니다. 참고 파동의 예상 방향을 내 보유 방향으로 간주하지 않습니다."
      : !candidate?"검토 가능한 파동 후보가 없습니다. 임의의 손절·익절을 산정하지 않습니다."
      : `${filled?"실제 체결가":"가정 진입가"} 입력 대기 · ${side} 기준의 손절·익절을 검토합니다.`;
    empty("manual-targets","내 방향과 진입가, 같은 방향의 파동 후보가 있어야 계산합니다.",6);return;
  }
  const opposed=Object.entries(latest.decision?.frames || {}).some(([tf,f])=>tf!==activeTf && f.candidates?.some(c=>c.direction===-candidate.direction));
  const plan=manualEvaluate(candidate,input.validity.badInput?NaN:Number(input.value),{direction,filled,
    quantity:quantity.validity.badInput?NaN:quantity.value===""?null:Number(quantity.value)},settings,latest.quote?.rules,
    {...data,stale,conflict:data.frame?.state==="CONFLICT",opposed});
  manualReview={...plan,key:`${latest.mode}:${latest.sample}:${activeTf}:${candidate.id}`};
  if(plan.error){
    const matching=(data.frame?.candidates || []).filter(c=>c.direction===direction && guideMath(c,settings,latest.quote?.rules));
    const help=direction!==candidate.direction
      ? matching.length?` 현재 시간봉에 같은 방향 후보 ${matching.length}개가 있습니다. 표시 카운트에서 해당 방향을 선택한 뒤 다시 입력하세요.`
        : " 현재 시간봉에 같은 방향의 계산 가능한 후보가 없습니다. 다른 시간봉·새 카운트가 필요하며, 반대 파동으로 손절·익절을 만들지 않습니다."
      : "";
    $("manual-summary").textContent=`계산 보류 · ${plan.error}${help}`;
    empty("manual-targets",plan.error,6);return;
  }
  const scale=plan.perUnit?"1 BTC당":`${plan.quantity.toLocaleString(window.elliottLocaleTag(),{maximumFractionDigits:8})} BTC 기준`;
  $("manual-position-summary").textContent+=` · ${scale}`;
  const primary=plan.targets.find(t=>t.primary);
  $("manual-summary").textContent=plan.issues.length
    ? `기본 목표의 참고 채택 보류 · ${plan.issues.join(" ")}`
    : `기본 목표 ${primary.label} ${fmt(primary.price)}의 가격 조건만 충족합니다. 최적값이나 진입 신호가 아니며, 파동 근거와 아래 경고를 별도로 확인해야 합니다.`;
  const metric=(name,value)=>{const div=document.createElement("div"),dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=name;dd.textContent=value;div.append(dt,dd);$("manual-metrics").append(div);};
  metric("파동 끝점 기준 손절",`${fmt(plan.stop)} USDT`);
  metric("카운트 무효화 · 손절 확대 지시 아님",`${fmt(plan.invalidation)} USDT`);
  metric(`${plan.filled?"손절선 청산 예상 순손익":"손절 예상 손실"} · ${scale}`,plan.filled?`${fmt(-plan.loss)} USDT`:plan.distance>0?`${fmt(plan.loss)} USDT`:"손절 위치 오류");
  metric("비용 후 본전 · 청산 기준가격",`${fmt(plan.breakEven)} USDT`);
  $("manual-warnings").replaceChildren(...plan.warnings.map(text=>{const li=document.createElement("li");li.textContent=text;return li;}));
  $("manual-targets").replaceChildren(...plan.targets.map(t=>{
    const tr=document.createElement("tr");if(t.primary)tr.className="manual-base";
    textCell(tr,`${t.label}${t.primary?" · 기존 기본 목표":""}`);textCell(tr,fmt(t.price));
    textCell(tr,`${t.name}. ${t.formula}`);textCell(tr,fmt(t.reward));textCell(tr,t.rr==null?"미산정":`${fmt(t.rr)} R`);
    textCell(tr,`${t.status}${t.touchedAt?` (${chartStamp(t.touchedAt)} KST 봉${t.touchedForming?" · 진행 중":""}; 내 주문 체결 아님)`:""}`);
    return tr;
  }));
}

function manualChartLevels() {
  const c=visibleCandidate(), p=manualReview;
  if(!$("show-manual-plan").checked || !c || !p?.showLines || p.key!==`${latest.mode}:${latest.sample}:${activeTf}:${c.id}`)return [];
  return [{name:`${p.direction>0?"LONG":"SHORT"} ${t(p.filled?"체결가":"가정 진입")}`,value:p.entry,color:"#276caa",decimals:2},
    {name:t("수동 검토 손절"),value:p.stop,color:"#b55965",decimals:2},
    ...p.targets.map(t=>({name:`${window.t("검토")} ${t.label}${t.primary?` · ${window.t("기본")}`:""}${t.touchedAt?` · ${window.t("기접촉")}`:""}`,
      value:t.price,color:t.pricePass?"#267c69":"#7c8581",decimals:2}))];
}
