const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let segmentHierarchy=null;
let catalog, current, campaignState=[], motionBatchState=[], motionBatchPreview=null, participantDraftActors=[];
async function api(path, method='GET', body) {
  const r = await fetch('/api'+path, {method, headers:{'Content-Type':'application/json'}, ...(body ? {body:JSON.stringify(body)} : {})});
  const data = await r.json(); if (!r.ok) throw Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)); return data;
}
async function act(fn) { try { await fn(); $('#status').textContent='Saved / loaded.'; } catch(e) { $('#status').textContent=e.message; } }
function options(items) { return items.map(([id,label])=>`<option value="${esc(id)}">${esc(label)}</option>`).join(''); }
async function loadCatalog(selected) {
  catalog = await api('/skills/catalog');
  const vision=await api('/skills/vision-proposals');
  $('#vision').innerHTML=vision.proposals.map(v=>`<details><summary>${esc(v.model)} · ${esc(v.proposal.call)} · ${v.context?.samples?.length || v.frames.length} sampled frames</summary><p>${esc(v.proposal.call_basis)}</p><ul>${v.proposal.observations.map(o=>`<li>${esc(o)}</li>`).join('')}</ul><p>${v.proposal.limitations.map(esc).join(' ')}</p><p class="muted">${v.frames.map(f=>esc(f.label)).join('<br>')}</p></details>`).join('') || '<p>No vision proposals yet.</p>';
  $('#game').innerHTML = options(catalog.games.map(g=>[g.id,g.label]));
  $('#source').innerHTML = options(catalog.sources.map(s=>[s.id,s.title || s.id]));
  if(selected) $('#game').value=selected;
  await load();
}
async function load() {
  $('#review').hidden = !$('#game').value;
  if(!$('#game').value) { $('#status').textContent='No game spans yet. Add a span below to begin review.'; return; }
  current=await api('/games/'+encodeURIComponent($('#game').value)+'/skills');
  const game=catalog.games.find(g=>g.id===$('#game').value);
  $('#shot-source').innerHTML=options([...new Set(game.spans.map(s=>s.source_id))].map(id=>[id,catalog.sources.find(s=>s.id===id)?.title || id]));
  $('#sequence-source').innerHTML=$('#shot-source').innerHTML;
  $('#motion-source').innerHTML=$('#shot-source').innerHTML;
  $('#order-source').innerHTML=$('#shot-source').innerHTML;
  $('#campaign-source').innerHTML=$('#shot-source').innerHTML;
  if(!$('#campaign').elements.start_seconds.value) $('#campaign').elements.start_seconds.value=game.spans[0].start_seconds;
  $('#coverage-span').innerHTML=options(current.spans.map((s,i)=>[String(i),`${catalog.sources.find(v=>v.id===s.source_id)?.title || 'Source'} · ${clock(s.start_seconds)}–${clock(s.end_seconds)}`]));
  fillCoverage();
  motionBatchState=current.motion_batches || [];
  if(motionBatchPreview?.gameId!==current.game_id){motionBatchPreview=null;$('#motion-batch-preview').innerHTML='';}
  renderMotionBatches();
  campaignState=current.campaigns || [];
  renderCampaigns();
  renderVisionBudget(await api('/skills/vision-status'));
  if(!$('#sequence').elements.start_seconds.value) $('#sequence').elements.start_seconds.value=game.spans[0].start_seconds;
  if(!$('#shot').elements.id.value) $('#shot').elements.id.value=crypto.randomUUID();
  render();
  const hierarchyGame=current.game_id;
  const hierarchy=await api('/games/'+encodeURIComponent(hierarchyGame)+'/skill-segments');
  if(current.game_id===hierarchyGame){segmentHierarchy=hierarchy;renderSegmentHierarchy();}
}
async function save(document) {
  await api('/games/'+encodeURIComponent(current.game_id)+'/skills','PUT',{expected_revision:current.revision,document}); await load();
}
function positions() {
  const end=Number($('#end').value);
  $('#positions').dataset.end=String(end);
  $('#positions').innerHTML='<table><tr><th>Side / position</th><th>Participant</th><th>Skip</th><th>Review</th></tr>'+['red','yellow'].flatMap(side=>['lead','second','third','fourth'].map(position=>{
    const s=current.document.slots.find(s=>s.end===end&&s.side===side&&s.position===position);
    const participants=current.document.participants || [];
    const known=participants.find(p=>p.acceptance!=='excluded'&&(p.id===s?.participant || (p.acceptance==='confirmed'&&p.actor_ids.includes(s?.participant))));
    return `<tr data-side="${side}" data-position="${position}"><td>${side} ${position}</td><td><select data-known-person aria-label="${side} ${position} saved participant">${options([['','Custom / end-only label'],...participants.filter(p=>p.acceptance!=='excluded').map(p=>[p.id,p.label+(p.acceptance==='confirmed'?'':' (proposed)')])]).replace(`value="${esc(known?.id)}"`,`value="${esc(known?.id)}" selected`)}</select><input data-person ${known?'disabled':''} value="${esc(known?'':(s?.participant || ''))}" placeholder="End ${end} ${side} ${position}" aria-label="${side} ${position} participant"></td><td><input data-skip type="checkbox" ${s?.skip?'checked':''} aria-label="Skip"></td><td><select data-accept><option ${s?.acceptance==='proposed'?'selected':''}>proposed</option><option ${s?.acceptance==='confirmed'?'selected':''}>confirmed</option></select></td></tr>`;
  })).join('')+'</table>';
}
const clock = seconds => `${Math.floor(seconds/60)}:${String(Math.floor(seconds%60)).padStart(2,'0')}`;
const exclusions = reasons => Object.entries(reasons).map(([reason,n])=>`${n} ${reason.replaceAll('_',' ')}`).join('; ') || 'none';
const stat = s => s.n ? `${s.mean.toFixed(3)} ± ${s.sample_sd===null?'unknown':s.sample_sd.toFixed(3)} (n=${s.n}${s.status==='insufficient_sample'?'; insufficient':''})` : 'Unavailable';
function render() {
  renderHouseContext();
  renderOrder();
  positions();
  renderMotion();
  renderCalibrations();
  renderAim();
  renderActors();
  renderParticipants();
  renderCalls();
  const coverage=current.candidate_coverage;
  $('#delivery-candidates').innerHTML=(coverage?`<p>${coverage.candidate_count} candidates · ${coverage.unreviewed} without a saved review · ${coverage.with_saved_review} with a saved review · ${coverage.excluded} excluded. ${esc(coverage.notice)}</p>`:'')+(current.delivery_candidates || []).map(c=>`<p><strong>${clock(c.start_seconds)}–${clock(c.end_seconds)} · ${c.stone_color}</strong> · ${c.observations.length} observations · ${esc(c.call)} call${c.call_conflict?' · conflicting call proposals':''}${c.ambiguous_overlap?' · ambiguous overlap: review before counting':''}${c.duplicate_review?' · multiple saved shots: resolve before scoring':''}${c.review?.status==='excluded'?' · excluded by review':''} <button data-candidate="${esc(c.id)}" ${c.duplicate_review?'disabled':''}>Review candidate</button> <button data-call-candidate="${esc(c.id)}" ${c.ambiguous_overlap?'disabled':''}>Search pre-delivery call</button> <button data-motion-candidate="${esc(c.id)}" ${c.ambiguous_overlap?'disabled':''}>Inspect delivery motion</button></p>`).join('') || '<p>No delivery candidates yet.</p>';
  $('#sequence-jobs').innerHTML=current.sequence_jobs.map(j=>`<p>${j.request.purpose==='pre_delivery_call'?'Call search':'Sequence'} ${j.request.start_seconds}–${j.request.end_seconds}s · ${esc(j.request.view)} · ${esc(j.state)}${j.error?` · ${esc(j.error)}`:''}</p>`).join('');
  $('#sequences').innerHTML=current.sequences.map(s=>`<details><summary>${s.request.start_seconds}–${s.request.end_seconds}s · ${s.proposal.deliveries.length} delivery proposals${s.stale?' · source/game changed':''}</summary><p>${esc(s.proposal.call_basis)}</p>${s.sheet_assets.map((_,i)=>`<img loading="lazy" style="max-width:100%;width:720px" src="/api/skill-sequences/${encodeURIComponent(s.id)}/sheets/${i}.png" alt="Sequence frames ${i*4} through ${i*4+3}">`).join('')}<p>${s.proposal.limitations.map(esc).join(' ')}</p>${s.proposal.deliveries.map((d,i)=>`<p>${esc(d.thrower_description)} · ${d.stone_color} · ${d.call} · <a href="${esc(s.samples[d.first_frame].youtube_url)}" target="_blank" rel="noopener">Watch delivery</a> <button data-sequence="${esc(s.id)}" data-delivery="${i}" ${s.stale?'disabled':''}>Review shot</button></p>`).join('')}</details>`).join('');
  $('#shots').innerHTML='<table><tr><th>Shot</th><th>End / side</th><th>Call</th><th>Review</th><th></th></tr>'+current.document.shots.map(s=>`<tr><td>${clock(s.seconds)} · ${esc(participantLabel(s.participant))}</td><td>${s.end??"Unknown end"} ${s.side}</td><td>${s.call}</td><td>${s.acceptance}</td><td><button data-edit="${esc(s.id)}">Edit</button></td></tr>`).join('')+'</table>';
  $('#roles').innerHTML='<h3>Throwing-order proposals</h3>'+current.role_proposals.map(p=>`<p>End ${p.end} ${p.side} ${p.position}: ${p.candidates.map(c=>`${esc(participantLabel(c.participant))} (${c.shot_ids.length} shots)`).join('; ')} · ${p.status}</p>`).join('');
  $('#report').innerHTML=`<p>${current.stale?"Game or source changed; review the saved observations before using their statistics. ":""}${current.report.total_shots} shots · Excluded: ${esc(exclusions(current.report.excluded))}</p><table><tr><th>Participant / cohort</th><th>Weight ratio</th><th>Right + / left − degrees</th><th>Wide + / narrow − degrees</th><th>Sweeping seconds</th></tr>`+current.report.summaries.map(g=>`<tr><td>${esc(participantLabel(g.participant))}<br>${g.call} / ${g.direction}<br>${Object.entries(g.turn_counts).map(([turn,n])=>`${turn}: ${n}`).join(" · ")}<br>Weight: ${esc(g.calibration || 'No reference')}<br>Line: ${esc(g.line_calibration || 'No reference')}<br>Missing: ${esc(exclusions(g.missing))}</td><td>${stat(g.weight)}</td><td>${stat(g.line)}</td><td>${stat(g.wide)}</td><td>${stat(g.sweeping)}</td></tr>`).join('')+'</table>';
}
$('#reload').onclick=()=>act(load);
$('#game').onchange=()=>act(async()=>{
  $('#shot').reset(); $('#shot').elements.id.value=crypto.randomUUID();
  $('#participant').reset(); participantDraftActors=[];
  $('#order-window').reset();
  setShotAim(null);
  calibrationDraft=null; $('#calibration').hidden=true;
  $('#sequence').elements.start_seconds.value=''; $('#campaign').elements.start_seconds.value='';
  $('#motion').elements.start_seconds.value=''; $('#motion').elements.reference_seconds.value='';
  await load();
});
$('#slots').onclick=positions;
$('#positions').onchange=e=>{if(e.target.matches('[data-known-person]'))e.target.closest('tr').querySelector('[data-person]').disabled=Boolean(e.target.value);};
$('#save-slots').onclick=()=>act(async()=>{
  const end=Number($('#positions').dataset.end), slots=current.document.slots.filter(s=>s.end!==end);
  for(const row of $('#positions').querySelectorAll('tr[data-side]')) {
    const participant=row.querySelector('[data-known-person]').value || row.querySelector('[data-person]').value.trim(); if(!participant) continue;
    slots.push({end,side:row.dataset.side,position:row.dataset.position,participant,skip:row.querySelector('[data-skip]').checked,acceptance:row.querySelector('[data-accept]').value,basis:'Operator position review'});
  }
  await save({...current.document,slots});
});
$('#shot').onsubmit=e=>{e.preventDefault(); act(async()=>{
  const data=Object.fromEntries(new FormData(e.target));
  for(const k of ['seconds','end','team_delivery','speed_mps','target_speed_mps','line_error_degrees','swept_seconds']) data[k]=data[k]===''?null:Number(data[k]);
  for(const k of ['participant','calibration']) data[k]=data[k].trim()||null;
  data.evidence=data.evidence.split(';').map(s=>s.trim()).filter(Boolean);
  data.aim=shotAimDraft; data.line_calibration=shotLineCalibrationDraft;
  await save({...current.document,shots:[...current.document.shots.filter(s=>s.id!==data.id),data]});
});};
$('#shots').onclick=e=>{const id=e.target.dataset.edit;if(!id)return;const shot=current.document.shots.find(s=>s.id===id); setShotAim(shot.aim,shot.line_calibration); for(const [key,value] of Object.entries(shot)){if($('#shot').elements[key]) $('#shot').elements[key].value=key==='evidence'?value.join('; '):(value??'');} $('#shot').scrollIntoView();};
$('#create').onsubmit=e=>{e.preventDefault();act(async()=>{
  const f=new FormData(e.target), source=catalog.sources.find(s=>s.id===f.get('source')), id=crypto.randomUUID();
  if(!source.sheet) throw Error('Recording needs a sheet assignment first.');
  await api('/games/'+id,'PUT',{expected_revision:0,author:'Local operator',reason:'Game span for skill review',game:{id,sheet:source.sheet,label:source.title+' · skill review',spans:[{source_id:source.id,start_seconds:Number(f.get('start')),end_seconds:Number(f.get('end'))}]}});
  await loadCatalog(id);
});};
act(()=>loadCatalog());

