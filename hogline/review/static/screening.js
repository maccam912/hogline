const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let previews = [];
const decisions = [['team_visible','My team is visible'],['not_visible','Not visible here'],['unsure','Unsure']];
function render() {
  const items = previews.filter((p) => (!$('#date').value || p.date === $('#date').value) && (!$('#selected').checked || p.label?.decision === 'team_visible'));
  $('#grid').innerHTML = items.map((p) => `<article data-source="${esc(p.source_id)}"><h2>${esc(p.date)} · Sheet ${p.sheet}</h2><a href="${esc(p.youtube_url)}" target="_blank" rel="noopener"><img loading="lazy" src="/api/screening/${encodeURIComponent(p.source_id)}/preview.jpg" alt="Sheet ${p.sheet} at source second ${Math.round(p.source_seconds)}"></a><p class="muted">Clock selection: ${esc(p.clock_text || 'unavailable')} · source ${Math.floor(p.source_seconds/60)}:${String(Math.floor(p.source_seconds%60)).padStart(2,'0')} · <a href="${esc(p.youtube_url)}" target="_blank" rel="noopener">Watch around this moment ↗</a>${p.clock_ambiguous ? ' · afternoon assumed for screening' : ''}${p.date_verified ? '' : ' · overlay date unverified'}</p>${p.additional_previews?.length ? `<details><summary>More moments · ${p.additional_previews.length} extra previews</summary><p class="muted">Check these additional moments and tell me the sheet if you recognize your team. The buttons below still label the original preview above.</p>${p.additional_previews.map((extra) => `<p>${esc(extra.label)} · <a href="${esc(extra.youtube_url)}" target="_blank" rel="noopener">Watch this moment ↗</a></p><img loading="lazy" src="/api/screening/${encodeURIComponent(p.source_id)}/preview.jpg?sha=${encodeURIComponent(extra.sha256)}" alt="Sheet ${p.sheet}, ${esc(extra.label)}">`).join('')}</details>` : ''}<div class="actions">${decisions.map(([value,title]) => `<button data-decision="${value}" aria-pressed="${p.label?.decision === value}" ${p.stale ? 'disabled' : ''}>${title}</button>`).join('')}</div><input class="note" aria-label="Optional note" placeholder="Optional: who is visible or when to look" maxlength="1000" value="${esc(p.label?.note || '')}"><p class="muted">${p.stale ? 'Recording changed; preview needs refreshing.' : p.label ? `Saved for ${esc(p.label.team)} · ${esc(p.label.decision.replaceAll('_',' '))}` : 'Not labeled yet'}</p></article>`).join('') || '<p>No previews in this view.</p>';
}
$('#grid').addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-decision]');
  if (!button) return;
  const card = button.closest('article'), item = previews.find((p) => p.source_id === card.dataset.source);
  const team = $('#team').value.trim();
  if (!team) { $('#status').textContent = 'Enter a team name.'; return; }
  const buttons = [...card.querySelectorAll('button')]; buttons.forEach((b) => b.disabled = true);
  try {
    const response = await fetch(`/api/screening/${encodeURIComponent(item.source_id)}/label`, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({decision:button.dataset.decision,team,note:card.querySelector('.note').value,expected_revision:item.label_revision,expected_preview_sha256:item.sha256})});
    const data = await response.json();
    if (!response.ok) throw Error(data.detail || 'Label could not be saved.');
    item.label = data; item.label_revision = data.revision; $('#status').textContent = `Saved ${item.date}, Sheet ${item.sheet}.`; render();
  } catch (error) { $('#status').textContent = error.message; buttons.forEach((b) => b.disabled = false); }
});
$('#date').addEventListener('change', render); $('#selected').addEventListener('change', render);
fetch('/api/screening').then((r) => {if (!r.ok) throw Error('Could not load previews.'); return r.json();}).then((data) => {
  previews = data.previews;
  $('#date').innerHTML += [...new Set(previews.map((p) => p.date))].sort().reverse().map((date) => `<option>${esc(date)}</option>`).join('');
  $('#status').textContent = data.notice || `${previews.length} previews ready for your review.`; render();
}).catch((error) => $('#status').textContent = error.message);
