'use strict';
const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeUrl = value => /^https?:\/\//i.test(value || '') ? value : '';
let workbook = null, mode = 'file', view = 'search', targets = [], sourceUrls = {}, results = [], saved = [], jobId = '', running = false, filter = 'all', selectedRow = null, candidateIndex = -1, polling = null;
let toastTimer, websites=[];
function toast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 4000); }
function notice(text) { $('notice').textContent = text; $('notice').hidden = !text; }
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '요청을 처리하지 못했습니다.');
  return data;
}
function columnName(index) { let value=''; for (let n=index+1;n;n=Math.floor((n-1)/26)) value=String.fromCharCode(65+(n-1)%26)+value; return value; }
function sheetRows() { return workbook?.sheets[Number($('sheet').value)]?.rows || []; }
function populateColumns() {
  const rows=sheetRows(), header=Number($('header-row').value), width=Math.max(0,...rows.slice(0,50).map(r=>r.length));
  const labels=Array.from({length:width},(_,i)=>({value:i,label:`${columnName(i)} · ${header>0 ? rows[header-1]?.[i] || '제목 없음' : '열 '+columnName(i)}`}));
  for (const id of ['location-column','province-column','url-column']) {
    const old=$(id).value; $(id).innerHTML=(id==='location-column'?'':'<option value="-1">선택하지 않음</option>')+labels.map(x=>`<option value="${x.value}">${escape(x.label)}</option>`).join('');
    if (labels.some(x=>String(x.value)===old)) $(id).value=old;
  }
  const headers=header>0 ? rows[header-1] || [] : [];
  const location=headers.findIndex(x=>/지자체|자치단체|시군구|시·군·구|기관명|관할/.test(x));
  const province=headers.findIndex(x=>/^(시도|시·도|광역|도명)$/.test(x));
  const url=headers.findIndex(x=>/URL|홈페이지|주소.*웹/i.test(x));
  if (location>=0) $('location-column').value=location;
  if (province>=0) $('province-column').value=province;
  if (url>=0) $('url-column').value=url;
  updateTargets();
}
function updateTargets() {
  let raw=[]; sourceUrls={};
  if (mode==='paste') {
    for (const line of $('locations').value.split(/\r?\n/)) {
      const parts=line.split('\t').map(x=>x.trim()), url=parts.find(x=>safeUrl(x)), value=parts.filter(x=>x&&!safeUrl(x)).join(' ').replace(/\s+/g,' ').trim();
      if (value) { raw.push(value); if (url) sourceUrls[value]=url; }
    }
  }
  else if (workbook) {
    const location=Number($('location-column').value), province=Number($('province-column').value), url=Number($('url-column').value), start=Math.max(0,Number($('header-row').value));
    for (const row of sheetRows().slice(start)) {
      const name=String(row[location] || '').trim(), prefix=province>=0 ? String(row[province] || '').trim() : '';
      if (!name) continue;
      const value=(prefix && !name.startsWith(prefix) ? prefix+' '+name : name).replace(/\s+/g,' ').trim();
      raw.push(value); if (url>=0 && safeUrl(row[url])) sourceUrls[value]=row[url];
    }
  }
  targets=[...new Set(raw)];
  $('target-count').textContent=targets.length;
  $('duplicate-note').textContent=raw.length>targets.length ? `중복 ${raw.length-targets.length}개 제외` : '최대 100개 / 중복 자동 제외';
  $('preview').innerHTML=targets.length ? targets.slice(0,5).map(x=>`<span>${escape(x)}${sourceUrls[x]?' · 공식 URL':''}</span>`).join('')+(targets.length>5 ? `<span class="more">외 ${targets.length-5}개 지자체</span>`:'') : '<span>조회할 지자체 목록이 표시됩니다.</span>';
  $('search-start').disabled=running || !targets.length || targets.length>100;
  if (targets.length>100) notice('한 번에 최대 100개까지 조회할 수 있습니다. 목록을 나눠 주세요.');
}
function setMode(next) {
  mode=next; $('tab-file').classList.toggle('active',next==='file'); $('tab-paste').classList.toggle('active',next==='paste');
  $('file-pane').hidden=next!=='file'; $('paste-pane').hidden=next!=='paste'; updateTargets();
}
async function upload(file) {
  if (!file || running) return;
  if (file.size>10_000_000) { toast('파일은 10MB 이하로 준비해 주세요.'); return; }
  $('upload-label').textContent='파일 읽는 중…';
  try {
    const buffer=new Uint8Array(await file.arrayBuffer()); let binary='';
    for (let i=0;i<buffer.length;i+=8192) binary+=String.fromCharCode(...buffer.subarray(i,i+8192));
    workbook=await api('/api/upload',{filename:file.name,data:btoa(binary)});
    $('sheet').innerHTML=workbook.sheets.map((s,i)=>`<option value="${i}">${escape(s.name)}</option>`).join('');
    $('mapping').hidden=false; $('upload-label').textContent=file.name; setMode('file'); populateColumns(); notice('시트와 지자체 열을 확인한 뒤 조회를 시작하세요. 선택한 이름과 공식 URL만 조회에 사용합니다.');
  } catch (e) { $('upload-label').textContent='엑셀 파일 선택'; toast(e.message); }
}
function setView(next) {
  view=next; $('nav-search').classList.toggle('active',next==='search'); $('nav-saved').classList.toggle('active',next==='saved');
  $('page-title').textContent=next==='search' ? '지방소득세 담당 부서 찾기' : '확인한 공문 수신처';
  $('page-description').textContent=next==='search' ? '지자체 목록을 넣고, 공식 홈페이지의 업무안내와 조직도를 확인하세요.' : '직접 확인해 저장한 수신처입니다. 재사용하기 전 조회 시각과 조직 변경을 확인하세요.';
  $('result-label').textContent=next==='search'?'조회 결과':'저장한 수신처';
  render();
}
function visibleResults() {
  const query=$('result-search').value.trim().toLowerCase();
  return (view==='saved'?saved:results).filter(row=>{
    if (filter==='confirmed' && !row.confirmed) return false;
    if (filter==='review' && row.confirmed) return false;
    return !query || JSON.stringify([row.location,row.canonical,row.candidates,row.selected]).toLowerCase().includes(query);
  });
}
function hasDutyEvidence(c) {
  return c.duty_verified!==false && (c.evidence||[]).some(e=>e.type==='업무안내'&&/지방\s*소득세/.test(e.text||c.duty||''));
}
function recommendedCandidate(row) {
  return row.confirmed&&row.selected ? row.selected : (row.candidates||[]).find(hasDutyEvidence);
}
function render() {
  const all=view==='saved'?saved:results, rows=visibleResults();
  $('result-total').textContent=all.length; $('result-found').textContent=all.filter(r=>recommendedCandidate(r)&&!r.confirmed).length;
  $('result-review').textContent=all.filter(r=>!r.confirmed).length; $('result-confirmed').textContent=all.filter(r=>r.confirmed).length;
  $('saved-count').textContent=saved.length; $('export').disabled=!all.length; $('copy-recipients').disabled=!rows.some(r=>r.confirmed);
  $('empty').hidden=rows.length>0;
  if (!rows.length) {
    $('empty').querySelector('h2').textContent=all.length ? '조건에 맞는 결과가 없습니다' : view==='saved' ? '확인한 수신처가 없습니다' : running ? '공식 홈페이지를 조회하고 있습니다' : '조회할 지자체를 추가하세요';
    $('empty').querySelector('p').textContent=view==='saved' ? '조회 결과에서 근거를 확인하고 수신처를 저장하세요.' : '엑셀의 지자체 목록에서 공문을 보낼 담당 부서를 찾습니다.';
  }
  $('result-rows').innerHTML=rows.map(row=>{
    const candidate=recommendedCandidate(row), c=candidate||{}, organizations=(row.candidates||[]).filter(c=>c.duty_verified===false);
    const css=row.confirmed?'confirmed':candidate?'found':'review';
    const evidence=c.evidence || [];
    const organizationNames=[...new Set(organizations.map(c=>c.department))].join(' · ');
    const subtitle=candidate ? (c.bureau||'상위 국 미확인')+(c.team?' · '+c.team:'') : organizationNames ? '조직 후보: '+organizationNames : '업무안내 확인 필요';
    const status=row.confirmed?'사용자 확인':!candidate&&organizations.length?'담당업무 미확인':row.status;
    return `<tr class="result-row" tabindex="0" data-id="${escape(row.id)}"><td><strong>${escape(row.location)}</strong>${row.canonical!==row.location?`<small>현재 명칭: ${escape(row.canonical)}</small>`:''}</td><td><strong>${escape(c.department || '담당 부서 미확인')}</strong><small>${escape(subtitle)}</small></td><td><span class="status ${css}">${escape(status)}</span></td><td><span class="proof-count">${evidence.length?evidence.length+'개 ↗':organizations.length?'조직 '+organizations.length+'개 →':'확인 →'}</span></td></tr>`;
  }).join('');
  $('footer-status').textContent=all.length ? `${all.length}개 결과 · 확인 완료 ${all.filter(r=>r.confirmed).length}개` : '조회 전';
  for (const tr of $('result-rows').querySelectorAll('tr')) {
    const open=()=>openDetail(all.find(r=>r.id===tr.dataset.id)); tr.addEventListener('click',open); tr.addEventListener('keydown',e=>{if(e.key==='Enter')open();});
  }
}
async function begin(locations=targets,sources=sourceUrls) {
  if (running) { toast('진행 중인 조회가 끝난 뒤 다시 시작해 주세요.'); return; }
  try {
    if ($('remember-websites').checked) {
      for (const [location,url] of Object.entries(sources)) await api('/api/websites',{location,url});
    }
    const job=await api('/api/search',{locations,kind:$('kind').value,sources}); jobId=job.id; results=[]; running=true; $('cancel').textContent='조회 중지';
    $('progress-area').hidden=false; $('cancel').disabled=false; setView('search'); updateTargets(); notice('공식 자료에서 후보를 찾고 있습니다. 결과가 나오면 각 행을 선택해 근거를 확인하세요.');
    await poll();
  } catch(e) { toast(e.message); }
}
async function poll() {
  try {
    const job=await api('/api/jobs/'+jobId); const changed=results.length!==job.results.length;
    results=job.results; $('progress').max=job.total; $('progress').value=job.completed;
    $('progress-text').textContent=`${job.completed} / ${job.total} 완료${job.current?' · '+job.current+' 조회 중':''}`;
    if (changed) render();
    if (job.state==='done'||job.state==='cancelled') {
      running=false; $('cancel').disabled=true; updateTargets(); render();
      $('progress-text').textContent=`${job.completed} / ${job.total} 완료${job.state==='cancelled'?' · 중지됨':''}`;
      notice(job.state==='cancelled'?'조회를 중지했습니다. 완료한 결과는 남아 있습니다.':'조회가 끝났습니다. 자동 결과는 후보이며, 업무와 조직 근거를 확인한 뒤 수신처로 저장하세요.');
      return;
    }
    polling=setTimeout(poll,1200);
  } catch(e) { running=false; updateTargets(); notice('조회 상태를 불러오지 못했습니다. 화면을 새로 열어 이전 완료 결과를 확인해 주세요.'); toast(e.message); }
}
function openDetail(row) {
  selectedRow=row; $('detail-title').textContent=row.location; $('detail-notes').textContent=(row.notes||[]).join(' ');
  $('detail-time').textContent=`조회: ${row.checked_at || '시각 미기록'}${row.confirmed_at ? ' · 확인: '+row.confirmed_at : ''}`;
  const checks=row.website_checks||[];
  $('website-trace').hidden=!checks.length;
  $('website-trace').querySelector('summary').textContent=`홈페이지 조회 기록 · ${checks.filter(c=>c.status==='확인').length}개 페이지 확인`;
  $('website-checks').innerHTML=checks.map(c=>`<article><b>${escape(c.status)}${c.roles?.length?' · '+escape(c.roles.join(' / ')):''}</b><a class="text-link" href="${escape(safeUrl(c.url))}" target="_blank" rel="noopener noreferrer">${escape(c.title||c.url)} ↗</a>${c.departments?.length?`<p>${escape(c.departments.join(' · '))}</p>`:''}${c.message?`<p>${escape(c.message)}</p>`:''}</article>`).join('');
  const candidates=row.candidates||[];
  const recommended=recommendedCandidate(row);
  candidateIndex=recommended?candidates.indexOf(recommended):-1;
  $('candidate-select').innerHTML=(candidateIndex>=0?'':'<option value="-1">직접 확인한 수신처 또는 담당업무 미확인</option>')+candidates.map((c,i)=>`<option value="${i}">${hasDutyEvidence(c)?'업무 근거 있음':'조직 정보만 있음'} · ${escape(c.department)} · ${escape(c.team || '팀 미확인')} · ${escape(c.phone || '연락처 미확인')} — ${escape(c.duty).slice(0,100)}</option>`).join('');
  $('candidate-select').value=String(candidateIndex);
  if (row.confirmed && row.selected) {
    const i=candidates.findIndex(c=>c.phone===row.selected.phone&&c.department===row.selected.department);
    if (i>=0) { candidateIndex=i; $('candidate-select').value=i; }
  }
  loadCandidate(recommended);
  $('manual-search').href=safeUrl(row.search_url) || 'https://search.naver.com/search.naver?query='+encodeURIComponent(row.location+' 지방소득세 담당');
  $('retry-url').value=row.website_source||''; $('verified').checked=false; $('confirm-error').textContent=''; $('detail-dialog').showModal();
}
function loadCandidate(c={}) {
  c=c||{}; $('duty-summary').textContent=c.duty || '지방소득세 담당업무를 확인하지 못했습니다. 기관코드의 조직 목록만으로 수신처를 선택하지 마세요. 공식 업무안내 URL로 다시 조회할 수 있습니다.';
  const evidence=c.evidence||(selectedRow.candidates||[]).filter(c=>c.duty_verified===false).flatMap(c=>c.evidence||[]);
  $('evidence').innerHTML=evidence.map(e=>`<article><b>${escape(e.type)}</b><p>${escape(e.text)}</p><a class="text-link" href="${escape(safeUrl(e.url))}" target="_blank" rel="noopener noreferrer">공식 근거 열기 ↗</a></article>`).join('') || '<p>확인한 공식 페이지의 URL을 아래에 입력해 주세요.</p>';
  $('canonical').value=selectedRow.canonical;
  for (const id of ['bureau','department','team','phone']) $(id).value=c[id] || '';
  $('source-url').value=c.url || ''; $('verified').checked=false; recipientPreview();
}
function recipientPreview() { $('recipient-preview').textContent=`${$('canonical').value || '기관명'}(${$('department').value || '담당 부서'})`; }
async function refreshSaved() { saved=(await api('/api/saved')).results; $('saved-count').textContent=saved.length; }
async function settingsStatus() {
  const data=await api('/api/settings'); $('connection-dot').classList.toggle('ready',data.naver_configured||data.public_configured);
  $('connection-label').textContent=data.public_configured?(data.naver_configured?'조직·검색 API 설정됨':'기관코드 API 설정됨'):(data.naver_configured?'검색 API 설정됨':'API 미설정');
  if (!data.naver_configured) notice('등록된 공식 홈페이지를 먼저 조회합니다. 홈페이지 관리에서 주소를 추가하거나 엑셀의 URL 열을 선택하세요. 기관코드 API는 조직 정보를 보완합니다.');
}
function renderWebsites() {
  const previous=$('website-region').value, regions=[...new Set(websites.map(w=>w.region||w.location.split(' ')[0]))].sort();
  $('website-region').innerHTML='<option value="">전체 시·도</option>'+regions.map(r=>`<option value="${escape(r)}">${escape(r)}</option>`).join('');
  if(regions.includes(previous)) $('website-region').value=previous;
  const region=$('website-region').value, query=$('website-filter').value.replace(/\s+/g,'').toLowerCase();
  const visible=websites.map((w,i)=>({w,i})).filter(({w})=>(!region||(w.region||w.location.split(' ')[0])===region)&&(!query||(w.location+w.url).replace(/\s+/g,'').toLowerCase().includes(query)));
  const builtin=websites.filter(w=>w.builtin).length;
  $('website-count').textContent=`총 ${websites.length}개 · 기본 등록 ${builtin}개 · 직접 등록 ${websites.length-builtin}개 · 현재 ${visible.length}개 표시`;
  $('website-list').innerHTML=visible.map(({w,i})=>`<article><div><strong>${escape(w.location)}</strong><small>${w.builtin?escape((w.url_kind||'홈페이지')+' 기본 등록'):'직접 등록'}${w.level?' · '+escape(w.level):''}${w.homepage_status?' · '+escape(w.homepage_status):''}</small><a class="text-link" href="${escape(safeUrl(w.url))}" target="_blank" rel="noopener noreferrer">공식 홈페이지 열기 ↗</a>${w.source_url?` · <a class="text-link" href="${escape(safeUrl(w.source_url))}" target="_blank" rel="noopener noreferrer">등록 출처 ↗</a>`:''}</div><div><button class="secondary" data-edit="${i}">수정</button>${w.builtin?'':`<button class="quiet" data-remove="${i}">등록 해제</button>`}</div></article>`).join('')||'<p>검색 조건에 맞는 등록 주소가 없습니다.</p>';
  for (const button of $('website-list').querySelectorAll('[data-edit]')) button.onclick=()=>{const w=websites[Number(button.dataset.edit)];$('website-location').value=w.location;$('website-url').value=w.url;$('website-location').focus();};
  for (const button of $('website-list').querySelectorAll('[data-remove]')) button.onclick=async()=>{try{websites=(await api('/api/websites/remove',{location:websites[Number(button.dataset.remove)].location})).websites;renderWebsites();toast('등록을 해제했습니다. 기본 등록 주소는 다시 표시됩니다.');}catch(e){$('website-error').textContent=e.message;}};
}
$('website-region').onchange=renderWebsites;$('website-filter').oninput=renderWebsites;
$('websites-open').onclick=async()=>{try{websites=(await api('/api/websites')).websites;renderWebsites();$('website-error').textContent='';$('websites-dialog').showModal();}catch(e){toast(e.message);}};
$('websites-close').onclick=()=>$('websites-dialog').close();
$('website-form').onsubmit=async e=>{
  e.preventDefault();$('website-save').disabled=true;$('website-error').textContent='';
  try{websites=(await api('/api/websites',{location:$('website-location').value,url:$('website-url').value})).websites;renderWebsites();$('website-form').reset();toast('주소를 저장했습니다. 다음 조회에 자동으로 사용합니다.');}
  catch(err){$('website-error').textContent=err.message;}finally{$('website-save').disabled=false;}
};
function settingsProvider() {
  const isPublic=$('settings-provider').value==='public';
  $('public-settings').hidden=!isPublic; $('naver-settings').hidden=isPublic;
  $('public-key').required=isPublic; $('client-id').required=!isPublic; $('client-secret').required=!isPublic;
  $('settings-save').textContent=isPublic?'저장 및 연결 확인':'이 컴퓨터에 저장';
  $('settings-error').textContent='';
}
$('tab-file').onclick=()=>setMode('file'); $('tab-paste').onclick=()=>setMode('paste'); $('locations').oninput=updateTargets;
$('sheet').onchange=populateColumns; $('header-row').onchange=populateColumns;
for(const id of ['location-column','province-column','url-column']) $(id).onchange=updateTargets;
$('upload').onchange=e=>upload(e.target.files[0]);
$('drop-zone').ondragover=e=>{e.preventDefault();$('drop-zone').classList.add('drag');};
$('drop-zone').ondragleave=()=>$('drop-zone').classList.remove('drag');
$('drop-zone').ondrop=e=>{e.preventDefault();$('drop-zone').classList.remove('drag');upload(e.dataTransfer.files[0]);};
$('sample').onclick=()=>{setMode('paste');$('locations').value='전남광주통합특별시 서구';updateTargets();notice('서구 예제가 입력되었습니다. 담당 부서 조회를 누르면 현재 공식 홈페이지를 읽습니다.');};
$('search-start').onclick=()=>begin();
$('cancel').onclick=async()=>{try{await api('/api/cancel',{id:jobId});$('cancel').disabled=true;$('cancel').textContent='현재 지자체 처리 후 중지';}catch(e){toast(e.message);}};
$('nav-search').onclick=()=>setView('search'); $('nav-saved').onclick=async()=>{try{await refreshSaved();setView('saved');}catch(e){toast(e.message);}};
$('result-search').oninput=render;
for(const button of $('filters').querySelectorAll('button')) button.onclick=()=>{filter=button.dataset.filter;for(const b of $('filters').querySelectorAll('button')) b.classList.toggle('active',b===button);render();};
$('settings-open').onclick=()=>{settingsProvider();$('settings-dialog').showModal();};
$('settings-provider').onchange=settingsProvider;
$('settings-close').onclick=()=>$('settings-dialog').close();
$('settings-dialog').addEventListener('close',()=>{for(const id of ['public-key','client-id','client-secret'])$(id).value='';});
$('settings-form').onsubmit=async e=>{
  e.preventDefault(); $('settings-save').disabled=true;
  const isPublic=$('settings-provider').value==='public';
  let stored=false;
  try{
    await api('/api/settings',isPublic?{provider:'public',service_key:$('public-key').value}:{provider:'naver',client_id:$('client-id').value,client_secret:$('client-secret').value});
    stored=true; for(const id of ['public-key','client-id','client-secret'])$(id).value='';
    await settingsStatus();
    if(isPublic){$('settings-save').textContent='연결 확인 중…';const check=await api('/api/settings/test',{});notice(check.message+' 지자체 목록을 조회해 주세요.');}
    else notice('검색 API 설정을 저장했습니다. 실제 인증 여부는 첫 조회에서 확인합니다.');
    $('settings-dialog').close();toast(isPublic?'기관코드 API 연결을 확인했습니다.':'설정을 저장했습니다.');
  }
  catch(err){$('settings-error').textContent=(stored?'키는 저장됐습니다. 연결 확인 결과: ':'')+err.message;}
  finally{$('settings-save').disabled=false;$('settings-save').textContent=isPublic?'저장 및 연결 확인':'이 컴퓨터에 저장';}
};
$('detail-close').onclick=()=>$('detail-dialog').close();
$('candidate-select').onchange=()=>{candidateIndex=Number($('candidate-select').value);loadCandidate(selectedRow.candidates[candidateIndex]);};
$('canonical').oninput=recipientPreview; $('department').oninput=recipientPreview;
$('confirm-form').onsubmit=async e=>{
  e.preventDefault(); $('confirm-save').disabled=true;
  try{
    const row=await api('/api/confirm',{job_id:view==='search'?jobId:'',row_id:selectedRow.id,candidate_index:candidateIndex,canonical:$('canonical').value,bureau:$('bureau').value,department:$('department').value,team:$('team').value,phone:$('phone').value,url:$('source-url').value,duty:$('duty-summary').textContent,verified:$('verified').checked});
    results=results.map(r=>r.id===row.id?row:r); await refreshSaved(); render(); $('detail-dialog').close();toast('확인한 수신처를 저장했습니다.');
  }catch(err){$('confirm-error').textContent=err.message;}finally{$('confirm-save').disabled=false;}
};
$('retry').onclick=async()=>{
  const url=$('retry-url').value.trim(); if(!safeUrl(url)){toast('공식 홈페이지의 https:// 주소를 입력해 주세요.');return;}
  if(running){toast('현재 조회가 끝난 후 다시 조회해 주세요.');return;}
  const name=selectedRow.location; $('detail-dialog').close();await begin([name],{[name]:url});
};
$('copy-recipients').onclick=async()=>{
  const rows=visibleResults().filter(r=>r.confirmed);const text=[...new Set(rows.map(r=>r.recipient))].join('\n');
  try{await navigator.clipboard.writeText(text);toast(`${rows.length}개 확인한 수신처를 복사했습니다.`);}catch(e){toast('클립보드 접근이 제한되었습니다. CSV 저장을 사용해 주세요.');}
};
$('export').onclick=()=>{
  const params=new URLSearchParams({mode:view,job_id:jobId,ids:visibleResults().map(r=>r.id).join(',')});
  const a=document.createElement('a');a.href='/api/export?'+params;a.download='departments.csv';document.body.appendChild(a);a.click();a.remove();
};
async function init(){try{await settingsStatus();await refreshSaved();const last=await api('/api/last');if(last.results?.length){results=last.results;notice('지난 조회 결과를 불러왔습니다. 최신 정보가 필요하면 새로 조회하세요.');}render();}catch(e){notice(e.message);}}
init();