$('#sequence').onsubmit=e=>{e.preventDefault();act(async()=>{
  const f=new FormData(e.target), start=Number(f.get('start_seconds'));
  await api('/games/'+encodeURIComponent(current.game_id)+'/skill-sequences','POST',{source_id:f.get('source_id'),start_seconds:start,end_seconds:start+Number(f.get('duration')),interval_seconds:1,view:f.get('view')});
  await load();
  $('#status').textContent='Sequence queued. Reload to see worker progress and results.';
});};
function reviewCandidate(candidate) {
  const review=candidate?.review;
  if(!review?.draft){$('#status').textContent='Resolve multiple saved reviews before making a combined draft.';return;}
  $('#shot').reset();setShotAim(review.draft.aim,review.draft.line_calibration);
  for(const [key,value] of Object.entries(review.draft)) {
    if($('#shot').elements[key]) $('#shot').elements[key].value=key==='evidence'?value.join('; '):(value??'');
  }
  $('#status').textContent=(review.status==='new'?'Proposed shot prepared. ':'Saved decisions preserved. ')+
    'Review the end, side, participant and call before saving.'+
    (review.release_proposals_seconds.length?` Proposed release times: ${review.release_proposals_seconds.join(', ')} seconds.`:' Release time remains unknown.')+
    (review.unattached_reference_count?` ${review.unattached_reference_count} additional frame references remain in the candidate evidence.`:'');
  $('#shot').scrollIntoView();
}
$('#delivery-candidates').onclick=e=>{
  const motionId=e.target.dataset.motionCandidate;
  if(motionId){act(async()=>{await api('/games/'+encodeURIComponent(current.game_id)+'/skill-candidates/'+encodeURIComponent(motionId)+'/motion','POST',{});await load();});return;}
  const callId=e.target.dataset.callCandidate;
  if(callId){act(async()=>{await api('/games/'+encodeURIComponent(current.game_id)+'/skill-candidates/'+encodeURIComponent(callId)+'/call-evidence','POST',{});await load();});return;}
  const id=e.target.dataset.candidate;if(!id)return;
  reviewCandidate(current.delivery_candidates.find(c=>c.id===id));
};
$('#sequences').onclick=e=>{
  const sid=e.target.dataset.sequence;if(!sid)return;
  const sequence=current.sequences.find(s=>s.id===sid), index=Number(e.target.dataset.delivery);
  if(sequence.stale)return;
  reviewCandidate(current.delivery_candidates.find(c=>c.observations.some(o=>o.sequence_id===sid&&o.delivery_index===index)));
};

$('#new-shot').onclick=()=>{$('#shot').reset();setShotAim(null); $('#shot').elements.id.value=crypto.randomUUID();};

function renderMotion() {
  $('#motion-jobs').innerHTML=(current.motion_jobs || []).map(j=>`<p>${j.processing_mode==='retained_detection_refinement'?'Stored-track refinement':'Motion'} ${clock(j.request.start_seconds)}–${clock(j.request.end_seconds)} · ${esc(j.state)}${j.error?` · ${esc(j.error)}`:''}</p>`).join('');
  $('#motions').innerHTML=[...(current.motions || [])].sort((a,b)=>b.created_at.localeCompare(a.created_at)).map(m=>{
    const usable=m.image_motion_tracks || m.measurements.filter(t=>t.status==='experimental_measurement');
    const measurementFor=t=>m.measurements.find(v=>v.track_id===(t.id || t.track_id));
    const pathFor=t=>(m.delivery_path_proposals || []).find(p=>p.track_id===t.id);
    const priorityCount=(m.delivery_path_proposals || []).filter(p=>p.status==='review_first').length;
    const base='/api/skill-motion/'+encodeURIComponent(m.id)+'/images/';
    return `<details data-motion-id="${esc(m.id)}"><summary>${clock(m.request.start_seconds)}–${clock(m.request.end_seconds)} · ${esc(m.request.view.replaceAll('_',' '))}${m.retained_detection_parent?' · refined':''} · ${usable.length} moving fragments${priorityCount?' · '+priorityCount+' to review first':''}${m.stale?' · source/game changed':''}</summary><p>${m.retained_detection_parent?'Refined from stored detections; original run retained. ':''}Experimental motion only. Release and intended target are unconfirmed; no weight or aim score. ${m.request.view.startsWith('perspective_')?(m.person_detector?.status==='available'?'Upper-body color patches are filtered using local person boxes.':'Person filtering was unavailable for this result.'):''}</p><img loading="lazy" style="max-width:100%;max-height:460px;width:auto" src="${base}overlay.png" alt="Proposed motion paths on the earlier reference image"><p><button data-refine-motion="${esc(m.id)}" ${m.stale||m.version===current.motion_tracking_version?'disabled':''}>Refine stored tracks</button> <button data-calibrate="${esc(m.id)}" ${m.stale?'disabled':''}>Calibrate this camera</button></p><p class="muted">Paths are drawn over the reference at ${m.reference_sample.source_seconds.toFixed(2)} seconds. Fragments are not independent shots. ${priorityCount?'Magenta/green paths are suggested for first review; other fragments are gray. This uses delivery color and a seed-camera downward-motion prior, not verified stone identity.':''}</p><div class="scroll"><table><tr><th>Fragment</th><th>Observed interval</th><th>Nominal plane speed</th><th>Release timing</th><th>Samples</th><th>Path suggestion</th></tr>${usable.map((t,i)=>`<tr><td>${i+1} · ${esc(t.color)}</td><td>${t.start_seconds.toFixed(2)}–${t.end_seconds.toFixed(2)}s</td><td>${measurementFor(t)?.status==='experimental_measurement'?measurementFor(t).nominal_plane_speed_mps.toFixed(2)+' m/s':'Unavailable: geometry needs review'}</td><td>${esc((t.relative_release_phase || 'release_unknown').replaceAll('_',' '))}${t.matches_delivery_color===false?'; different color':''}</td><td>${t.point_count ?? t.points.length}</td><td>${pathFor(t)?.status==='review_first'?'<strong>Review first</strong>':esc((pathFor(t)?.reasons || ['Not assessed']).join('; ').replaceAll('_',' '))}</td></tr>`).join('')}</table></div>${!usable.length?'<p>No supported motion measurements. '+esc((m.projection.reasons || []).join('; ').replaceAll('_',' '))+'</p>':''}<details><summary>Original retained frames (${m.retained_samples.length})</summary><div class="row">${m.retained_samples.map(f=>`<figure style="margin:4px"><img loading="lazy" width="220" style="max-width:100%" src="${base}${f.sample_index}.png" alt="Native camera crop at ${m.samples[f.sample_index].source_seconds.toFixed(2)} seconds"><figcaption>${m.samples[f.sample_index].source_seconds.toFixed(2)}s</figcaption></figure>`).join('')}</div></details><p class="muted">${m.limitations.map(v=>esc(v.replaceAll('_',' '))).join('; ')}. Colored patches can include clothing. House size filters are provisional; perspective tracks have no metric scale. Painted-plane projection does not correct handle height or lens distortion.</p></details>`;
  }).join('') || '<p>No motion analysis yet.</p>';
}
$('#motion').onsubmit=e=>{e.preventDefault();act(async()=>{
  const f=new FormData(e.target), start=Number(f.get('start_seconds'));
  await api('/games/'+encodeURIComponent(current.game_id)+'/skill-motion','POST',{
    source_id:f.get('source_id'),start_seconds:start,end_seconds:start+Number(f.get('duration')),
    reference_seconds:Number(f.get('reference_seconds')),interval_seconds:0.1,view:f.get('view')});
  await load();
});};

