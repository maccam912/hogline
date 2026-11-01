/* Task-oriented recording workspace. Decisions use the existing revision-checked APIs. */
(() => {
  const esc = escapeHTML, enc = encodeURIComponent;
  const filters=document.createElement('details'); filters.className='library-filters';
  filters.innerHTML='<summary>Filter recordings by date, weekday or sheet</summary>';
  ['#sheet-filters','#recording-date-filters'].forEach(selector=>filters.append($(selector)));
  $('#search').after(filters);
  const panel = document.createElement('section');
  panel.id = 'recording-next';
  $('#recording-progress').before(panel);
  const dialogs = document.createElement('div');
  dialogs.innerHTML = `<dialog id="quick-review-dialog" class="workspace-dialog"><div class="dialog-heading"><h2 id="quick-review-title">Quick review</h2><button class="close-dialog" aria-label="Close quick review">×</button></div><div id="quick-review-body"></div><p id="quick-review-status" role="status"></p></dialog>
    <dialog id="recording-reset-dialog"><div class="dialog-heading"><h2>Clear automatic analysis</h2><button class="close-dialog" aria-label="Close clear analysis">×</button></div><div id="reset-preview"></div><p id="reset-status" role="status"></p><div class="batch-actions"><button class="button secondary" id="reset-clear" disabled>Clear analysis</button><button class="button primary" id="reset-restart" disabled>Clear & start again</button></div></dialog>
    <dialog id="game-performance-dialog" class="workspace-dialog"><div class="dialog-heading"><h2>Player results</h2><button class="close-dialog" aria-label="Close player results">×</button></div><div id="game-performance"></div></dialog>`;
  [...dialogs.children].forEach(d => document.body.append(d));
  let renderKey = '', review = null, reset = null, proposal = null, performanceGeneration = 0;
  function render() {
    if (!state.source) return;
    const games = sourceGames(), batches = (state.data.batches || []).filter(b => b.request.source_ids.includes(state.source));
    const key = JSON.stringify([state.source, source()?.title_teams, games, batches.map(b => [b.id,b.state]), state.data.review_tasks]);
    if (key === renderKey) return;
    renderKey = key;
    const pending = games.reduce((n,g) => n + Number(g.acceptance !== 'human_confirmed') + g.ends.filter(e => e.acceptance !== 'human_confirmed' && ['played','blank'].includes(e.state)).length, 0);
    panel.innerHTML = `${source()?.title_teams?.length ? `<p><strong>Teams: ${source().title_teams.map(esc).join(" vs ")}</strong><br><span class="fine-print">From the recording title · stone colors and player assignments need review.</span></p>` : ""}<div class="workspace-actions"><button class="button primary" data-work-action="review">Review decisions</button><button class="button secondary" data-work-action="players">Players & results</button><button class="button secondary" data-work-action="faces">Find / redo faces</button><details class="recording-actions"><summary>Recording actions</summary><div><button class="button quiet" data-work-action="reset">Clear analysis / start again</button><button class="button quiet" data-open-jobs>Jobs & progress</button><button class="button quiet" data-work-action="help">How this works</button></div></details></div>
      <p class="workspace-next">${pending ? `${pending} ${pending === 1 ? "decision" : "decisions"} on saved games ready to inspect. Review one boundary or end at a time using its frames and timestamp links.` : games.length ? 'Open Players & results to compare the saved observations for one game.' : 'Start with Analyze recording. When processing finishes, inspect the proposed scores and confirm the game boundaries.'}</p>`;
  }
  function frame(id, caption) {
    const f = state.data.evidence.find(f => f.id === id);
    return `<figure class="quick-frame"><img loading="lazy" src="/api/evidence/${enc(id)}/composite" alt="${esc(caption)}"><figcaption>${esc(caption)}${f ? ` · ${time(f.source_seconds)}` : ''}${f?.youtube_url ? ` · <a href="${esc(f.youtube_url)}" target="_blank" rel="noopener">Watch this moment ↗</a>` : ''}</figcaption></figure>`;
  }
  function listReviews() {
    review = null;
    $('#quick-review-title').textContent = 'Review this recording';
    $('#quick-review-status').textContent = '';
    const proposed=window.hoglineRecordings.proposedGames() || [];
    $('#quick-review-body').innerHTML = `<p>Choose one decision. Confirming a boundary does not confirm its scores or final result.</p>${proposed.map((book,i)=>`<article class="review-task-card"><h3>Proposed game ${i+1} · end scores</h3><p>Evidence ${time(book.first_seconds)}–${time(book.last_seconds)}. Assign a score to a saved game before confirming.</p><div class="batch-actions">${book.ends.map((e,j)=>`<button class="button secondary" data-review-book="${i}" data-review-end="${j}">End ${e.official_number ?? e.observed_index ?? "?"} · ${e.points?.red ?? "?"}–${e.points?.yellow ?? "?"}</button>`).join('')}</div></article>`).join('')}<h3>Saved games</h3>${sourceGames().map(g => `<article class="review-task-card"><h3>${esc(g.label)}</h3><div class="batch-actions"><button class="button secondary" data-quick-game="${esc(g.id)}">${g.acceptance === 'human_confirmed' ? 'Adjust' : 'Review'} game boundaries</button>${g.ends.filter(e => ['played','blank'].includes(e.state)).map(e => `<button class="button secondary" data-quick-game="${esc(g.id)}" data-quick-end="${esc(e.id)}">End ${e.official_number ?? e.observed_index} · ${e.red_points}–${e.yellow_points}${e.acceptance === 'human_confirmed' ? ' · reviewed' : ''}</button>`).join('')}<button class="button quiet" data-full-game="${esc(g.id)}">Full game editor</button></div></article>`).join('') || '<p>No saved game intervals yet. Open the score evidence below to inspect proposed ends, then use “Add game interval” in More tools to establish the game.</p>'}<button class="button quiet" data-review-evidence>Open proposed score evidence</button>`;
  }
  function openQuick(gameId, endId, proposedEnd = null) {
    const raw = state.data.games.find(g => g.id === gameId);
    if (!raw) throw Error('Game changed. Refresh the recording.');
    review = {game: cleanGame(raw), revision: raw.revision, endId, sourceId: state.source};
    let end = raw.ends.find(e => e.id === endId);
    if (proposedEnd) {
      const existing = review.game.ends.find(e => e.official_number != null && e.official_number === proposedEnd.official_number);
      const eligible=proposedEnd.evidence_ids.filter(id=>{
        const f=state.data.evidence.find(f=>f.id===id);
        return f && raw.spans.some(s=>s.source_id===f.source_id && f.source_seconds>=s.start_seconds && f.source_seconds<=s.end_seconds);
      });
      end = {...proposedEnd, evidence_ids:eligible.slice(0,4), id: existing?.id || crypto.randomUUID(), observed_index: existing?.observed_index || Math.max(0,...review.game.ends.map(e=>e.observed_index))+1};
      if(existing) review.game.ends[review.game.ends.indexOf(existing)] = end; else review.game.ends.push(end);
      review.endId=end.id;
    }
    $('#quick-review-title').textContent = end ? `Review end ${end.official_number ?? end.observed_index}` : 'Review game boundaries';
    const spans = raw.spans.filter(s => s.source_id === state.source);
    const nearest = seconds => [...state.data.evidence].filter(f => f.source_id === state.source).sort((a,b) => Math.abs(a.source_seconds-seconds)-Math.abs(b.source_seconds-seconds))[0];
    const evidence = end ? end.evidence_ids.slice(0,4).map(id => frame(id, 'Supporting score frame')).join('') : spans.flatMap(s => [s.start_seconds,s.end_seconds]).map((seconds,i) => {
      const f = nearest(seconds);
      return `<div>${f ? frame(f.id, i % 2 ? 'Nearest saved frame to end boundary' : 'Nearest saved frame to start boundary') : '<p>No saved frame near this boundary.</p>'}<a href="${esc(youtube(source(),seconds) || `/api/media/${enc(state.source)}#t=${seconds}`)}" target="_blank" rel="noopener">Inspect ${i % 2 ? 'end' : 'start'} at ${time(seconds)} ↗</a></div>`;
    }).join('');
    $('#quick-review-body').innerHTML = `<button class="button quiet" data-quick-back>← All decisions</button><h3>${esc(raw.label)}</h3><div class="quick-evidence">${evidence || '<p>No supporting frame is available within this game’s boundaries. Choose the correct game, adjust its boundaries, or use the full editor to attach evidence.</p>'}</div><form id="quick-review-form">${end ? `<div class="form-grid"><label>Red points<input name="red" type="number" required min="0" max="8" value="${end.red_points}"></label><label>Yellow points<input name="yellow" type="number" required min="0" max="8" value="${end.yellow_points}"></label></div><p>Confirm the points for this end only. A posted score does not prove that the game is finished.</p>` : `<p>Check the start and end moments. Times are within this recording; other linked recordings stay as saved.</p>${spans.map((s,i) => `<div class="form-grid"><label>Start (HH:MM:SS)<input name="start-${i}" required value="${time(s.start_seconds)}"></label><label>End (HH:MM:SS)<input name="end-${i}" required value="${time(s.end_seconds)}"></label></div>`).join('')}`}
      <label>Decision<select name="decision"><option value="human_confirmed">Confirm after inspecting evidence</option><option value="disputed">Mark disputed / needs more evidence</option></select></label><label>Reviewer<input name="author" required maxlength="120" value="${esc(localStorage.getItem('hogline-reviewer') || 'Local operator')}"></label><label>Reason<input name="reason" required maxlength="2000" placeholder="What did you check or correct?"></label><div class="batch-actions"><button class="button primary" type="submit" ${end && !end.evidence_ids.length ? 'disabled' : ''}>Save decision & next</button><button class="button secondary" type="button" data-quick-back>Leave pending</button><button class="button quiet" type="button" data-full-game="${esc(raw.id)}">Full editor</button></div></form>`;
    $('#quick-review-status').textContent = '';
  }
  async function showPerformance(gameId) {
    const generation = ++performanceGeneration;
    const host = $('#game-performance');
    if (!$('#game-performance-dialog').open) $('#game-performance-dialog').showModal();
    const games = sourceGames();
    if (!gameId) {
      host.innerHTML = `<p>Choose a saved game to see all participants and their measured results.</p>${games.map(g => `<article class="review-task-card"><h3>${esc(g.label)}</h3><p>${g.spans.filter(s=>s.source_id===state.source).map(s=>`${time(s.start_seconds)}–${time(s.end_seconds)}`).join(', ')} · ${g.acceptance === 'human_confirmed' ? 'Boundaries reviewed' : 'Boundaries need review'}</p><button class="button primary" data-performance-game="${esc(g.id)}">View player results</button></article>`).join('') || '<p>No saved games yet. Review a game interval first. Face groups alone do not establish a game or player performance.</p>'}<button class="button secondary" data-open-recording-faces>See face lineups for this recording</button>`;
      return;
    }
    host.innerHTML = '<p role="status">Loading this game’s player results…</p>';
    try {
      const data = await api(`/games/${enc(gameId)}/skills`);
      if (generation !== performanceGeneration) return;
      const participants = data.document.participants || [], shots = data.document.shots || [];
      const ids = [...new Set([...participants.filter(p=>p.acceptance!=='excluded').map(p=>p.id), ...data.report.summaries.map(g=>g.participant)])];
      const stat = (s,unit) => s?.n ? `${s.mean.toFixed(2)} ${unit} · n=${s.n}${s.status === 'insufficient_sample' ? ' · few samples' : ''}` : 'Not measured';
      const label = id => participants.find(p=>p.id===id || (p.acceptance==='confirmed' && p.actor_ids.includes(id)))?.label || id || 'Unassigned';
      host.innerHTML = `<button class="button quiet" data-performance-back>← Games in this recording</button><h3>${esc(games.find(g=>g.id===gameId)?.label || 'Game')}</h3><p>${data.report.total_shots} saved ${data.report.total_shots===1 ? "shot" : "shots"} · ${data.candidate_coverage?.unreviewed || 0} delivery candidates awaiting review</p>${data.stale ? '<p class="callout">Game or source changed. Review saved observations before using these results.</p>' : ''}<p class="fine-print">Face names and shot participants are separate until linked by reviewed evidence. Missing measurements are shown as unavailable, never as zero. Results are grouped by comparable call, direction and calibration.</p><div class="end-table-wrap"><table class="end-table"><thead><tr><th>Player / call</th><th>Weight ratio</th><th>Line error</th><th>Wide / narrow</th><th>Sweeping</th></tr></thead><tbody>${ids.map(id=>{
        const groups=data.report.summaries.filter(g=>g.participant===id);
        return (groups.length ? groups : [null]).map(g=>`<tr><th scope="row">${esc(label(id))}${g ? `<br><small>${esc(g.call)} · ${esc(g.direction)}<br>${esc(g.calibration || 'No weight reference')} · ${esc(g.line_calibration || 'No line reference')}</small>` : ''}</th><td>${stat(g?.weight,'× target')}</td><td>${stat(g?.line,'°')}</td><td>${stat(g?.wide,'°')}</td><td>${stat(g?.sweeping,'s')}</td></tr>`).join('');
      }).join('') || '<tr><td colspan="5">No reviewed player measurements yet. Review deliveries and assign participants to build this report.</td></tr>'}</tbody></table></div><p class="fine-print">Positive line error is right; positive wide/narrow is wide. Fewer than five samples is insufficient for a stable comparison.</p><a class="button primary" href="/static/skills.html?game=${enc(gameId)}#delivery-candidates">Review shots & add measurements</a>`;
    } catch(error) { if(generation===performanceGeneration) host.textContent=error.message; }
  }
  document.addEventListener('click', async event => {
    const b=event.target.closest('button'); if(!b) return;
    try {
      if(b.dataset.workAction==='review') { listReviews(); $('#quick-review-dialog').showModal(); }
      if(b.dataset.workAction==='players') await showPerformance();
      if(b.dataset.workAction==='faces' || b.hasAttribute('data-open-recording-faces')) { $('#game-performance-dialog').close(); await window.hoglineFaces.openRecording(state.source); }
      if(b.dataset.workAction==='help') {
        $('#quick-review-title').textContent='Working with a recording';
        $('#quick-review-body').innerHTML='<ol class="workflow-help"><li><strong>Analyze recording</strong> finds likely games and scores. Processing finished means the jobs ended; results still need review.</li><li><strong>Review decisions</strong> shows one boundary or score at a time. Inspect the supplied frames, confirm or adjust, and save. Leave uncertain items pending.</li><li><strong>Players & results</strong> shows measured performance for a saved game. Review shots and participants to fill missing data.</li><li><strong>Find / redo faces</strong> opens this recording’s lineups. Delete a game’s face data to clear it, then rescan its saved interval.</li><li><strong>Clear analysis / start again</strong> previews which machine results will be removed. Cancel active jobs first. Saved reviews and face scans are kept.</li></ol>';
        $('#quick-review-status').textContent=''; $('#quick-review-dialog').showModal();
      }
      if(b.dataset.proposalGame) { openQuick(b.dataset.proposalGame,null,proposal); }
      if(b.hasAttribute('data-create-review-game')) { $('#quick-review-dialog').close(); window.hoglineRecordings.showTab('tools'); newGame(); }
      if(b.dataset.quickGame) openQuick(b.dataset.quickGame,b.dataset.quickEnd);
      if(b.hasAttribute('data-quick-back')) listReviews();
      if(b.dataset.fullGame) { $('#quick-review-dialog').close(); window.hoglineRecordings.showTab('tools'); editGame(b.dataset.fullGame); }
      if(b.hasAttribute('data-review-evidence')) { $('#quick-review-dialog').close(); $('#end-timeline-button').click(); }
      if(b.dataset.performanceGame) await showPerformance(b.dataset.performanceGame);
      if(b.hasAttribute('data-performance-back')) await showPerformance();
      if(b.dataset.workAction==='reset') {
        reset=null; $('#reset-clear').disabled=$('#reset-restart').disabled=true;
        $('#reset-preview').textContent='Checking this recording’s analysis…'; $('#reset-status').textContent=''; $('#recording-reset-dialog').showModal();
        const sid=state.source, plan=await api(`/recordings/${enc(sid)}/reset-preview`);
        reset={...plan,sourceId:sid};
        $('#reset-preview').innerHTML=`<p><strong>${esc(state.data.sources.find(s=>s.id===sid)?.title)}</strong></p><p>${esc(plan.kept)}</p><ul>${Object.entries(plan.counts).map(([k,n])=>`<li>${n} ${esc(({job:"analysis jobs",processing_batch:"analysis runs",job_execution:"task attempts",activity_scan:"recording surveys",analysis_window:"detail windows",scoring_segment:"score-reading segments",frame_observation:"machine frame readings",window_tracks:"stone tracking results",window_house_states:"house layouts",window_actors:"person tracking results",window_deliveries:"delivery proposals"})[k] || k.replaceAll('_',' '))}</li>`).join('') || '<li>No automatic analysis to clear.</li>'}</ul>${plan.blockers.map(v=>`<p class="callout">${esc(v)}</p>`).join('')}<p>Clearing these machine results cannot be undone. Starting again queues a new analysis.</p>`;
        $('#reset-clear').disabled=$('#reset-restart').disabled=Boolean(plan.blockers.length);
      }
      if(['reset-clear','reset-restart'].includes(b.id) && reset) {
        $('#reset-clear').disabled=$('#reset-restart').disabled=true;
        const sid=reset.sourceId;
        await api(`/recordings/${enc(sid)}/reset`,'POST',{expected_sha256:reset.sha256});
        reset=null;
        $('#reset-status').textContent='Automatic analysis cleared.';
        if(b.id==='reset-restart') {
          try { await api(`/recordings/${enc(sid)}/analyze`,'POST',{}); $('#reset-status').textContent='Cleared. New analysis queued.'; }
          catch(error) { $('#reset-status').textContent=`Analysis cleared, but restart failed: ${error.message}. Use Analyze recording to try again.`; }
        }
        await refresh(); window.hoglineRecordings.reloadResults(); render();
      }
    } catch(error) { if($('#recording-reset-dialog').open) $('#reset-status').textContent=error.message; else toast(error.message,true); }
  });
  $('#quick-review-body').addEventListener('submit',async event=>{
    if(event.target.id!=='quick-review-form')return;
    event.preventDefault(); event.stopPropagation();
    const form=event.target, values=new FormData(form), current=review;
    form.querySelector('button[type=submit]').disabled=true;
    try {
      const game=structuredClone(current.game);
      if(current.endId) {
        const e=game.ends.find(e=>e.id===current.endId);
        e.red_points=Number(values.get('red')); e.yellow_points=Number(values.get('yellow'));
        e.state=e.red_points===0 && e.yellow_points===0 ? 'blank':'played'; e.acceptance=values.get('decision');
      } else {
        game.spans.filter(s=>s.source_id===current.sourceId).forEach((s,i)=>{s.start_seconds=parseTime(values.get(`start-${i}`)); s.end_seconds=parseTime(values.get(`end-${i}`));});
        game.acceptance=values.get('decision');
      }
      await api(`/games/${enc(game.id)}`,'PUT',{game,expected_revision:current.revision,author:values.get('author'),reason:values.get('reason')});
      localStorage.setItem('hogline-reviewer',values.get('author'));
      await refresh(); await refreshSelectedWorkspace(); window.hoglineRecordings.reloadResults();
      const next=sourceGames().find(g=>g.acceptance!=='human_confirmed' && !(g.id===game.id && !current.endId));
      const scoreGame=sourceGames().find(g=>g.ends.some(e=>['played','blank'].includes(e.state) && e.acceptance!=='human_confirmed' && e.id!==current.endId));
      if(next) openQuick(next.id); else if(scoreGame) openQuick(scoreGame.id,scoreGame.ends.find(e=>['played','blank'].includes(e.state) && e.acceptance!=='human_confirmed' && e.id!==current.endId).id); else listReviews();
      $('#quick-review-status').textContent='Decision saved. Review the next item, or close to return to the recording.';
    } catch(error) { $('#quick-review-status').textContent=error.message; form.querySelector('button[type=submit]').disabled=false; }
  });
  document.addEventListener('error',event=>{
    if(event.target.matches?.('.quick-frame img')) {const p=document.createElement('p');p.className='fine-print';p.textContent='Saved image unavailable. Use the timestamp link to inspect the original.';event.target.replaceWith(p);}
  },true);
  function reviewProposal(book, end) {
    const board=end.board_evidence || {}, anchor=end.board_posting?.first_observation;
    const ids=[...new Set([...(board.evidence_ids || []),...(end.ice_evidence_ids || []),...(anchor?.evidence_id ? [anchor.evidence_id] : [])])];
    proposal={official_number:end.official_number ?? null, state:'played', red_points:end.points?.red ?? '', yellow_points:end.points?.yellow ?? '', acceptance:'proposed', evidence_ids:ids, note:'Reviewed from proposed recording score history'};
    $('#quick-review-title').textContent=`Review proposed end ${end.official_number ?? end.observed_index ?? '?'}`;
    $('#quick-review-status').textContent='';
    const games=sourceGames();
    $('#quick-review-body').innerHTML=`<p>Choose the game this end belongs to. You can adjust its points on the next screen. This saves an end score, not a final game result.</p><div class="quick-evidence">${ids.slice(0,2).map(id=>frame(id,'Score evidence')).join('')}</div>${games.map(g=>`<article class="review-task-card"><h3>${esc(g.label)}</h3><p>${g.spans.filter(s=>s.source_id===state.source).map(s=>`${time(s.start_seconds)}–${time(s.end_seconds)}`).join(', ')}</p><button class="button primary" data-proposal-game="${esc(g.id)}">Review in this game${g.ends.some(e=>e.official_number!=null && e.official_number===proposal.official_number) ? ' · replaces saved end' : ''}</button></article>`).join('') || '<p>First establish a game interval so this score can be assigned to the right game.</p>'}<button class="button secondary" data-create-review-game>Add game interval</button>`;
    if(!$('#quick-review-dialog').open) $('#quick-review-dialog').showModal();
  }
  window.hoglineWorkspace={render,reviewProposal}; render();
})();
