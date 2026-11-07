(() => {
  const esc=escapeHTML, enc=encodeURIComponent;
  const weekdays=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"];
  let preview=null, request=null, startId=null, loading=false;
  const button=document.createElement('button');
  button.id='batch-work-button'; button.className='button primary'; button.textContent='Batch add & analyze';
  $('#import-button').before(button); $('#import-button').className='button secondary';
  const host=document.createElement('dialog'); host.id='batch-work-dialog'; host.className='workspace-dialog';
  host.innerHTML=`<div class="dialog-heading"><div><h2>Add & analyze a date range</h2><p>One setup for many recordings. Work continues while the server is running.</p></div><button class="close-dialog" aria-label="Close batch work">×</button></div>
    <form id="batch-work-form"><div class="form-grid"><label>From date (inclusive)<input type="date" name="start_date" required></label><label>Through date (inclusive)<input type="date" name="end_date" required></label></div>
    <label>Video title contains (optional)<input name="title_filter" type="text" maxlength="500" placeholder="e.g. Koski" aria-describedby="batch-title-help"></label>
    <p id="batch-title-help" class="fine-print">Only videos whose titles contain this text will be downloaded and processed by this batch. Matching ignores capitalization. Leave blank to include all titles within the other filters.</p>
    <fieldset><legend>Sheets</legend><div class="batch-actions">${Array.from({length:8},(_,i)=>`<label class="check-label"><input name="sheets" type="checkbox" value="${i+1}" checked> ${i+1}</label>`).join('')}</div></fieldset>
    <fieldset><legend>Days of the week</legend><div class="batch-actions">${weekdays.map((day,i)=>`<label class="check-label"><input type="checkbox" name="weekdays" value="${i}" checked> ${day}</label>`).join('')}</div></fieldset>
    <label>Where to look<select name="scope"><option value="feed">Add from a YouTube channel or playlist</option><option value="existing">Only recordings already in this archive</option></select></label>
    <label id="batch-feed-label">Channel or playlist URL (Wausau preset)<input name="feed_url" type="url" list="batch-known-feeds" placeholder="https://www.youtube.com/@channel/streams" value="https://www.youtube.com/@curlingstadiumwausau/streams" required></label><datalist id="batch-known-feeds"></datalist>
    <p class="fine-print">Dates come from recording titles. Unknown dates receive a metadata lookup; unresolved dates and unselected sheets are excluded from analysis. Channel listing may add metadata outside the range; only matching recordings are analyzed. Already completed analyses are skipped and active analyses are reused.</p>
    <details><summary>Limits per recording</summary><div class="form-grid"><label>Processing minutes<input name="minutes" type="number" min="1" max="1440" value="240" required></label><label>Additional retained data (MB)<input name="mb" type="number" min="32" max="4096" value="512" required></label></div><p class="fine-print">These limits apply to every recording, not the batch total. Jobs pause when they reach a limit.</p></details>
    <button class="button secondary" type="submit">Preview matching recordings</button></form>
    <div id="batch-work-preview"></div><p id="batch-work-status" role="status"></p><button class="button primary" id="batch-work-start" disabled>Start batch</button>
    <h3>Batch progress</h3><div id="batch-work-history"></div>`;
  document.body.append(host);
  function readRequest() {
    const d=new FormData($('#batch-work-form'));
    return {start_date:d.get('start_date'),end_date:d.get('end_date'),title_filter:d.get('title_filter').trim(),sheets:d.getAll('sheets').map(Number),weekdays:d.getAll('weekdays').map(Number),feed_url:d.get('scope')==='feed' ? d.get('feed_url').trim() : null,max_worker_seconds:Number(d.get('minutes'))*60,max_image_growth_mb:Number(d.get('mb'))};
  }
  async function progress() {
    if(loading)return;loading=true;
    try {
      const data=await api('/recording-campaigns');
      $('#batch-work-history').innerHTML=data.campaigns.reverse().map(c=>`<article class="review-task-card"><h3>${esc(c.request.start_date)} – ${esc(c.request.end_date)} · ${esc({active:'Running',paused:'Paused',completed:'Processing finished',attention:'Needs attention',cancelled:'Cancelled'}[c.state] || c.state)}</h3>${c.request.title_filter?`<p>Video title contains: <strong>${esc(c.request.title_filter)}</strong></p>`:''}<p>Sheets ${c.request.sheets.join(', ')} · ${(c.request.weekdays || (c.request.sundays_only ? [6] : [0,1,2,3,4,5,6])).map(i=>weekdays[i]).join(', ')}</p><p>${Object.entries(c.counts).map(([k,n])=>`${n} ${{active:'queued / running',completed:'processing finished',metadata:'fetching metadata',attention:'need attention'}[k] || k}`).join(' · ') || 'Waiting to find matching recordings'}</p>${c.feed_id?`<p>Channel listing: ${esc(c.discovery_state.replaceAll('_',' '))}${c.discovery_problem?` · ${esc(c.discovery_problem)}`:''}</p>`:''}
      <div class="batch-actions">${['active','paused','attention'].includes(c.state)?`<button class="button secondary" data-campaign-control="${c.state==='active'?'pause':'resume'}" data-campaign-id="${esc(c.id)}">${c.state==='active'?'Pause batch':'Resume batch'}</button><button class="button quiet" data-campaign-control="cancel" data-campaign-id="${esc(c.id)}">Cancel remaining work</button>`:''}<button class="button quiet" data-batch-jobs>Individual jobs & limits</button></div>${c.control_errors.map(e=>`<p class="job-error">${esc(e)}</p>`).join('')}
      <details><summary>Recordings · ${Object.keys(c.items).length}</summary>${Object.values(c.items).map(i=>`<p><button class="button quiet" data-batch-source="${esc(i.source_id)}">${esc(i.title)}</button> · ${esc(i.state)}${i.owned===false?' · existing analysis':''}${i.problem?`<br><span class="job-error">${esc(i.problem)}</span>`:''}</p>`).join('')}</details></article>`).join('') || '<p>No batches yet.</p>';
      if(state.data.worker?.running===false) $('#batch-work-history').insertAdjacentHTML('afterbegin','<p class="callout">Worker offline. Start the server with workers to process saved batches.</p>');
    } catch(error) {$('#batch-work-history').textContent=error.message;} finally {loading=false;}
  }
  $('#batch-work-button').addEventListener('click',async()=>{
    host.showModal();
    if(!$('#batch-work-form').elements.start_date.value) {
      $('#batch-work-form').elements.start_date.value=state.dateFrom;
      $('#batch-work-form').elements.end_date.value=state.dateThrough;
      $$('#batch-work-form input[name="weekdays"]').forEach(input=>{input.checked=state.weekdays.includes(Number(input.value));});
    }
    await progress();
    try {const {feeds}=await api('/discovery');$('#batch-known-feeds').innerHTML=feeds.map(f=>`<option value="${esc(f.url)}">`).join('');if(feeds.length===1 && !$('#batch-work-form').elements.feed_url.value)$('#batch-work-form').elements.feed_url.value=feeds[0].url;}catch(error){$('#batch-work-status').textContent=error.message;}
  });
  $('#batch-work-form').addEventListener('input',()=>{
    preview=null;request=null;startId=null;$('#batch-work-start').disabled=true;$('#batch-work-preview').textContent='';
    const form=$('#batch-work-form'), feed=form.elements.scope.value==='feed';
    $('#batch-feed-label').hidden=!feed;form.elements.feed_url.required=feed;
  });
  $('#batch-work-form').addEventListener('submit',async event=>{
    event.preventDefault();event.stopPropagation();const b=event.submitter;b.disabled=true;
    const candidate=readRequest(); $('#batch-work-status').textContent='Checking the archive…';
    try {
      const result=await api('/recording-campaigns/preview','POST',candidate);
      if(JSON.stringify(candidate)!==JSON.stringify(readRequest()))return;
      request=candidate;preview=result;startId=crypto.randomUUID();
      const fresh=result.items.filter(i=>i.status==='will_analyze').length;
      $('#batch-work-preview').innerHTML=`<article class="review-task-card"><h3>${result.items.length} matching recordings already known</h3>${request.title_filter?`<p>Video title contains: <strong>${esc(request.title_filter)}</strong> (ignoring capitalization).</p>`:''}<p>${fresh} to analyze · ${result.items.length-fresh} existing analyses · ${result.hours.toFixed(1)} hours of known video</p>${result.discovery?'<p>The batch will keep listing the channel and automatically add matching recordings it finds. The total can increase until the visible listing ends.</p>':''}<p>Excluded title words: ${result.excluded_words.map(esc).join(', ') || 'none'}.</p><p>Each recording is analyzed for game and score proposals. Automatic face finding follows when enabled on this server. Scores, identities and player measurements still need evidence and review.</p><details><summary>Preview recordings</summary>${result.items.map(i=>`<p>${esc(i.title)} · ${esc(i.status.replaceAll('_',' '))}</p>`).join('') || '<p>No matches found in the current archive.</p>'}</details></article>`;
      $('#batch-work-start').disabled=!result.discovery && !result.items.length;
      $('#batch-work-status').textContent='Preview ready. Start batch to queue the work.';
    } catch(error) {$('#batch-work-status').textContent=error.message;}finally{b.disabled=false;}
  });
  $('#batch-work-start').addEventListener('click',async event=>{
    if(!preview || !request)return;event.currentTarget.disabled=true;
    try {await api('/recording-campaigns','POST',{id:startId,request});$('#batch-work-status').textContent='Batch saved. You can close this window; keep the server running. Pause stops new admissions and pauses analyses created by this batch. An in-progress listing or metadata request may finish.';preview=null;await progress();}
    catch(error){$('#batch-work-status').textContent=error.message;event.currentTarget.disabled=false;}
  });
  host.addEventListener('click',async event=>{
    const b=event.target.closest('button');if(!b)return;
    if(b.hasAttribute('data-batch-jobs')){host.close();window.hoglineRecordings.openJobs();return;}
    if(b.dataset.batchSource){host.close();const target=$(`[data-source="${b.dataset.batchSource}"]`);if(target)target.click();else{state.source=b.dataset.batchSource;state.seconds=0;state.draft=null;$("#game-editor").hidden=true;await refreshSelectedWorkspace();render();}return;}
    if(b.dataset.campaignControl){b.disabled=true;try{await api(`/recording-campaigns/${enc(b.dataset.campaignId)}/control`,'POST',{action:b.dataset.campaignControl});await progress();}catch(error){$('#batch-work-status').textContent=error.message;b.disabled=false;}}
  });
  setInterval(()=>{if(host.open && !document.hidden)progress();},5000);
})();