function participantLabel(id) {
  if(!id)return 'Unassigned player';
  const participant=(current?.document.participants || []).find(p=>p.id===id || (p.acceptance==='confirmed'&&p.actor_ids.includes(id)));
  if(participant)return participant.label;
  const actor=(current?.actor_proposals || []).find(a=>a.id===id);
  return actor?`Anonymous ${actor.actor_tag} · ${clock(actor.start_seconds)}`:id;
}
function renderActors() {
  $('#skill-actors').innerHTML=(current.actor_proposals || []).map(actor=>{
    const sequence=current.sequences.find(s=>s.id===actor.sequence_id);
    const crop=(sequence.activity_assets || []).find(a=>a.asset&&actor.activities.some(v=>v.activity_index===a.activity_index));
    return `<details><summary>${esc(participantLabel(actor.id))} · ${esc(actor.team_side)} side proposed · ${actor.role_proposals.map(r=>esc(r.replaceAll('_',' '))).join(', ') || 'Role unknown'}</summary><div class="row">${crop?`<img loading="lazy" style="max-width:180px;max-height:240px;object-fit:contain" src="/api/skill-sequences/${encodeURIComponent(actor.sequence_id)}/actors/${crop.activity_index}.png" alt="Anonymous participant crop at an observed frame">`:''}<div><p>${actor.descriptions.map(esc).join('; ')}</p><p>${actor.side_conflict?'Conflicting team-side proposals. ':''}Throwing position and skip: unconfirmed.</p><button data-use-actor="${esc(actor.id)}">Use participant in shot draft</button> <button data-link-actor="${esc(actor.id)}">Add to participant draft</button></div></div><ul>${actor.activities.map(a=>`<li>${esc(a.action.replaceAll('_',' '))} · ${sequence.samples[a.first_frame].source_seconds.toFixed(2)}–${sequence.samples[a.last_frame].source_seconds.toFixed(2)}s: ${esc(a.evidence_basis)} ${esc(a.side_basis)} ${a.limitations.map(esc).join(' ')}</li>`).join('')}</ul><p class="muted">Activity intervals span observed samples; they are not measured sweeping duration.</p></details>`;
  }).join('') || '<p>No anonymous activity proposals yet. New sequence analyses include them.</p>';
}
$('#skill-actors').onclick=e=>{
  if(e.target.dataset.linkActor){$('#participant').elements.acceptance.value='proposed';participantDraftActors=[...new Set([...participantDraftActors,e.target.dataset.linkActor])];renderParticipantActors();$('#participant').scrollIntoView();return;}
  const id=e.target.dataset.useActor;if(!id)return;
  const actor=current.actor_proposals.find(a=>a.id===id), form=$('#shot');
  if(form.elements.source_id.value!==actor.source_id){$('#status').textContent='Choose this actor’s source in the shot draft first.';return;}
  form.elements.participant.value=id;
  const references=form.elements.evidence.value.split(';').map(s=>s.trim()).filter(Boolean);
  form.elements.evidence.value=[...new Set([...references,...actor.evidence])].join('; ');
  $('#status').textContent=participantLabel(id)+' selected. Review the shot time, end and delivery order before saving.';
  form.scrollIntoView();
};

function renderCampaigns() {
  $('#skill-campaigns').innerHTML=campaignState.map(c=>{
    const survey=c.survey.filter(u=>u.state==='succeeded').length, refined=c.refinements.filter(u=>u.state==='succeeded').length;
    return `<details><summary>${clock(c.plan.request.start_seconds)}–${clock(c.plan.request.end_seconds)} · ${esc(c.state)} · survey ${survey}/${c.survey.length}${c.stale?' · source/game changed':''}</summary><p>${c.refinements_planned?`${refined}/${c.refinements.length} follow-up clips completed; ${c.deferred_refinements || 0} beyond the selected limit.`:`Up to ${c.plan.request.max_refinements} delivery close-ups and up to ${(c.plan.request.max_call_candidates || 0)*2} call-search clips after the survey.`}</p>${c.pause_reason?`<p>${esc(c.pause_reason)}</p>`:''}${c.state==='active'?`<button data-campaign="${esc(c.id)}" data-control="pause">Pause</button>`:''}${c.state==='paused'&&!c.stale?`<button data-campaign="${esc(c.id)}" data-control="resume">Resume</button>`:''}${['active','paused'].includes(c.state)?` <button data-campaign="${esc(c.id)}" data-control="cancel">Cancel remaining clips</button>`:''}<table><tr><th>Stage</th><th>Clip</th><th>State</th></tr>${[['Survey',c.survey],['Delivery follow-up',c.refinements]].flatMap(([label,units])=>units.map(u=>`<tr><td>${u.request.purpose==='pre_delivery_call'?'Call search · '+esc(u.request.view):label}</td><td>${clock(u.request.start_seconds)}–${clock(u.request.end_seconds)}</td><td>${esc(u.state.replaceAll('_',' '))}</td></tr>`)).join('')}</table><p class="muted">${esc(c.plan.notice)}</p></details>`;
  }).join('') || '<p>No excerpt processing runs yet.</p>';
}
$('#campaign').onsubmit=e=>{e.preventDefault();act(async()=>{
  const f=new FormData(e.target), start=Number(f.get('start_seconds'));
  await api('/games/'+encodeURIComponent(current.game_id)+'/skill-campaigns','POST',{
    source_id:f.get('source_id'),start_seconds:start,end_seconds:start+Number(f.get('duration')),
    max_refinements:Number(f.get('max_refinements')),max_call_candidates:Number(f.get('max_call_candidates'))});
  await load();
});};
$('#skill-campaigns').onclick=e=>{const id=e.target.dataset.campaign, action=e.target.dataset.control;if(!id||!action)return;act(async()=>{
  const campaign=campaignState.find(c=>c.id===id);
  await api('/skill-campaigns/'+encodeURIComponent(id)+'/control','POST',{action,expected_revision:campaign.revision});
  campaignState=(await api('/games/'+encodeURIComponent(current.game_id)+'/skill-campaigns')).campaigns;renderCampaigns();
});};
setInterval(async()=>{
  if(!current || !campaignState.some(c=>c.state==='active'))return;
  const gameId=current.game_id;
  try {
    const [result,budget]=await Promise.all([api('/games/'+encodeURIComponent(gameId)+'/skill-campaigns'),api('/skills/vision-status')]);
    if(current.game_id!==gameId)return;
    // Refresh status only; never overwrite a shot or position the operator is editing.
    const opened=[...$('#skill-campaigns').querySelectorAll('details')].map(d=>d.open);
    campaignState=result.campaigns;renderCampaigns();renderVisionBudget(budget);
    $('#skill-campaigns').querySelectorAll('details').forEach((d,i)=>{d.open=opened[i] || false;});
  } catch(e) { $('#status').textContent=e.message; }
},5000);

function renderVisionBudget(status) {
  $('#vision-budget').textContent=`Reserved/estimated usage: $${(status.committed_cents/100).toFixed(2)} of the local $${Number(status.budget_usd).toFixed(2)} limit. This is not your OpenAI invoice.`;
}

