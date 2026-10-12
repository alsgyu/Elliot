"use strict";
let chartData = null, chartRequest = 0, chartFetched = 0, chartLoading = "", chartAttempt = "";
let chartCount = "auto", chartWindow = 360, chartAnchor = null, chartHover = null;
const chartStamp = ms => ms ? new Date(ms).toLocaleString(window.elliottLocaleTag(), {timeZone:"Asia/Seoul", year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false}) : "—";
const waveLabels = c => c?.pattern === "TRIANGLE_WATCH" ? ["0","A","B","C","D","E"] : ["ZIGZAG_COMPLETE","WAVE_C"].includes(c?.pattern) ? ["0","A","B","C"] : c?.pattern === "FLAT_OR_COMPLEX_WATCH" ? ["P0","P1","P2","P3"] : ["0","1","2","3","4","5"];
const isReferenceChart = () => activeTf === "15m" || (activeTf === "30m" && latest?.settings.strategy_mode !== "active");

function chartSnapshot() {
  if (!latest) return {candles:[], frame:null};
  if (chartData?.timeframe === activeTf && chartData.mode === latest.mode && chartData.sample === latest.sample) {
    return {...chartData, frame:isReferenceChart() ? chartData.frame : latest.decision?.frames[activeTf], analysis_asof:isReferenceChart() ? chartData.analysis_asof : latest.decision?.asof};
  }
  return {candles:latest.candles[activeTf] || [], frame:latest.decision?.frames[activeTf], interval:{"15m":900000,"30m":1800000,"1h":3600000,"4h":14400000,"1d":86400000,"1w":604800000}[activeTf], analysis_asof:latest.decision?.asof, role:"", swings:[]};
}
function chartFrame() { return chartSnapshot().frame; }
function visibleCandidate() {
  const frame = chartFrame(), cs = frame?.candidates || [];
  return cs.find(c => c.id === chartCount) || cs.find(c => c.id === frame?.selected) || cs[0];
}
async function loadChart(force = false) {
  if (!latest || activeView !== "analysis") return;
  const tf = activeTf, mode = latest.mode, sample = latest.sample;
  const key = `${mode}:${sample}:${tf}:${latest.decision?.id}`;
  if (chartLoading === key || (!force && key === chartAttempt && Date.now() - chartFetched < 15000)) return;
  chartLoading = chartAttempt = key; chartFetched = Date.now();
  const sequence = ++chartRequest;
  try {
    const data = await api(`/api/chart?timeframe=${encodeURIComponent(tf)}`);
    if (sequence !== chartRequest || tf !== activeTf || mode !== latest.mode || sample !== latest.sample) return;
    chartData = data;
  } catch (error) {
    if (sequence !== chartRequest || tf !== activeTf || mode !== latest.mode || sample !== latest.sample) return;
    const previous = chartSnapshot();
    chartData = {...previous, timeframe:tf, mode, sample, warning:`확장 차트 연결 실패 · ${error.message}`, fetched_at:previous.fetched_at};
  } finally {
    if (sequence === chartRequest) {
      chartLoading = "";
      renderCandidates(); renderWaveEvidence(); draw(); lucide.createIcons();
    }
  }
}
function renderCountPicker() {
  const frame = chartFrame(), cs = frame?.candidates || [];
  if (chartCount !== "auto" && !cs.some(c => c.id === chartCount)) chartCount = "auto";
  const options = [new Option(frame?.selected ? "자동 · 우선 카운트" : "자동 · 첫 대안 (우선 없음)", "auto")];
  for (const [i, c] of cs.entries()) options.push(new Option(`${i+1}. ${names[c.pattern]} · ${c.direction_name} · ${fmt(c.scale*100,2)}%`, c.id));
  const signature=JSON.stringify(options.map(o=>[o.value,o.textContent]));
  if($("chart-count").dataset.options!==signature){$("chart-count").replaceChildren(...options);$("chart-count").dataset.options=signature;}
  $("chart-count").value = chartCount;
}
function renderWaveEvidence() {
  const data = chartSnapshot(), c = visibleCandidate(), frame = data.frame;
  renderCountPicker();
  renderChartGuide();
  const active=latest.settings.strategy_mode==="active";
  $("wave-status").textContent = activeTf === "15m" ? (active?"15분봉 · 하위 확인 전용":"15분봉 · 참고 전용")
    : activeTf === "30m" && !active ? (latest.settings.strategy_mode === "multi" ? "30분봉 · 하위 확인 전용" : "30분봉 · 참고 전용") : `${tfNames[activeTf]} · ${states[frame?.state] || "분석 대기"}`;
  $("wave-metrics").replaceChildren(); $("wave-rules").replaceChildren(); $("wave-subdivisions").replaceChildren();
  const metric = (name, value) => { const box=document.createElement("div"), dt=document.createElement("dt"), dd=document.createElement("dd"); dt.textContent=name; dd.textContent=value; box.append(dt,dd); $("wave-metrics").append(box); };
  metric("카운트 분석 기준", chartStamp(data.analysis_asof));
  metric("차트 최근 마감", chartStamp(data.candles.at(-1)?.end));
  metric("진행 중 봉", data.forming ? `${chartStamp(data.forming.time)} · 미확정` : "없음 / 미수신");
  if (!c) {
    $("wave-context").textContent = "채택할 수 있는 파동 후보 없음 · 회색 스윙은 엘리엇 확정 카운트가 아닙니다.";
    empty("wave-pivots", "확정 피벗 후보 없음", 4); return;
  }
  const points=c.pivots, last=points.at(-1), labels=waveLabels(c), proven=c.subdivisions.filter(s=>s.confirmed).length;
  metric("관측된 파동 방향", c.observed_direction_name || "미확정");
  metric("이후 예상 방향", c.direction_name);
  metric("반전 확인 기준", `${fmt(c.scale*100,2)}%`);
  metric("내부 분할 확인", c.subdivisions.length ? `${proven} / ${c.subdivisions.length}` : "미검증");
  metric("최종 피벗 확인", chartStamp(last.confirmed_at));
  metric("카운트 무효화", c.invalidation > 0 ? fmt(c.invalidation) : "미산정");
  metric("후보 손절", c.stop > 0 ? fmt(c.stop) : "미산정");
  metric("후보 목표", c.target > 0 ? fmt(c.target) : "미산정");
  const length = i => Math.abs(points[i].price-points[i-1].price);
  if (["WAVE_3","WAVE_5","IMPULSE_COMPLETE"].includes(c.pattern)) {
    metric("2파 / 1파 되돌림", `${fmt(length(2)/length(1)*100,1)}%`);
    if(points.length>=5){metric("3파 / 1파 길이",`${fmt(length(3)/length(1),3)}×`);metric("4파 / 3파 되돌림",`${fmt(length(4)/length(3)*100,1)}%`);}
  } else if(c.pattern==="ZIGZAG_COMPLETE") metric("C파 / A파 길이",`${fmt(length(3)/length(1),3)}×`);
  else if(c.pattern==="WAVE_C") metric("B파 / A파 되돌림",`${fmt(length(2)/length(1)*100,1)}%`);
  const status = frame?.selected === c.id ? "우선 후보" : "대안 후보 · 최종 선택 아님";
  const researchSelected=latest.settings.research_mode && latest.decision?.selection?.id===c.id;
  const eligibility = isReferenceChart() ? "독립 진입 후보 아님" : researchSelected ? "모의 연구 신호 · 내부 구조 확인 여부는 별도 표시" : c.eligible ? "구조 자격 충족 · 실제 진입은 전체 조건 별도 검사" : "자동 진입 구조 자격 미충족";
  const elapsed = Math.max(0, Math.floor(((data.candles.at(-1)?.end || last.confirmed_at)-last.confirmed_at)/data.interval));
  $("wave-context").textContent = `${names[c.pattern]} · ${status} · 적합도 ${fmt(c.score,1)}/100 (승률 아님) · ${eligibility}. 마지막 피벗 확인 후 ${elapsed}봉. 가격 기준은 표시 후보의 값이며 실제 주문이 아닙니다.`;
  $("wave-rules").replaceChildren(...c.rules.map(r=>{const li=document.createElement("li");li.className=r.passed?"positive":"negative";li.textContent=`${r.passed?"통과":"미확인"} · ${r.id} · ${r.detail}`;return li;}));
  $("wave-subdivisions").replaceChildren(...c.subdivisions.map((s,i)=>{const el=document.createElement("p");el.className="subdivision";el.textContent=`${labels[i]}→${labels[i+1]} · 기대 ${s.expected}개 / 관측 ${s.observed_legs}개 · ${s.coverage_missing?"하위 이력 부족":s.confirmed?"구조 확인":"구조 미확인"}`;return el;}));
  $("wave-pivots").replaceChildren(...points.map((p,i)=>{const tr=document.createElement("tr");textCell(tr,labels[i]);textCell(tr,fmt(p.price));textCell(tr,chartStamp(p.time));textCell(tr,chartStamp(p.confirmed_at));return tr;}));
}
function chartBars() {
  const data=chartSnapshot(), all=[...data.candles];
  if(data.forming) all.push({...data.forming, forming:true});
  return all;
}
function chartSlice() {
  const all=chartBars(), size=chartWindow===1500?all.length:Math.min(chartWindow,all.length), max=Math.max(0,all.length-size);
  let start=chartAnchor == null ? max : all.findIndex(b=>b.time>=chartAnchor);
  start=Math.max(0,Math.min(max,start<0?max:start));
  return {all,bars:all.slice(start,start+size),start,max};
}
function draw() {
  if(!latest || activeView!=="analysis")return;
  const ctx=context($("chart"));if(!ctx)return;
  const {c,w,h}=ctx, data=chartSnapshot(), candidate=visibleCandidate(), {bars,start,max}=chartSlice();
  $("chart-window").max=max;$("chart-window").value=start;$("chart-window").disabled=!max;
  $("chart-zoom-in").disabled=chartWindow<=120;$("chart-zoom-out").disabled=chartWindow>=1500;
  $("chart-fit").disabled=!candidate;$("chart-latest").disabled=chartAnchor===null;
  const age = data.fetched_at ? Date.now()/1000-data.fetched_at : null;
  const stale = age != null && age>45;
  const warning = data.warning || (stale ? `차트 수신 지연 · 마지막 수신 ${chartStamp(data.fetched_at*1000)}` : "");
  $("chart-warning").hidden=!warning;$("chart-warning").textContent=warning;
  $("chart-role").textContent = `${data.role || ""}${data.source ? ` · ${data.source}` : ""}`;
  $("chart-start").textContent=bars.length?new Date(bars[0].time).toLocaleDateString(window.elliottLocaleTag(),{timeZone:"Asia/Seoul"}):"—";
  $("chart-end").textContent=bars.length?new Date(bars.at(-1).time).toLocaleDateString(window.elliottLocaleTag(),{timeZone:"Asia/Seoul"}):"—";
  if(!bars.length){c.fillStyle="#718078";c.font="12px sans-serif";c.fillText(t("표시할 차트 데이터 없음"),12,h/2);$("chart-ohlc").textContent="OHLC 없음";$("bar-count").textContent="0봉";$("chart-caption").textContent="분석 대기";return;}
  const left=8,right=73,top=23,bottom=24,cw=Math.max(20,w-left-right),ch=h-top-bottom;
  const manualLevels=manualChartLevels(), manualPrices=manualLevels.map(l=>l.value);
  const lo=Math.min(...bars.map(b=>b.low),...manualPrices),hi=Math.max(...bars.map(b=>b.high),...manualPrices),padding=(hi-lo)*.12 || 1;
  const y=v=>top+ch*(1-(v-lo+padding)/(hi-lo+2*padding));
  const span=bars.at(-1).time-bars[0].time+data.interval;
  const x=t=>left+cw*(t-bars[0].time+data.interval/2)/span;
  c.font="10px -apple-system,sans-serif";c.textBaseline="middle";
  for(let i=0;i<5;i++){const value=hi+padding-(hi-lo+2*padding)*i/4,yy=y(value);c.strokeStyle="#e5ebe7";c.lineWidth=1;c.beginPath();c.moveTo(left,yy);c.lineTo(w-right,yy);c.stroke();c.fillStyle="#66776d";c.fillText(fmt(value,0),w-right+7,yy);}
  c.save();c.beginPath();c.rect(left,top,cw,ch);c.clip();
  const bw=Math.max(.7,Math.min(10,cw/bars.length*.65));
  for(const b of bars){c.strokeStyle=c.fillStyle=b.close>=b.open?"#258468":"#bf626b";c.lineWidth=1;c.beginPath();c.moveTo(x(b.time),y(b.high));c.lineTo(x(b.time),y(b.low));c.stroke();const body=[x(b.time)-bw/2,Math.min(y(b.open),y(b.close)),bw,Math.max(1,Math.abs(y(b.open)-y(b.close)))];if(b.forming){c.setLineDash([2,2]);c.strokeRect(...body);c.setLineDash([]);}else c.fillRect(...body);}
  const path=(points,color,width,dash=[])=>{c.strokeStyle=color;c.lineWidth=width;c.setLineDash(dash);c.beginPath();points.forEach((p,i)=>i?c.lineTo(x(p.time),y(p.price)):c.moveTo(x(p.time),y(p.price)));c.stroke();c.setLineDash([]);};
  const points=candidate?.pivots || data.swings || [];
  if($("show-subwaves").checked && candidate)for(const s of candidate.subdivisions)path(s.pivots,"#817caa",1,[3,3]);
  if(points.length)path(points,candidate?"#286ba3":"#8b9992",candidate?2:1);
  if($("show-tail").checked && points.length){const last=points.at(-1), tail=[last,...chartBars().filter(b=>b.time>last.time).map(b=>({time:b.time,price:b.close}))];path(tail,"#b18023",1.3,[4,4]);}
  const labels=waveLabels(candidate), labelBoxes=[];
  for(const [i,p] of points.entries()){
    if(p.time<bars[0].time || p.time>bars.at(-1).time)continue;
    c.fillStyle=candidate?"#286ba3":"#8b9992";c.beginPath();c.arc(x(p.time),y(p.price),candidate?3:2,0,Math.PI*2);c.fill();
    if(!candidate)continue;
    const label=labels[i] || "?", xx=Math.max(left+4,Math.min(w-right-18,x(p.time)-3)), preferred=y(p.price)+(p.kind==="H"?-14:15);
    c.font="bold 11px sans-serif";
    const width=c.measureText(label).width+6;
    let yy=Math.max(top+9,Math.min(h-bottom-9,preferred));
    for(const offset of [0,-16,16,-32,32,-48,48]){const next=Math.max(top+9,Math.min(h-bottom-9,preferred+offset));if(!labelBoxes.some(b=>xx-3<b.x+b.w && xx+width>b.x && Math.abs(next-b.y)<16)){yy=next;break;}}
    labelBoxes.push({x:xx-3,y:yy,w:width});
    if(Math.abs(yy-preferred)>8){c.strokeStyle="#286ba380";c.lineWidth=.7;c.beginPath();c.moveTo(x(p.time),y(p.price));c.lineTo(xx+2,yy);c.stroke();}
    c.fillStyle="#ffffffeb";c.fillRect(xx-3,yy-7,width,14);c.fillStyle="#225d8f";c.fillText(label,xx,yy);
  }
  const levels=[...manualLevels];
  if($("show-levels").checked && candidate)for(const [name,value,color] of [[t("무효화"),candidate.invalidation,"#8c6388"],[t("후보 손절"),candidate.stop,"#b55965"],[t("후보 목표"),candidate.target,"#267c69"]])if(value>0 && !manualLevels.some(l=>Math.abs(l.value-guideRound(value,Number(latest.quote?.rules?.tick)||.01,candidate.direction<0))<1e-8))levels.push({name,value,color});
  if($("show-fib").checked && candidate && ["WAVE_3","WAVE_5","IMPULSE_COMPLETE","ZIGZAG_COMPLETE"].includes(candidate.pattern)){
    const ps=candidate.pivots, base=candidate.pattern==="WAVE_5"?2:0, a=ps[base],b=ps[base+1];
    for(const ratio of [.382,.5,.618,.786])levels.push({name:`${base===2?t("3파"):t("첫 다리")} ${fmt(ratio*100,1)}%`,value:b.price-(b.price-a.price)*ratio,color:"#788aaa"});
  }
  let labelY=top-14;
  for(const level of levels.filter(l=>y(l.value)>=top && y(l.value)<=h-bottom).sort((a,b)=>y(a.value)-y(b.value))){const yy=y(level.value);path([{time:bars[0].time-data.interval,price:level.value},{time:bars.at(-1).time+data.interval,price:level.value}],level.color,1,[5,5]);const text=`${level.name} ${fmt(level.value,level.decimals || 0)}`;c.font="10px sans-serif";labelY=Math.max(yy-6,labelY+14);if(labelY>h-bottom-6)continue;c.fillStyle="#fffffff0";c.fillRect(left+2,labelY-7,c.measureText(text).width+8,14);c.fillStyle=level.color;c.fillText(text,left+6,labelY);}
  let picked=bars.at(-1);
  if(chartHover!=null){const index=Math.max(0,Math.min(bars.length-1,Math.floor((chartHover-left)/cw*bars.length)));picked=bars[index];c.strokeStyle="#697b85";c.lineWidth=1;c.setLineDash([3,3]);c.beginPath();c.moveTo(x(picked.time),top);c.lineTo(x(picked.time),h-bottom);c.moveTo(left,y(picked.close));c.lineTo(w-right,y(picked.close));c.stroke();c.setLineDash([]);}
  c.restore();
  if(data.forming && bars.at(-1).forming){c.fillStyle="#986b16";c.font="10px sans-serif";const text=t("진행 중");c.fillText(text,Math.max(left,Math.min(w-right-38,x(data.forming.time)-20)),12);}
  c.fillStyle="#6d7c74";c.font="9px sans-serif";c.textBaseline="alphabetic";
  const firstLabel=new Date(bars[0].time).toLocaleDateString(window.elliottLocaleTag(),{timeZone:"Asia/Seoul"}),lastLabel=new Date(bars.at(-1).time).toLocaleDateString(window.elliottLocaleTag(),{timeZone:"Asia/Seoul"});
  c.fillText(firstLabel,left,h-3);if(cw>170)c.fillText(lastLabel,w-right-c.measureText(lastLabel).width,h-3);
  $("chart-ohlc").textContent=`${chartStamp(picked.time)} · ${picked.forming?t("진행 중"):t("마감")}   O ${fmt(picked.open)}   H ${fmt(picked.high)}   L ${fmt(picked.low)}   C ${fmt(picked.close)}   ${t("거래량")} ${fmt(picked.volume,1)}`;
  $("bar-count").textContent=`${tfNames[activeTf]} ${bars.length} / ${chartBars().length} ${t("bars")} · ${t(bars.at(-1).forming?"진행봉 포함":"마감봉만")}`;
  const hiddenPoints=candidate?.pivots.filter(p=>p.time<bars[0].time||p.time>bars.at(-1).time).length || 0;
  $("chart-caption").textContent=candidate?`${names[candidate.pattern]} · ${data.frame?.selected===candidate.id?"우선":"대안 · 최종 선택 아님"} · 확인된 끝점 ${candidate.pivots.length}개${hiddenPoints?` · 범위 밖 ${hiddenPoints}개`:""}`:"미확정 구조 · 번호 없는 스윙";
}
function setChartWindow(value){chartWindow=value;chartHover=null;$("chart-range").value=String(value);draw();}
function setupChart(){
  for(const id of ["guide-entry","manual-quantity"])$(id).addEventListener("input",()=>{renderChartGuide();draw();});
  for(const el of document.querySelectorAll('[name="manual-direction"]'))el.addEventListener("change",()=>{renderChartGuide();draw();});
  for(const el of document.querySelectorAll('[name="manual-entry-kind"]'))el.addEventListener("change",()=>{resetManualInputs();renderChartGuide();draw();});
  $("chart-range").addEventListener("change",e=>setChartWindow(Number(e.target.value)));
  $("chart-count").addEventListener("change",e=>{chartCount=e.target.value;renderCandidates();renderWaveEvidence();draw();lucide.createIcons();});
  for(const id of ["show-tail","show-levels","show-fib","show-subwaves","show-manual-plan"])$(id).addEventListener("change",draw);
  $("chart-window").addEventListener("input",e=>{const {all,max}=chartSlice(),index=Number(e.target.value);chartAnchor=index>=max?null:all[index]?.time;chartHover=null;draw();});
  $("chart-latest").addEventListener("click",()=>{chartAnchor=null;chartHover=null;draw();});
  $("chart-fit").addEventListener("click",()=>{const c=visibleCandidate(),all=chartBars();if(!c)return;const index=all.findIndex(b=>b.time>=c.pivots[0].time),need=all.length-Math.max(0,index)+10;chartAnchor=null;setChartWindow([120,360,720,1500].find(n=>n>=need)||1500);});
  $("chart-zoom-in").addEventListener("click",()=>setChartWindow([720,360,120].find(n=>n<chartWindow)||120));
  $("chart-zoom-out").addEventListener("click",()=>setChartWindow([360,720,1500].find(n=>n>chartWindow)||1500));
  $("chart").addEventListener("pointermove",e=>{chartHover=e.clientX-e.currentTarget.getBoundingClientRect().left;draw();});
  $("chart").addEventListener("pointerleave",()=>{chartHover=null;draw();});
  $("chart").addEventListener("keydown",e=>{if(!["ArrowLeft","ArrowRight","Home","End"].includes(e.key))return;e.preventDefault();const{all,start,max}=chartSlice();const index=e.key==="Home"?0:e.key==="End"?max:Math.max(0,Math.min(max,start+(e.key==="ArrowLeft"?-1:1)*Math.max(1,Math.floor(chartWindow/5))));chartAnchor=index>=max?null:all[index]?.time;chartHover=null;draw();});
}