function renderCalls() {
  $('#call-evidence').innerHTML=(current.call_evidence || []).map(c=>`<details><summary>${clock(c.start_seconds)}–${clock(c.end_seconds)} · ${esc(c.view.replaceAll('_',' '))} · ${c.targets.length} broom/target points${c.stale?' · stale evidence':''}</summary><p>Observed call proposal: <strong>${esc(c.call)}</strong>. ${esc(c.call_basis)}</p><p class="muted">${c.candidate_ids.length?'Precedes a delivery candidate; receiving end and call association remain unconfirmed.':'Delivery association unavailable.'}</p>${c.targets.map(t=>`<div><p><strong>${esc(t.kind.replaceAll('_',' '))}</strong> · ${t.source_seconds.toFixed(2)}s · ${esc(t.evidence_basis)}</p><img loading="lazy" style="max-width:100%;width:400px" src="/api/skill-sequences/${encodeURIComponent(c.sequence_id)}/targets/${t.target_index}/overlay.png" alt="Proposed ${esc(t.kind.replaceAll('_',' '))} marked in magenta"><details><summary>Original unmarked frame</summary><img loading="lazy" style="max-width:100%;width:400px" src="/api/skill-sequences/${encodeURIComponent(c.sequence_id)}/targets/${t.target_index}/native.png" alt="Original target evidence"></details><p class="muted">Source-image pixel point: ${t.source_pixel_xy.map(v=>v.toFixed(1)).join(', ')}. ${t.limitations.map(esc).join(' ')}</p></div>`).join('') || '<p>No clearly supported broom or target point found.</p>'}${c.strategy_options.length?`<h3>Strategy hypotheses — intent unconfirmed</h3><ul>${c.strategy_options.map(s=>`<li>${esc(s.call)}: ${esc(s.rationale)} Missing context: ${s.missing_context.map(esc).join('; ')}.</li>`).join('')}</ul>`:''}<p>${c.limitations.map(esc).join(' ')}</p></details>`).join('') || '<p>No pre-delivery house search yet. Use Search pre-delivery call beside a candidate, or enable call searches in an automatic excerpt run.</p>';
}

function actorPreview(id) {
  const actor=current.actor_proposals.find(a=>a.id===id);
  if(!actor)return '<p>Observation no longer available.</p>';
  const sequence=current.sequences.find(s=>s.id===actor.sequence_id);
  const crop=(sequence.activity_assets || []).find(a=>a.asset&&actor.activities.some(v=>v.activity_index===a.activity_index));
  return `<figure style="margin:6px;max-width:260px">${crop?`<img loading="lazy" style="max-width:140px;max-height:190px;object-fit:contain" src="/api/skill-sequences/${encodeURIComponent(actor.sequence_id)}/actors/${crop.activity_index}.png" alt="Anonymous observation at ${clock(actor.start_seconds)}">`:''}<figcaption>${esc(participantLabel(id))}<br>${actor.descriptions.map(esc).join('; ')}</figcaption></figure>`;
}
function renderParticipantActors() {
  $('#participant-actors').innerHTML=participantDraftActors.map(id=>`<div class="row">${actorPreview(id)}<button data-remove-actor="${esc(id)}">Remove observation</button></div>`).join('') || '<p>No observations selected. Review a suggestion or add an observation from the activity cards.</p>';
}
function renderParticipants() {
  const participants=current.document.participants || [], selected=$('#participant-id').value;
  $('#participant-id').innerHTML=options([['','New participant'],...participants.map(p=>[p.id,p.label])]);
  if(participants.some(p=>p.id===selected))$('#participant-id').value=selected;
  if(!$('#participant').elements.label.value)$('#participant').elements.label.value='Anonymous player '+(participants.length+1);
  renderParticipantActors();
  $('#participants').innerHTML=participants.map(p=>`<p><strong>${esc(p.label)}</strong> · ${p.actor_ids.length} observations · ${esc(p.acceptance)} <button data-edit-participant="${esc(p.id)}">Edit participant</button> <button data-use-participant="${esc(p.id)}">Use in shot draft</button></p>`).join('') || '<p>No stable participants saved yet.</p>';
  $('#continuity-proposals').innerHTML=(current.continuity_proposals || []).map(p=>`<details><summary>${p.evidence.some(e=>e.kind==='same_delivery_proposal')?'Possible same thrower':'Possible same participant'} · ${p.evidence.length} shared observations${p.conflicts.length?' · conflicting assignments':''}</summary><div class="row">${p.actor_ids.map(actorPreview).join('')}</div><ul>${p.evidence.map(e=>`<li>${e.kind==='same_delivery_proposal'?`Same proposed delivery at ${clock(e.start_seconds)}–${clock(e.end_seconds)}`:`Body boxes overlap in the identical source frame at ${e.source_seconds.toFixed(2)} seconds`}</li>`).join('')}</ul><p>${p.conflicts.map(v=>esc(v.replaceAll('_',' '))).join('; ')}</p><button data-prepare-link="${esc(p.id)}">Prepare participant link</button><p class="muted">${esc(p.notice)}</p></details>`).join('') || '<p>No unresolved continuity suggestions with shared evidence.</p>';
}
function editParticipant(id) {
  const participant=(current.document.participants || []).find(p=>p.id===id);
  $('#participant').reset();
  if(participant){for(const [key,value] of Object.entries(participant))if($('#participant').elements[key])$('#participant').elements[key].value=value;participantDraftActors=[...participant.actor_ids];}
  else {$('#participant-id').value='';participantDraftActors=[];$('#participant').elements.label.value='Anonymous player '+((current.document.participants || []).length+1);}
  renderParticipantActors();
}
$('#participant-id').onchange=()=>editParticipant($('#participant-id').value);
$('#new-participant').onclick=()=>editParticipant('');
$('#participant-actors').onclick=e=>{const id=e.target.dataset.removeActor;if(id){participantDraftActors=participantDraftActors.filter(a=>a!==id);renderParticipantActors();}};
$('#continuity-proposals').onclick=e=>{
  const id=e.target.dataset.prepareLink;if(!id)return;
  const proposal=current.continuity_proposals.find(p=>p.id===id);
  const existing=(current.document.participants || []).filter(p=>p.acceptance!=='excluded'&&proposal.actor_ids.some(a=>p.actor_ids.includes(a)));
  if(existing.length>1){$('#status').textContent='These observations have separate participant assignments. Edit those assignments before combining them.';return;}
  editParticipant(existing[0]?.id || '');
  participantDraftActors=[...new Set([...participantDraftActors,...proposal.actor_ids])];
  $('#participant').elements.acceptance.value='proposed';
  $('#participant').elements.basis.value='Shared observations suggest continuity; review both clips before confirming.';
  renderParticipantActors();$('#participant').scrollIntoView();
};
$('#participant').onsubmit=e=>{e.preventDefault();act(async()=>{
  const f=new FormData(e.target), id=f.get('id') || 'skill-player:'+crypto.randomUUID();
  const participant={id,label:f.get('label').trim(),acceptance:f.get('acceptance'),basis:f.get('basis').trim(),actor_ids:participantDraftActors};
  await save({...current.document,participants:[...(current.document.participants || []).filter(p=>p.id!==id),participant]});
  editParticipant(id);
});};
$('#participants').onclick=e=>{
  if(e.target.dataset.editParticipant){editParticipant(e.target.dataset.editParticipant);$('#participant').scrollIntoView();}
  if(e.target.dataset.useParticipant){$('#shot').elements.participant.value=e.target.dataset.useParticipant;$('#shot').scrollIntoView();}
};

function fillCoverage() {
  const span=current.spans[Number($('#coverage-span').value)];
  if(!span)return;
  $('#coverage').elements.start_seconds.value=span.start_seconds;
  $('#coverage').elements.end_seconds.value=span.end_seconds;
}
$('#coverage-span').onchange=fillCoverage;
$('#coverage').onsubmit=e=>{e.preventDefault();act(async()=>{
  const f=new FormData(e.target), gameId=current.game_id;
  await api('/games/'+encodeURIComponent(gameId)+'/skills/coverage','POST',{
    expected_game_revision:current.game_revision,span_index:Number(f.get('span_index')),
    start_seconds:Number(f.get('start_seconds')),end_seconds:Number(f.get('end_seconds'))});
  await loadCatalog(gameId);
});};

function renderHouseContext() {
  const context=current.house_context;
  if(!context) { $('#house-context').innerHTML='<p>No retained-house analysis yet.</p>'; return; }
  const frame=(group, state, endpoint)=>`/api/skill-house-context/${encodeURIComponent(context.id)}/${group}/${state}/${endpoint}.png`;
  $('#house-context').innerHTML=`<p>${context.sample_count} source frames · ${context.inputs.length} survey sequences · ${context.pending_sequences || 0} newer sequences available${context.stale?' · Game/source changed; reanalyze before using these proposals':''}</p>`+
    (context.excluded_sequences.length || context.skipped_frames.length ? `<p>Excluded: ${context.excluded_sequences.length} sequences and ${context.skipped_frames.length} frames. Coverage may be incomplete.</p>`:'')+
    context.groups.map((g,gi)=>`<h3>${esc(catalog.sources.find(s=>s.id===g.source_id)?.title || 'Source')}</h3><p>${esc(g.notice)}</p><p>Sampled coverage: ${g.coverage_intervals.map(i=>`${clock(i.start_seconds)}–${clock(i.last_seconds)}`).join('; ')}. ${g.states.length} stable inner-house layouts.</p>`+
      ([...g.possible_clearings, ...(g.unresolved_changes || [])].map(c=>{
        const before=g.states.findIndex(s=>s.last.sample_index===c.before.sample_index&&s.view===c.view);
        const after=g.states.findIndex(s=>s.first.sample_index===c.after.sample_index&&s.view===c.view);
        return `<article><h4>${c.kind==='unresolved_house_change'?'Unresolved house change':'Possible clearing'} · ${esc(c.view.replaceAll('_',' '))} · ${clock(c.earliest_seconds)}–${clock(c.latest_seconds)}</h4><p>${esc(c.notice)} ${c.unresolved_reasons?.length?'Withheld: '+c.unresolved_reasons.map(r=>esc(r.replaceAll('_',' '))).join('; '):""}</p><div class="row"><figure><img loading="lazy" width="256" src="${frame(gi,before,'last')}" alt="Before possible clearing"><figcaption>${c.before_candidate_count} inner-house handle candidates</figcaption></figure><figure><img loading="lazy" width="256" src="${frame(gi,after,'first')}" alt="After possible clearing"><figcaption>No inner-house detections through ${clock(c.zero_detections_through_seconds)}</figcaption></figure></div><a href="${esc(c.before.youtube_url)}" target="_blank" rel="noopener">Watch transition</a></article>`;
      }).join('') || '<p>No supported clearing proposals in these samples.</p>')+
      `<details><summary>Stable layouts and detection diagnostics</summary><p>Inner house: ${esc(exclusions(g.diagnostics))}. Full house: ${g.full_house_state_count} stable layouts; ${esc(exclusions(g.full_house_diagnostics))}.</p><div class="row">${g.states.map((s,si)=>`<figure><img loading="lazy" width="128" src="${frame(gi,si,'first')}" alt="Proposed ${esc(s.view)} layout at ${clock(s.start_seconds)}"><figcaption>${esc(s.view.replaceAll('_',' '))} · ${clock(s.start_seconds)}–${clock(s.end_seconds)}<br>${s.candidate_count} handle candidates · <a href="${esc(s.first.youtube_url)}" target="_blank" rel="noopener">Video</a></figcaption></figure>`).join('')}</div></details>`).join('');
}
$('#analyze-house-context').onclick=()=>act(async()=>{
  const gameId=current.game_id;
  const button=$('#analyze-house-context'); button.disabled=true;
  $('#house-context-status').textContent='Analyzing retained images locally…';
  try {
    const result=await api('/games/'+encodeURIComponent(gameId)+'/skills/house-context','POST');
    if(current.game_id===gameId) {
      current.house_context=result; renderHouseContext();
      $('#house-context-status').textContent=result.cached?'Loaded unchanged analysis.':'Local analysis saved. End numbers remain unconfirmed.';
    }
  } catch(error) { $('#house-context-status').textContent=error.message; throw error; }
  finally { button.disabled=false; }
});

function renderOrder() {
  const windows=current.document.end_windows || [];
  $('#order-windows').innerHTML=windows.map(w=>`<p>End ${w.end} · ${clock(w.start_seconds)}–${clock(w.end_seconds)} · ${esc(w.acceptance)} · first side ${esc(w.first_side)} <button data-order-window="${esc(w.id)}">Edit interval</button></p>`).join('') || '<p>No reviewed end intervals yet. Observed deliveries remain unscoped.</p>';
  const report=current.order_report;
  if(!report) return;
  $('#throwing-order').innerHTML=`<p>${esc(report.notice)} ${report.unscoped_event_count} observations outside saved end intervals.</p>`+
    report.windows.map(w=>`<h3>End ${w.end} order possibilities</h3>${w.issues.length?`<p>Order withheld: ${w.issues.map(i=>esc(i.replaceAll('_',' '))).join('; ')}.</p>`:''}<div class="scroll"><table><tr><th>Observation</th><th>Possible side / team throw</th><th>Possible position</th><th>Review</th></tr>${w.entries.map(e=>`<tr><td>${clock(e.start_seconds)} · ${esc(e.side)}</td><td>${e.side_options.join(' / ') || 'Unknown'} · ${e.team_delivery_options.join(', ') || 'Unresolved'}</td><td>${e.position_options.join(', ') || 'Unresolved'}</td><td>${e.candidate_id?`<button data-order-candidate="${esc(e.candidate_id)}" data-order-window="${esc(w.window_id)}">Review shot${e.proposed_team_delivery?' with order proposal':''}</button>`:'Saved shot anchor'}</td></tr>`).join('')}</table></div>`).join('')+
    `<h3>Possible missed-delivery gaps</h3>`+(report.search_leads.map(g=>`<p>${clock(g.start_seconds)}–${clock(g.end_seconds)} · ${g.side} observed on both sides of this gap. ${esc(g.notice)}${g.search_plan?` <button data-order-gap="${esc(g.id)}">Search ${clock(g.search_plan.request.start_seconds)}–${clock(g.search_plan.request.end_seconds)} in ${esc(g.search_plan.request.view.replaceAll('_',' '))}</button>`:""}</p>`).join('') || '<p>No same-color gaps flagged. This does not establish complete coverage.</p>');
}
function editOrderWindow(id) {
  const form=$('#order-window'); form.reset();
  const w=(current.document.end_windows || []).find(w=>w.id===id);
  if(w) { for(const [key,value] of Object.entries(w)) if(form.elements[key]) form.elements[key].value=key==='evidence'?value.join('; '):value; }
  else form.elements.id.value='';
}
$('#new-order-window').onclick=()=>editOrderWindow('');
$('#order-windows').onclick=e=>{if(e.target.dataset.orderWindow) editOrderWindow(e.target.dataset.orderWindow);};
$('#order-window').onsubmit=e=>{e.preventDefault();act(async()=>{
  const data=Object.fromEntries(new FormData(e.target));
  data.id=data.id || crypto.randomUUID();
  for(const key of ['end','start_seconds','end_seconds']) data[key]=Number(data[key]);
  data.evidence=data.evidence.split(';').map(s=>s.trim()).filter(Boolean);
  data.format='standard_four_player';
  await save({...current.document,end_windows:[...(current.document.end_windows || []).filter(w=>w.id!==data.id),data]});
  editOrderWindow(data.id);
});};
$('#throwing-order').onclick=e=>{
  const gap=e.target.dataset.orderGap;
  if(gap) { act(async()=>{ await api('/games/'+encodeURIComponent(current.game_id)+'/skills/order-gaps/'+encodeURIComponent(gap)+'/search','POST',{}); await load(); }); return; }
  const id=e.target.dataset.orderCandidate; if(!id)return;
  const window=current.order_report.windows.find(w=>w.window_id===e.target.dataset.orderWindow);
  const entry=window?.entries.find(v=>v.candidate_id===id);
  const candidate=current.delivery_candidates.find(c=>c.id===id);
  reviewCandidate(candidate);
  if(!entry || !candidate?.review?.draft || candidate.review.status!=='new' || window.issues.length) return;
  $('#shot').elements.end.value=entry.end;
  if(entry.proposed_side) $('#shot').elements.side.value=entry.proposed_side;
  if(entry.proposed_team_delivery) $('#shot').elements.team_delivery.value=entry.proposed_team_delivery;
  $('#shot').elements.note.value+=' End/order proposal from reviewed interval '+window.window_id+'. Missing throws were allowed; confirm against the video.';
  $('#status').textContent='Prepared an unsaved shot with the available end/order proposals. Review against the video before saving.';
};

// Reviewed camera maps remain separate from player ratings.
let calibrationDraft=null, calibrationImage=null, calibrationFit=null, comparisonGameId=null;
function renderCalibrations() {
  if(comparisonGameId!==current.game_id){$('#camera-comparison-result').innerHTML='';comparisonGameId=current.game_id;}

  const motions=(current.motions || []).filter(m=>!m.stale && m.version===current.motion_tracking_version);
  for(const selector of ['#camera-reference','#camera-target']){
    const previous=$(selector).value;
    $(selector).innerHTML=options(motions.map(m=>[m.id,`${m.request.view.replaceAll('_',' ')} · ${clock(m.request.start_seconds)}–${clock(m.request.end_seconds)}${m.retained_detection_parent?' · refined':''}`]));
    if(motions.some(m=>m.id===previous))$(selector).value=previous;
  }
  const parents=(current.document.calibrations || []).filter(c=>c.acceptance==='confirmed'&&!c.transfer);
  $('#camera-parent').innerHTML=options(parents.map(c=>[c.id,`${c.view.replaceAll('_',' ')} · ${clock(c.start_seconds)}–${clock(c.end_seconds)}`]));
  $('#camera-transfer').hidden=!parents.length;
  $('#camera-transfer-availability').textContent=parents.length?'Choose a target motion from the same recording and camera. The transferred map remains a proposal.':'Save an original confirmed camera map to propose its landmarks for another clip. Frame comparison is available without one.';

  $('#calibrations').innerHTML=(current.document.calibrations || []).map(c=>{
    const result=(current.calibration_reports || []).find(r=>r.id===c.id);
    return `<details><summary>${esc(c.view.replaceAll('_',' '))} · ${clock(c.start_seconds)}–${clock(c.end_seconds)} · ${esc(c.acceptance)} · ${esc((result?.fit?.status || 'unavailable').replaceAll('_',' '))}</summary><p>${esc(c.basis)}</p>${calibrationResultHtml(result)}<button data-edit-calibration="${esc(c.id)}">Review calibration</button></details>`;
  }).join('') || '<p>No reviewed camera maps yet.</p>';
}
function calibrationResultHtml(result) {
  if(!result)return '';
  if(!result.fit)return `<p>${esc(result.reason || 'Calibration unavailable')}</p>`;
  const fit=result.fit;
  return `<p><strong>${esc(fit.status.replaceAll('_',' '))}</strong>${fit.maximum_validation_error_m!==undefined?` · maximum check-point error ${fit.maximum_validation_error_m.toFixed(3)} m`:''}</p>${fit.reasons.length?`<ul>${fit.reasons.map(r=>`<li>${esc(r.replaceAll('_',' '))}</li>`).join('')}</ul>`:''}<p class="muted">A small landmark error is not a bound on physical accuracy. Lens distortion, handle height, camera movement and point labeling still matter. Only tracks inside the landmark region and validity interval can be projected.</p>`+
    (fit.landmark_errors?.length?`<details><summary>Landmark errors</summary><table><tr><th>Landmark</th><th>Use</th><th>Error</th></tr>${fit.landmark_errors.map(p=>`<tr><td>${esc(p.label)}</td><td>${p.use==='fit'?'Fit':'Check'}</td><td>${p.error_m.toFixed(3)} m${p.within_tolerance?'':' · exceeds tolerance'}</td></tr>`).join('')}</table></details>`:'')+
    (result.measurements?.length?`<div class="scroll"><table><tr><th>Track</th><th>Plane speed</th><th>Review</th></tr>${result.measurements.map((m,i)=>`<tr><td>${i+1}</td><td>${m.speed_mps!==null?m.speed_mps.toFixed(3)+' m/s':'Unavailable'}</td><td>${esc((m.reason || m.status).replaceAll('_',' '))}</td></tr>`).join('')}</table></div><p>No weight or aim score is created by this calibration.</p>`:'');
}
function showCalibration(draft, result) {
  calibrationDraft=structuredClone(draft);
  calibrationImage={url:result.image_url,size:result.image_size};
  calibrationFit=result.fit || null;
  const form=$('#calibration');form.hidden=false;
  for(const name of ['coordinate_system','dimension_basis','start_seconds','end_seconds','max_validation_error_m','acceptance','basis']) form.elements[name].value=draft[name];
  form.elements.evidence.value=draft.evidence.join('; ');
  $('#calibration-transfer-review').hidden=!draft.transfer;
  form.elements.target_landmarks_reviewed.checked=Boolean(draft.transfer?.target_landmarks_reviewed);
  $('#calibration-source').textContent=`${draft.view.replaceAll('_',' ')} · ${result.image_size?.join(' × ') || 'Unknown'} native pixels · calibration ${draft.id}`;
  $('#calibration-message').textContent='';
  $('#calibration-result').innerHTML=calibrationResultHtml(result);
  renderLandmarkRows();drawCalibrationImage();
  $('#camera-calibration').scrollIntoView({block:'start'});
}
function readLandmarks() {
  return [...$('#calibration-landmarks').querySelectorAll('tr')].map(row=>({
    label:row.querySelector('[data-field="label"]').value,
    pixel:['px','py'].map(n=>Number(row.querySelector(`[data-field="${n}"]`).value)),
    plane_m:['x','y'].map(n=>Number(row.querySelector(`[data-field="${n}"]`).value)),
    use:row.querySelector('[data-field="use"]').value,
  }));
}
function preserveLandmarks() {
  // Keep empty numeric fields empty when adding/removing rows; never turn blanks into zero landmarks.
  calibrationDraft.landmarks=[...$('#calibration-landmarks').querySelectorAll('tr')].map(row=>({
    label:row.querySelector('[data-field="label"]').value,
    pixel:['px','py'].map(n=>row.querySelector(`[data-field="${n}"]`).value),
    plane_m:['x','y'].map(n=>row.querySelector(`[data-field="${n}"]`).value),
    use:row.querySelector('[data-field="use"]').value,
  }));
}
function renderLandmarkRows() {
  $('#calibration-landmarks').innerHTML=calibrationDraft.landmarks.map((p,i)=>`<tr>
    <td><input data-field="label" value="${esc(p.label)}" maxlength="100" required aria-label="Landmark ${i+1} label" style="min-width:100px"></td>
    ${[['px',p.pixel[0]],['py',p.pixel[1]],['x',p.plane_m[0]],['y',p.plane_m[1]]].map(([name,v])=>`<td><input data-field="${name}" value="${esc(v)}" type="number" step="any" required aria-label="Landmark ${i+1} ${name}" style="min-width:70px"></td>`).join('')}
    <td><select data-field="use" aria-label="Landmark ${i+1} use"><option value="fit" ${p.use==='fit'?'selected':''}>Fit</option><option value="validation" ${p.use==='validation'?'selected':''}>Check</option></select></td><td><button type="button" data-remove-landmark="${i}" aria-label="Remove landmark ${i+1}">Remove</button></td></tr>`).join('');
}
function drawCalibrationImage() {
  if(!calibrationImage?.size){$('#calibration-image').textContent='Original frame unavailable.';return;}
  const [w,h]=calibrationImage.size;
  const polygon=calibrationFit?.support_polygon_px || [];
  const points=calibrationDraft.landmarks;
  $('#calibration-image').innerHTML=`<svg role="img" aria-label="Camera calibration frame with landmark markers" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" style="display:block;width:auto;height:auto;max-width:100%;max-height:460px;cursor:crosshair"><image href="${esc(calibrationImage.url)}" width="${w}" height="${h}"/>${polygon.length?`<polygon points="${polygon.map(p=>p.join(',')).join(' ')}" fill="#00aabb" fill-opacity="0.12" stroke="#00aabb" stroke-width="2"/>`:''}${points.filter(p=>p.pixel.every(v=>v!==''&&Number.isFinite(Number(v)))).map(p=>`<g><circle cx="${p.pixel[0]}" cy="${p.pixel[1]}" r="6" fill="${p.use==='fit'?'#1464d2':'#f58b16'}" stroke="white" stroke-width="2"/><text x="${Number(p.pixel[0])+9}" y="${Number(p.pixel[1])-9}" font-size="16" fill="white" stroke="#172e26" stroke-width="3" paint-order="stroke">${esc(p.label)}</text></g>`).join('')}</svg>`;
}
function addLandmark(pixel=['','']) {
  preserveLandmarks();
  if(calibrationDraft.landmarks.length>=32){$('#calibration-message').textContent='Maximum 32 landmarks.';return;}
  calibrationDraft.landmarks.push({label:'Point '+(calibrationDraft.landmarks.length+1),pixel,plane_m:['',''],use:calibrationDraft.landmarks.filter(p=>p.use==='fit').length<4?'fit':'validation'});
  invalidateCalibrationPreview();renderLandmarkRows();drawCalibrationImage();
}
function invalidateCalibrationPreview() {
  calibrationFit=null;$('#calibration-result').innerHTML='';
  $('#calibration-message').textContent='Landmarks or settings changed. Check geometry before confirming.';
}
function calibrationFromForm() {
  if(!$('#calibration').reportValidity())throw Error('Complete the landmark coordinates and review fields.');
  const f=new FormData($('#calibration'));
  return {...calibrationDraft,landmarks:readLandmarks(),
    ...(calibrationDraft.transfer?{transfer:{...calibrationDraft.transfer,target_landmarks_reviewed:$('#calibration').elements.target_landmarks_reviewed.checked}}:{}),
    ...Object.fromEntries(['coordinate_system','dimension_basis','acceptance','basis'].map(k=>[k,f.get(k)])),
    ...Object.fromEntries(['start_seconds','end_seconds','max_validation_error_m'].map(k=>[k,Number(f.get(k))])),
    evidence:f.get('evidence').split(';').map(v=>v.trim()).filter(Boolean)};
}
$('#motions').addEventListener('click',e=>{const id=e.target.dataset.calibrate;if(!id)return;act(async()=>{
  const draft=await api('/games/'+encodeURIComponent(current.game_id)+'/skill-motion/'+encodeURIComponent(id)+'/calibration-draft');
  showCalibration(draft.document,draft);
});});
$('#calibrations').onclick=e=>{const id=e.target.dataset.editCalibration;if(!id)return;act(async()=>{
  const c=current.document.calibrations.find(c=>c.id===id);
  const result=await api('/games/'+encodeURIComponent(current.game_id)+'/skills/calibration-preview','POST',c);
  showCalibration(c,result);
});};
$('#calibration-image').onclick=e=>{const svg=e.target.closest('svg');if(!svg||!calibrationImage)return;
  const rect=svg.getBoundingClientRect(), [w,h]=calibrationImage.size;
  addLandmark([Math.min(w-1,Math.max(0,Math.round((e.clientX-rect.left)*w/rect.width*10)/10)),Math.min(h-1,Math.max(0,Math.round((e.clientY-rect.top)*h/rect.height*10)/10))]);
};
$('#calibration-add-point').onclick=()=>addLandmark();
$('#calibration-landmarks').onclick=e=>{if(e.target.dataset.removeLandmark===undefined)return;preserveLandmarks();calibrationDraft.landmarks.splice(Number(e.target.dataset.removeLandmark),1);invalidateCalibrationPreview();renderLandmarkRows();drawCalibrationImage();};
$('#calibration').oninput=()=>{preserveLandmarks();invalidateCalibrationPreview();drawCalibrationImage();};
async function calibrationAction(fn) {
  try {await fn();} catch(e) {$('#calibration-message').textContent=e.message;$('#status').textContent=e.message;}
}
$('#calibration-preview').onclick=()=>calibrationAction(async()=>{
  const draft=calibrationFromForm();
  const result=await api('/games/'+encodeURIComponent(current.game_id)+'/skills/calibration-preview','POST',draft);
  calibrationDraft=draft;calibrationFit=result.fit;drawCalibrationImage();
  $('#calibration-result').innerHTML=calibrationResultHtml(result);
  $('#calibration-message').textContent='Geometry checked. Preview has not been saved.';
});
$('#calibration').onsubmit=e=>{e.preventDefault();calibrationAction(async()=>{
  const draft=calibrationFromForm();
  await save({...current.document,calibrations:[...(current.document.calibrations || []).filter(c=>c.id!==draft.id),draft]});
  const result=current.calibration_reports.find(c=>c.id===draft.id);
  showCalibration(draft,result);$('#calibration-message').textContent='Calibration saved with review history.';
});};

let shotAimDraft=null, shotLineCalibrationDraft=null, measuredAimDraft=null;
function setShotAim(aim,lineCalibration=null) {
  shotAimDraft=aim?structuredClone(aim):null;shotLineCalibrationDraft=lineCalibration;
  $('#clear-shot-aim').hidden=!shotAimDraft;
  $('#shot-aim-notice').textContent=shotAimDraft?'Aim is linked to reviewed camera and path evidence; it is recomputed when saved.':'';
  $('#shot').elements.line_error_degrees.readOnly=Boolean(shotAimDraft);
}
$('#clear-shot-aim').onclick=()=>{setShotAim(null);$('#shot').elements.line_error_degrees.value='';};
function renderAim() {
  const selected=$('#aim-shot').value;
  $('#aim-shot').innerHTML=options(current.document.shots.map(s=>[s.id,`${clock(s.seconds)} · ${s.side} · ${participantLabel(s.participant)}`]));
  if(current.document.shots.some(s=>s.id===selected))$('#aim-shot').value=selected;
  $('#aim-availability').textContent=current.stale?'Game evidence changed; review it before measuring aim.':
    'Choose a saved shot and a confirmed camera map. Inspect the original motion and pre-shot call evidence before confirming the checkboxes.';
  loadAimShot();
}
function loadAimShot() {
  const shot=current.document.shots.find(s=>s.id===$('#aim-shot').value);
  const form=$('#aim');form.reset();$('#aim-shot').value=shot?.id || '';
  const cals=(current.document.calibrations || []).filter(c=>c.acceptance==='confirmed'&&c.source_id===shot?.source_id);
  form.hidden=!shot || !cals.length || current.stale;
  $('#aim-calibration').innerHTML=options(cals.map(c=>[c.id,`${c.view.replaceAll('_',' ')} · ${clock(c.start_seconds)}–${clock(c.end_seconds)}`]));
  if(shot?.aim)$('#aim-calibration').value=shot.aim.calibration_id;
  fillAimTracks();
  if(shot?.aim){
    const a=shot.aim;
    $('#aim-track').value=a.track_id;
    for(const key of ['release_seconds','start_seconds','end_seconds','call_seconds','acceptance','basis'])form.elements[key].value=a[key];
    for(const key of ['reviewed_free_travel','reviewed_stone_association','reviewed_called_line'])form.elements[key].checked=a[key];
    for(const [prefix,point] of [['origin',a.line_origin_m],['broom',a.broom_m],['right',a.right_side_m]])for(const [i,axis] of ['x','y'].entries())form.elements[prefix+'_'+axis].value=point[i];
    form.elements.evidence.value=a.evidence.join('; ');
  }
  if(!shot || !cals.length)$('#aim-availability').textContent='Save a shot and confirm a camera calibration to enable measured aim. The existing shot form still supports manually reviewed observations.';
}
function fillAimTracks() {
  const c=(current.document.calibrations || []).find(c=>c.id===$('#aim-calibration').value);
  const m=current.motions.find(m=>m.id===c?.motion_id);
  const tracks=m?.image_motion_tracks || [];
  $('#aim-track').innerHTML=options(tracks.map((t,i)=>[t.id,`Fragment ${i+1} · ${t.color} · ${t.start_seconds.toFixed(2)}–${t.end_seconds.toFixed(2)}s`]));
  const priorities=new Set((m?.delivery_path_proposals || []).filter(p=>p.status==='review_first').map(p=>p.track_id));
  const preferred=tracks.find(t=>priorities.has(t.id)&&t.relative_release_phase==='after_proposed_release') || tracks.find(t=>priorities.has(t.id)) || tracks.find(t=>t.relative_release_phase==='after_proposed_release'&&t.matches_delivery_color);
  if(preferred)$('#aim-track').value=preferred.id;
  $('#aim-coordinate-system').textContent=c?'Sheet coordinates: '+c.coordinate_system:'';
  $('#preview-aim').disabled=!c || !tracks.length || current.stale;
  fillAimWindow();
}
function fillAimWindow() {
  const c=(current.document.calibrations || []).find(c=>c.id===$('#aim-calibration').value);
  const m=current.motions.find(m=>m.id===c?.motion_id);
  const t=m?.image_motion_tracks.find(t=>t.id===$('#aim-track').value);
  const form=$('#aim'), release=m?.delivery_anchor?.release_seconds;
  form.elements.release_seconds.value=release ?? '';
  form.elements.start_seconds.value=t?Math.max(t.start_seconds,release ?? t.start_seconds):'';
  form.elements.end_seconds.value=t?Math.min(t.end_seconds,Number(form.elements.start_seconds.value)+0.8):'';
  measuredAimDraft=null;$('#apply-aim').disabled=true;$('#aim-result').innerHTML='';
  $('#aim-message').textContent=release!==null&&release!==undefined?'Release time is a proposal. Review it and the selected path before accepting aim.':'';
}
$('#aim-shot').onchange=loadAimShot;
$('#aim-calibration').onchange=fillAimTracks;
$('#aim-track').onchange=fillAimWindow;
$('#aim').oninput=()=>{measuredAimDraft=null;$('#apply-aim').disabled=true;$('#aim-result').innerHTML='';};
$('#aim').onsubmit=e=>{e.preventDefault();(async()=>{
  try {
    const f=new FormData(e.target),shot=current.document.shots.find(s=>s.id===f.get('shot_id'));
    const c=current.document.calibrations.find(c=>c.id===f.get('calibration_id'));
    const report=current.calibration_reports.find(r=>r.id===c?.id);
    if(!shot || !report?.calibration_sha256)throw Error('Choose a current saved shot and camera map.');
    const aim={calibration_id:c.id,calibration_sha256:report.calibration_sha256,track_id:f.get('track_id'),
      ...Object.fromEntries(['release_seconds','start_seconds','end_seconds','call_seconds'].map(k=>[k,Number(f.get(k))])),
      ...Object.fromEntries(['reviewed_free_travel','reviewed_stone_association','reviewed_called_line'].map(k=>[k,f.has(k)])),
      ...Object.fromEntries(['acceptance','basis'].map(k=>[k,f.get(k)])),
      line_origin_m:['x','y'].map(v=>Number(f.get('origin_'+v))),broom_m:['x','y'].map(v=>Number(f.get('broom_'+v))),right_side_m:['x','y'].map(v=>Number(f.get('right_'+v))),
      evidence:f.get('evidence').split(';').map(v=>v.trim()).filter(Boolean)};
    const result=await api('/games/'+encodeURIComponent(current.game_id)+'/skills/aim-preview','POST',{...shot,aim});
    const m=result.measurement;
    $('#aim-message').textContent=m.status==='unavailable'?m.reason.replaceAll('_',' '):'Preview only. Use it in the shot draft, then save the shot to retain the reviewed measurement.';
    $('#aim-result').innerHTML=m.status==='unavailable'?'':`<table><tr><th>Right + / left −</th><th>Wide + / narrow −</th><th>Start / end right offset</th><th>Observed samples</th><th>Early-to-late heading change</th></tr><tr><td>${m.line_error_degrees.toFixed(3)}°</td><td>${m.wide_error_degrees===null?'Unknown turn':m.wide_error_degrees.toFixed(3)+'°'}</td><td>${m.start_right_offset_m.toFixed(3)} / ${m.end_right_offset_m.toFixed(3)} m</td><td>${m.sample_count} · ${m.sample_span_seconds.map(s=>s.toFixed(2)).join('–')}s</td><td>${m.heading_change_degrees===null?'Unavailable':m.heading_change_degrees.toFixed(3)+'°'}</td></tr></table><p>${esc(m.notice)}</p><p class="muted">Path-fit residual ${m.fit_residual_rms_m.toFixed(3)} m; maximum two-pixel position sensitivity ${m.maximum_two_pixel_sensitivity_m.toFixed(3)} m. Direction error and lateral offset are separate: a parallel path can have zero angle while missing the called line. These diagnostics are not a confidence interval or a validated player rating.</p>`;
    measuredAimDraft=result.draft;$('#apply-aim').disabled=m.status!=='experimental_line_measurement'||aim.acceptance!=='confirmed';
  } catch(e) {$('#aim-message').textContent=e.message;}
})();};
$('#apply-aim').onclick=()=>{
  if(!measuredAimDraft)return;
  const draft=measuredAimDraft;$('#shot').reset();setShotAim(draft.aim,draft.line_calibration);
  for(const [key,value] of Object.entries(draft))if($('#shot').elements[key])$('#shot').elements[key].value=key==='evidence'?value.join('; '):(value??'');
  $('#shot').scrollIntoView();$('#status').textContent='Reviewed aim copied to the shot draft. Save the shot to retain it; existing weight and position decisions are preserved.';
};

$('#motions').addEventListener('click',e=>{const id=e.target.dataset.refineMotion;if(!id)return;act(async()=>{
  e.target.disabled=true;
  try {
    const result=await api('/skill-motion/'+encodeURIComponent(id)+'/refine','POST',{});await load();
    const detail=$('#motions details[data-motion-id="'+CSS.escape(result.motion_id)+'"]');
    if(detail){detail.open=true;detail.scrollIntoView({block:'start'});}
  }
  finally {e.target.disabled=false;}
});});

function renderMotionBatches() {
  const opened=new Set([...$('#motion-batches').querySelectorAll('details[open]')].map(d=>d.dataset.batchId));
  $('#motion-batches').innerHTML=motionBatchState.map(b=>`<details data-batch-id="${esc(b.id)}" ${opened.has(b.id)?'open':''}><summary>Local motion · ${esc(b.state)} · ${b.units.filter(u=>u.state==='succeeded').length}/${b.units.length} clips completed</summary><p>${esc(b.plan.notice)} Reload to load new motion results.</p>${b.pause_reason?`<p>${esc(b.pause_reason)}</p>`:''}${b.state==='active'?`<button data-motion-batch="${esc(b.id)}" data-control="pause">Pause local motion</button>`:''}${b.state==='paused'?`<button data-motion-batch="${esc(b.id)}" data-control="resume">Resume local motion</button>`:''}${['active','paused'].includes(b.state)?` <button data-motion-batch="${esc(b.id)}" data-control="cancel">Cancel remaining motion clips</button>`:''}<div class="scroll"><table><tr><th>Delivery</th><th>Camera / clip</th><th>State</th></tr>${b.units.map(u=>`<tr><td>${esc(u.stone_color)}${u.release_known?'':' · release unknown'}</td><td>${esc(u.request.view.replaceAll('_',' '))} · ${clock(u.request.start_seconds)}–${clock(u.request.end_seconds)}</td><td>${esc(u.state.replaceAll('_',' '))}${u.error?': '+esc(u.error):''}</td></tr>`).join('')}</table></div></details>`).join('') || '<p>No local motion batches yet.</p>';
}
$('#motion-batch-plan').onsubmit=e=>{e.preventDefault();act(async()=>{
  const gameId=current.game_id, request={max_candidates:Number(e.target.elements.max_candidates.value)};
  const result=await api('/games/'+encodeURIComponent(gameId)+'/skill-motion-batches/preview','POST',request);
  if(current.game_id!==gameId)return;
  motionBatchPreview={gameId,request,...result};
  const p=result.plan, counts={};p.omitted.forEach(o=>counts[o.reason]=(counts[o.reason] || 0)+1);
  $('#motion-batch-preview').innerHTML=`<p><strong>${p.units.length} new clips</strong> · up to ${p.maximum_decoded_samples} sampled frames · no OpenAI calls.</p><p>${Object.entries(counts).map(([k,n])=>`${n} ${esc(k.replaceAll('_',' '))}`).join('; ') || 'All supported candidates included.'}</p><ul>${p.units.map(u=>`<li>${esc(u.stone_color)} · ${clock(u.request.start_seconds)}–${clock(u.request.end_seconds)} · ${esc(u.request.view.replaceAll('_',' '))}${u.release_known?'':' · release unknown; clip follows delivery action'}</li>`).join('')}</ul>${p.units.length?'<button id="start-motion-batch" type="button">Start local motion batch</button>':'<p>No new clips to process in this plan.</p>'}`;
});};
$('#motion-batch-preview').onclick=e=>{if(e.target.id!=='start-motion-batch')return;act(async()=>{
  const preview=motionBatchPreview;if(!preview || current.game_id!==preview.gameId)return;
  e.target.disabled=true;
  try {
    await api('/games/'+encodeURIComponent(preview.gameId)+'/skill-motion-batches','POST',{request:preview.request,plan_sha256:preview.plan_sha256});
    if(current.game_id!==preview.gameId)return;
    motionBatchPreview=null;$('#motion-batch-preview').innerHTML='<p>Local motion batch started. Progress appears below.</p>';
    motionBatchState=(await api('/games/'+encodeURIComponent(preview.gameId)+'/skill-motion-batches')).batches;renderMotionBatches();
  } finally {e.target.disabled=false;}
});};
$('#motion-batches').onclick=e=>{const id=e.target.dataset.motionBatch, action=e.target.dataset.control;if(!id || !action)return;act(async()=>{
  const gameId=current.game_id, batch=motionBatchState.find(b=>b.id===id);
  await api('/skill-motion-batches/'+encodeURIComponent(id)+'/control','POST',{action,expected_revision:batch.revision});
  const result=await api('/games/'+encodeURIComponent(gameId)+'/skill-motion-batches');
  if(current.game_id===gameId){motionBatchState=result.batches;renderMotionBatches();}
});};
setInterval(async()=>{
  if(!current || !motionBatchState.some(b=>b.state==='active'))return;
  const gameId=current.game_id;
  try {
    const result=await api('/games/'+encodeURIComponent(gameId)+'/skill-motion-batches');
    if(current.game_id===gameId){motionBatchState=result.batches;renderMotionBatches();}
  } catch(e){$('#status').textContent=e.message;}
},5000);

function showCameraComparison(result) {
  $('#camera-comparison-result').innerHTML=`<p><strong>${esc((result.reason || result.status).replaceAll('_',' '))}</strong>${result.maximum_corner_shift_px!==undefined?` · maximum predicted corner shift ${result.maximum_corner_shift_px.toFixed(2)} px`:''}</p>${result.held_out_count?`<p>${result.fit_inlier_count} agreeing fit matches; ${result.held_out_count} held-out image matches, 90th-percentile residual ${result.held_out_p90_error_px.toFixed(2)} px.</p>`:''}<p>${esc(result.notice)}</p>${result.landmarks_outside_feature_support?.length?`<p>Landmark predictions outside the matched feature region: ${result.landmarks_outside_feature_support.map(esc).join(', ')}. Inspect these especially carefully.</p>`:''}<div class="row"><figure><img src="${esc(result.reference_image_url)}" style="max-width:100%;width:300px" alt="Original camera reference"><figcaption>Reference</figcaption></figure><figure><img src="${esc(result.target_image_url)}" style="max-width:100%;width:300px" alt="Target camera frame"><figcaption>Target</figcaption></figure></div>`;
}
$('#camera-comparison').onsubmit=e=>{e.preventDefault();act(async()=>{
  const gameId=current.game_id;
  const result=await api('/games/'+encodeURIComponent(gameId)+'/skill-motion/'+encodeURIComponent($('#camera-target').value)+'/camera-comparison','POST',{reference_motion_id:$('#camera-reference').value});
  if(current.game_id===gameId)showCameraComparison(result);
});};
$('#camera-transfer').onsubmit=e=>{e.preventDefault();act(async()=>{
  const gameId=current.game_id, parent=current.document.calibrations.find(c=>c.id===$('#camera-parent').value);
  const report=current.calibration_reports.find(r=>r.id===parent.id);
  if(!report?.calibration_sha256)throw Error('Original calibration is unavailable; review its evidence.');
  const result=await api('/games/'+encodeURIComponent(gameId)+'/skills/calibration-transfer','POST',{parent_id:parent.id,parent_sha256:report.calibration_sha256,target_motion_id:$('#camera-target').value});
  if(current.game_id!==gameId)return;
  showCameraComparison(result.registration);
  if(result.document)showCalibration(result.document,result);
});};

function renderSegmentHierarchy() {
  const h=segmentHierarchy,rule=h.direction_rule;
  $('#end-direction').elements.odd_delivery_view.value=rule.odd_delivery_view || '';
  $('#end-direction').elements.basis.value=rule.basis;
  $('#segment-export').href='/api/games/'+encodeURIComponent(current.game_id)+'/skill-segments';
  $('#segment-hierarchy').innerHTML=`<p>${h.delivery_count} delivery candidates · ${esc(h.game.coverage)} game coverage. ${esc(h.notice)}</p>`+h.end_groups.map(g=>`<details><summary>${g.end?'End '+g.end:'End number unknown'} · ${g.parity || 'parity unknown'} · ${esc(g.delivery_view?.replaceAll('_',' ') || 'origin unknown')} · ${g.deliveries.length} throws</summary><p>Observed deliveries: ${clock(g.observed_start_seconds)}–${clock(g.last_delivery_observed_seconds)}. This interval does not establish when the final stone stops.</p>${g.preceding_boundary?`<p>Possible boundary: ${clock(g.preceding_boundary.earliest_seconds)}–${clock(g.preceding_boundary.latest_seconds)} · ${esc(g.preceding_boundary.reason.replaceAll('_',' '))}.</p>`:''}${g.issues.length?`<p>${g.issues.map(v=>esc(v.replaceAll('_',' '))).join('; ')}</p>`:''}<div class="scroll"><table><tr><th>Delivery label</th><th>Observed start</th><th>Release proposals</th><th>Stone / action finish</th><th>Review</th></tr>${g.deliveries.map(d=>`<tr><td>${esc(d.side)} · ${d.position?`${esc(d.position)} · stone ${d.stone_in_position}`:'position / stone number unknown'}${d.end?' · end '+d.end:''}</td><td>${clock(d.start_seconds)}</td><td>${d.release_seconds_options.length?d.release_seconds_options.map(clock).join(', '):'Unknown'}</td><td>Not observed</td><td><button data-segment-candidate="${esc(d.id)}">Review delivery</button><br>${esc(d.origin.status.replaceAll('_',' '))}</td></tr>`).join('')}</table></div></details>`).join('');
}
$('#end-direction').onsubmit=e=>{e.preventDefault();act(async()=>{
  const f=e.target.elements;
  await api('/skills/end-direction','PUT',{expected_revision:segmentHierarchy.direction_rule.revision,odd_delivery_view:f.odd_delivery_view.value || null,basis:f.basis.value});
  const gameId=current.game_id, result=await api('/games/'+encodeURIComponent(gameId)+'/skill-segments');
  if(current.game_id===gameId){segmentHierarchy=result;renderSegmentHierarchy();}
});};
$('#segment-hierarchy').onclick=e=>{const id=e.target.dataset.segmentCandidate;if(!id)return;const c=current.delivery_candidates.find(c=>c.id===id);if(c)reviewCandidate(c);};
