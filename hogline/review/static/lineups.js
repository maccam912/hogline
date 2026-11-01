(() => {
  // Faces & lineups: automatic per-recording face finding, one face per probable person
  // across games, and the per-game lineups underneath. Face data never leaves this computer.
  let recordingScope = null;
  let tab = "faces", summary = null, detail = null, openGame = null, gameView = null, showMinor = false;
  let faces = { items: [], total: 0, counts: null, method: null, q: "", named: false, minGames: 1, limit: 120 };
  let names = {}, strip = { sourceId: null, at: 0, games: null, loading: false };
  const selectedFaces = new Set();
  const style = document.createElement("style");
  style.textContent = `#lineups-dialog{width:min(1280px,96vw);max-width:96vw}
    #lineups-dialog .lu-tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 14px}
    #lineups-dialog .lu-tabs button[aria-pressed=true]{background:var(--green);color:#fff;border-color:var(--green)}
    #lineups-dialog .lu-toolbar{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin-bottom:12px}
    #lineups-dialog .lu-toolbar label{margin:0}#lineups-dialog .lu-toolbar input,#lineups-dialog .lu-toolbar select{margin:4px 0 0}
    #lineups-dialog .lu-faces{display:grid;grid-template-columns:repeat(auto-fill,minmax(118px,1fr));gap:10px}
    #lineups-dialog .lu-face{display:flex;flex-direction:column;align-items:stretch;gap:4px;padding:6px;border:1px solid var(--line);border-radius:6px;background:#fff;cursor:pointer;text-align:left;font:inherit;color:inherit}
    #lineups-dialog .lu-face:hover,#lineups-dialog .lu-face:focus-visible{border-color:var(--green);outline:none;box-shadow:0 0 0 2px var(--green-light)}
    #lineups-dialog .lu-face img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:4px;background:#e8ebe2;display:block}
    #lineups-dialog .lu-face strong{font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    #lineups-dialog .lu-face small{font-size:10px;color:var(--muted)}
    #lineups-dialog .lu-select{display:flex;gap:6px;align-items:center;padding:6px;font-size:12px}
    #lineups-dialog .lu-select input{margin:0}
    #lineups-dialog .lu-group-toolbar{position:sticky;top:0;z-index:2;background:var(--paper,#fafcf5);padding:8px;border:1px solid var(--line);border-radius:6px}
    #lineups-dialog .lu-face-tile:has(input:checked){outline:2px solid var(--green);border-radius:6px}
    #lineups-dialog .lu-group-members{display:flex;flex-wrap:wrap;gap:4px}
    #lineups-dialog .lu-group-members .lu-strip{width:calc(50% - 4px)}
    #lineups-dialog .lu-group-members .lu-strip:only-child{width:100%}
    #lineups-dialog .lu-person{display:grid;grid-template-columns:160px 1fr;gap:18px;align-items:start;margin-bottom:14px}
    #lineups-dialog .lu-person>img{width:160px;height:160px;border-radius:6px;object-fit:cover;background:#e8ebe2}
    #lineups-dialog .lu-person h3{margin:0 0 4px;font-size:20px}
    #lineups-dialog .lu-rows{display:flex;flex-direction:column;gap:8px}
    #lineups-dialog .lu-row{display:grid;grid-template-columns:auto 1fr auto;gap:12px;align-items:center;padding:8px;border:1px solid var(--line);border-radius:6px;background:#fff}
    #lineups-dialog .lu-row img{height:48px;width:auto;border-radius:3px;display:block}
    #lineups-dialog .lu-row p{margin:0}#lineups-dialog .lu-row .batch-actions{gap:6px;justify-content:flex-end}
    #lineups-dialog .lu-row .button,#lineups-dialog .lu-person .button,#lineups-dialog .lineup-card .button{padding:4px 10px}
    #lineups-dialog .lineup-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
    #lineups-dialog .lineup-card{padding:10px;margin:0}
    #lineups-dialog .lineup-card .lu-strip{width:100%;height:auto;display:block;border-radius:4px;margin:6px 0}
    #lineups-dialog .lineup-card p{margin:4px 0}
    #lineups-dialog .lineup-name{display:grid;grid-template-columns:1fr 1fr 6.5rem auto;gap:6px;align-items:end}
    #lineups-dialog .lineup-name label{font-size:12px;margin:0}
    #lineups-dialog .lineup-name input,#lineups-dialog .lineup-name select{width:100%;min-width:0;padding:4px 6px;height:auto;margin:2px 0 0}
    #lineups-dialog .lu-name{display:flex;gap:8px;align-items:end;flex-wrap:wrap}
    #lineups-dialog .lu-name label{margin:0}#lineups-dialog .lu-name input{margin:4px 0 0}
    #lineups-dialog .lu-check{display:flex;gap:6px;align-items:center;font-size:11px;color:var(--ink);white-space:nowrap}
    #lineups-dialog .lu-check input{margin:0}
    #lineups-dialog .lu-games{width:100%;border-collapse:collapse;font-size:12px}
    #lineups-dialog .lu-games td,#lineups-dialog .lu-games th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}
    #lineups-dialog .lu-games tr[data-game]{cursor:pointer}#lineups-dialog .lu-games tr[data-game]:hover{background:var(--green-light)}
    @media (max-width:640px){#lineups-dialog .lu-person{grid-template-columns:1fr}#lineups-dialog .lu-row{grid-template-columns:1fr}#lineups-dialog .lineup-name{grid-template-columns:1fr 1fr}}
    #recording-faces{margin:0 0 12px;padding:10px 12px;border:1px solid var(--line);border-radius:6px;background:#fff}
    #recording-faces .rf-head{display:flex;justify-content:space-between;gap:8px;align-items:center;font-size:11px;color:var(--muted)}
    #recording-faces .rf-list{display:flex;gap:8px;overflow-x:auto;padding-top:8px}
    #recording-faces button.rf-face{flex:0 0 auto;width:64px;border:1px solid var(--line);border-radius:5px;background:#fff;padding:3px;cursor:pointer;font-family:inherit;font-size:10px;line-height:1.3;color:var(--ink);text-align:center}
    #recording-faces button.rf-face:hover{border-color:var(--green)}
    #recording-faces button.rf-face img{width:56px;height:56px;border-radius:3px;display:block;object-fit:cover;background:#e8ebe2}
    #recording-faces button.rf-face span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}`;
  document.head.appendChild(style);
  const dialog = () => $("#lineups-dialog");
  const status = (text) => ($("#lineups-status").textContent = text || "");
  const enc = encodeURIComponent;
  const portraitURL = (p, size = 96) => (p ? `/api/lineups/${enc(p.lineup_id)}/players/${enc(p.player_id)}/portrait.jpg?size=${size}` : "");
  const stripURL = (lineupId, playerId, limit = 6, size = 64) => `/api/lineups/${enc(lineupId)}/players/${enc(playerId)}/faces.jpg?limit=${limit}&size=${size}`;
  const gameLabel = (g) => `${g.title_date || "Date ?"} · Sheet ${g.sheet ?? "?"} · ${time(g.start_seconds)}–${time(g.end_seconds)}`;
  const dates = (f) => (f.first_date ? (f.first_date === f.last_date ? f.first_date : `${f.first_date} – ${f.last_date}`) : "Date unknown");
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const personName = (f) => f.person?.name || "Unnamed";
  const sourceTitle = (id) => state.data.sources.find((s) => s.id === id)?.title || "Recording";

  async function loadSummary() {
    summary = await api("/lineups?view=summary");
  }
  async function loadFaces(more = false) {
    const params = new URLSearchParams({ q: faces.q, min_games: faces.minGames, limit: faces.limit, offset: more ? faces.items.length : 0 });
    if (faces.named) params.set("named", "true");
    const page = await api(`/faces?${params}`);
    faces.items = more ? faces.items.concat(page.identities) : page.identities;
    Object.assign(faces, { total: page.total, counts: page.counts, method: page.method, fingerprint: page.fingerprint });
  }
  async function loadNames() {
    try { names = await api("/faces/names"); } catch { names = {}; }
  }

  // ---- automatic face finding ------------------------------------------------------------
  function automatic() {
    const a = summary.automatic, c = a.counts || {}, b = a.backfill;
    const head = a.enabled
      ? `Runs after every recording analysis · ${a.slots} recording at a time · one sample every ${a.interval_seconds} s`
      : "Off (HOGLINE_AUTO_LINEUPS=0). Recordings still join the queue after analysis.";
    const counts = ["running", "waiting", "done", "attention", "skipped", "cancelled"].filter((k) => c[k]).map((k) => `${c[k]} ${k === "attention" ? "need attention" : k}`).join(" · ") || "Nothing queued yet";
    const running = a.running.map((r) => `<p>Finding faces in <strong>${escapeHTML(r.title || sourceTitle(r.source_id))}</strong> · ${plural(r.spans?.length || 0, "game span")} (${r.basis === "posted_score_histories" ? "from posted scores" : "whole recording"})</p>`).join("");
    const attention = a.attention.map((r) => `<p><span class="pill amber">attention</span> ${escapeHTML(r.title || sourceTitle(r.source_id))}: ${escapeHTML(r.problem || "")} <button class="button secondary" type="button" data-auto="queue" data-auto-source="${r.source_id}">Queue again</button></p>`).join("");
    const filtered = filteredAnalyzedSources();
    const backfill = b.backfill_candidates
      ? `<div class="batch-actions"><button class="button secondary" type="button" data-backfill="all">Find faces in all ${b.backfill_candidates} analyzed recordings without them</button>${filtered.length ? `<button class="button secondary" type="button" data-backfill="filtered">Only recordings in the current list (${filtered.length})</button>` : ""}</div>
         <p class="fine-print">All of them: about ${b.backfill_hours} h of video, roughly ${b.estimated_worker_hours} worker-hours and ${b.estimated_storage_gb} GB of face data. Newest games run first; skip or cancel any time under Jobs.</p>`
      : "";
    return `<details class="batch-card" ${a.running.length || a.attention.length || a.blocked ? "open" : ""}><summary><strong>Automatic face finding</strong> · ${escapeHTML(counts)}</summary>
      <p class="fine-print">${escapeHTML(head)}. Titles containing ${a.excluded_title_words.map((w) => `“${escapeHTML(w)}”`).join(", ")} are skipped (no junior events).</p>
      ${a.blocked ? `<p class="callout">Waiting: ${escapeHTML(a.blocked)}</p>` : ""}${running}${attention}${backfill}</details>`;
  }

  function filteredAnalyzedSources() {
    const ids = new Set($$("#source-list [data-source]").map((b) => b.dataset.source));
    return [...ids];
  }

  // ---- faces gallery ----------------------------------------------------------------------
  function facesTab() {
    const c = faces.counts;
    const intro = c
      ? `<p class="fine-print">${plural(c.identities, "probable person")} from ${plural(c.lineups, "game")}, ${c.in_two_or_more_games} seen in two or more games, ${c.named} named. Select a face to see every game. Tick faces belonging to one person, then choose Group selected faces. Names are optional.</p>`
      : "";
    const tiles = faces.items.map((f) => `<div class="lu-face-tile"><button type="button" class="lu-face" data-face="${f.id}" title="${escapeHTML(personName(f))}">
        <img loading="lazy" src="${portraitURL(f.portrait)}" alt="">
        <strong>${escapeHTML(personName(f))}</strong><small>${plural(f.games, "game")} · ${escapeHTML(dates(f))}</small></button><label class="lu-select"><input type="checkbox" data-select-face="${f.id}" ${selectedFaces.has(f.id) ? "checked" : ""}> Select face</label></div>`).join("");
    return `${intro}<form class="lu-toolbar" id="faces-filter">
        <label>Search names or teams<input type="search" name="q" value="${escapeHTML(faces.q)}" placeholder="e.g. Koski"></label>
        <label>Seen in<select name="min_games"><option value="1" ${faces.minGames === 1 ? "selected" : ""}>Any number of games</option><option value="2" ${faces.minGames === 2 ? "selected" : ""}>2+ games</option><option value="3" ${faces.minGames === 3 ? "selected" : ""}>3+ games</option></select></label>
        <label class="lu-check"><input type="checkbox" name="named" ${faces.named ? "checked" : ""}> Named only</label>
        <button class="button secondary" type="submit">Apply</button></form>
      ${tiles ? `${groupToolbar()}<div class="lu-faces">${tiles}</div>` : `<p class="fine-print">${summary.lineups.length ? "No faces match." : "No faces yet. They appear here as recordings are analyzed, or start one under Find faces."}</p>`}
      ${faces.items.length < faces.total ? `<p><button class="button secondary" type="button" id="faces-more">Show more (${faces.total - faces.items.length} left)</button></p>` : ""}`;
  }

  function groupToolbar() {
    return `<p class="batch-actions lu-group-toolbar"><button class="button primary" type="button" data-group-selected ${selectedFaces.size < 2 ? "disabled" : ""}>Group selected faces</button><span data-selected-count>${selectedFaces.size} selected</span><button class="button quiet" type="button" data-clear-face-selection>Clear selection</button></p>`;
  }

  function groupingUndo(groups) {
    const players = tab === "games" ? gameView?.lineup.players || [] : detail?.appearances || [];
    return groups.map(g => {
      const labels = g.player_ids.map(id => players.find(p => p.id === id)).filter(Boolean).map(p => `Player ${p.number}`);
      return `<p class="fine-print">Manually grouped: ${escapeHTML(labels.length ? labels.join(" + ") : plural(g.player_ids.length, "face group"))} <button class="button secondary" type="button" data-undo-grouping="${g.id}">Undo grouping</button></p>`;
    }).join("");
  }

  function nameForm(d) {
    const unnamed = d.appearances.filter((a) => !a.link).length;
    if (d.person) {
      return `<p><strong>${escapeHTML(d.person.name)}</strong>${d.person.team ? ` · ${escapeHTML(d.person.team)}` : ""}</p>
        ${unnamed ? `<button class="button" type="button" data-name-rest="${d.person.id}">Name the other ${plural(unnamed, "game")} too</button>` : ""}
        <p class="fine-print">Edit the name or team under Teams &amp; people.</p>`;
    }
    return `<form class="lu-name" id="face-name"><label>Name<input name="name" list="lineup-people" required maxlength="160" placeholder="Who is this?"></label>
      <label>Team<input name="team" list="lineup-teams" maxlength="120" placeholder="Optional"></label>
      <button class="button" type="submit">Name in ${plural(d.appearances.length, "game")}</button></form>
      <p class="fine-print">Naming links this face in each listed game (and any possible matches you tick below) to one person. You can remove a single game's name later.</p>`;
  }

  function appearanceRow(d, a) {
    const g = a.lineup;
    const tag = a.link ? `<span class="pill green">${escapeHTML(a.link.name)}${a.link.position ? ` · ${escapeHTML(a.link.position)}` : ""}</span>` : "";
    const separable = d.appearances.length > 1 && a.id !== d.id && !(a.link && d.person && a.link.person_id === d.person.id);
    return `<div class="lu-row"><img loading="lazy" src="${stripURL(g.id, a.id, 4, 48)}" alt="">
      <div><p><strong>${escapeHTML(g.title || sourceTitle(g.source_id))}</strong> ${tag} ${g.automatic ? '<span class="pill">auto</span>' : ""}</p>
      <p class="fine-print">${escapeHTML(gameLabel(g))} · ${a.faces} faces · seen ${time(a.first_seconds)}–${time(a.last_seconds)}${a.similarity != null ? ` · match ${a.similarity.toFixed(2)}` : ""}</p></div>
      <div class="batch-actions"><button class="button secondary" type="button" data-open-game="${g.id}">Game</button>
      <button class="button secondary" type="button" data-source="${g.source_id}" data-seek="${a.first_seconds}">Recording</button>
      ${a.watch_url ? `<a class="button secondary" href="${escapeHTML(a.watch_url)}" target="_blank" rel="noopener">Watch ↗</a>` : ""}
      ${separable ? `<button class="button secondary" type="button" data-separate="${a.id}">Not this person</button>` : ""}</div></div>`;
  }

  function possibleRow(d, a) {
    const g = a.lineup;
    const action = d.person
      ? `<button class="button secondary" type="button" data-link-person="${d.person.id}" data-link-player="${a.id}">Same person</button>`
      : `<button class="button secondary" type="button" data-group-with="${a.id}">Same person</button><label class="lu-check"><input type="checkbox" data-also="${a.id}"> Include when naming</label>`;
    return `<div class="lu-row"><img loading="lazy" src="${stripURL(g.id, a.id, 4, 48)}" alt="">
      <div><p><strong>${escapeHTML(g.title || sourceTitle(g.source_id))}</strong></p><p class="fine-print">${escapeHTML(gameLabel(g))} · ${a.faces} faces · match ${a.similarity.toFixed(2)}</p></div>
      <div class="batch-actions">${action}<button class="button secondary" type="button" data-face="${a.id}">Look</button></div></div>`;
  }

  function faceDetail() {
    const d = detail;
    const separations = d.separations.map((s) => `<p class="fine-print">Kept apart: ${escapeHTML(s.player?.lineup?.title || "a game")} ${s.player ? escapeHTML(gameLabel(s.player.lineup)) : ""} <button class="button secondary" type="button" data-unseparate="${s.id}">Undo</button></p>`).join("");
    return `<p><button class="button secondary" type="button" id="face-back">← All faces</button></p>
      <div class="lu-person"><img src="${portraitURL(d.portrait, 160)}" alt="">
        <div><h3>${escapeHTML(personName(d))}</h3>
        <p class="fine-print">${plural(d.games, "game")} on ${plural(d.recordings, "recording")} · ${escapeHTML(dates(d))} · ${d.faces} faces${d.below_identity_floor ? " · too few faces to link across games automatically" : ""}</p>
        ${nameForm(d)}</div></div>
      <h3>Games</h3><div class="lu-rows">${d.appearances.map((a) => appearanceRow(d, a)).join("")}</div>
      ${d.possible.length ? `<h3>Possibly also</h3><p class="fine-print">Weaker matches in other games. Check them by eye before ${d.person ? "confirming" : "ticking"}.</p><div class="lu-rows">${d.possible.map((a) => possibleRow(d, a)).join("")}</div>` : ""}
      ${groupingUndo(d.groupings || [])}${separations}`;
  }

  // ---- games ------------------------------------------------------------------------------
  function gamesTab() {
    if (gameView) return gameDetail();
    if (!summary.lineups.some(g => !recordingScope || g.source_id === recordingScope)) return `<p>No face lineups for ${recordingScope ? "this recording" : "the archive"} yet.</p><button class="button primary" data-tab="manual">Find faces in a game interval</button>${recordingScope ? `<button class="button secondary" data-auto="queue" data-auto-source="${recordingScope}">Queue automatic face scan</button>` : ""}`;
    const rows = [...summary.lineups].filter(g => !recordingScope || g.source_id === recordingScope).reverse().map((g) => `<tr data-game="${g.id}"><td><button class="button secondary" data-open-game="${g.id}">Open lineup</button></td><td>${escapeHTML(g.title_date || "?")}</td><td>${g.sheet ?? "?"}</td><td>${escapeHTML(g.title || sourceTitle(g.source_id))}</td><td>${time(g.start_seconds)}–${time(g.end_seconds)}</td><td>${g.player_count}</td><td>${g.names.map(escapeHTML).join(", ")}</td><td>${g.automatic ? "auto" : "manual"}</td></tr>`).join("");
    return `${recordingScope ? `<p><strong>${escapeHTML(sourceTitle(recordingScope))}</strong></p><p>Open a lineup to review faces or rescan the same game. <button class="button quiet" id="all-lineup-recordings">Show all recordings</button></p>` : ""}<div class="end-table-wrap"><table class="lu-games"><thead><tr><th>Review</th><th>Date</th><th>Sheet</th><th>Recording</th><th>Span</th><th>Players</th><th>Named</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function playerCard(lineup, p, members = [p]) {
    const named = p.link
      ? `<p><strong>${escapeHTML(p.link.name)}</strong>${p.link.team ? ` · ${escapeHTML(p.link.team)}` : ""}${p.link.position ? ` · ${escapeHTML(p.link.position)}` : ""}</p>
         <button class="button secondary" type="button" data-unlink="${p.id}">Remove name</button>`
      : `${p.suggestions.map((s) => `<button class="button secondary" type="button" data-suggest="${p.id}" data-person-id="${s.person_id}">${escapeHTML(s.name)}?${s.team ? ` · ${escapeHTML(s.team)}` : ""} · ${s.similarity.toFixed(2)}${s.strength === "likely" ? " · likely" : ""}</button>`).join(" ")}`;
    const title = p.link ? p.link.name : `Player ${p.number}`;
    return `<article class="batch-card lineup-card"><div class="batch-heading"><strong>${escapeHTML(title)}</strong><span class="pill ${p.role === "minor" ? "amber" : ""}">${members.reduce((n, m) => n + m.faces, 0)} faces</span></div>
      <label class="lu-select"><input type="checkbox" data-select-face="${p.id}" ${selectedFaces.has(p.id) ? "checked" : ""}> Select ${members.length > 1 ? "this person" : "face group"}</label>
      <div class="lu-group-members">${members.map(m => `<img class="lu-strip" loading="lazy" src="${stripURL(lineup.id, m.id)}" alt="Faces grouped as ${escapeHTML(title)}">`).join("")}</div>
      <p class="fine-print">${members.length > 1 ? `${members.length} groups · ` : ""}${time(Math.min(...members.map(m => m.first_seconds)))}–${time(Math.max(...members.map(m => m.last_seconds)))} · left ${members.reduce((n,m) => n + (m.views.perspective_left || 0),0)} · right ${members.reduce((n,m) => n + (m.views.perspective_right || 0),0)}${p.best_face.youtube_url ? ` · <a href="${escapeHTML(p.best_face.youtube_url)}" target="_blank" rel="noopener">Watch ↗</a>` : ""}</p>
      <p><button class="button secondary" type="button" data-face="${p.id}">All games with this face</button></p>
      ${named}
      ${members.length > 1 ? `<button class="button secondary" type="button" data-face="${p.id}">Review name for grouped faces</button>` : `<form data-link="${p.id}" class="lineup-name"><label>Name<input name="name" list="lineup-people" maxlength="160" required placeholder="Name" value="${escapeHTML(p.link?.name || "")}"></label>
      <label>Team<input name="team" list="lineup-teams" maxlength="120" placeholder="Optional" value="${escapeHTML(p.link?.team || "")}"></label>
      <label>&nbsp;<select name="position" aria-label="Position"><option value="">Position</option>${summary.positions.map((x) => `<option ${p.link?.position === x ? "selected" : ""}>${x}</option>`).join("")}</select></label>
      <button class="button secondary" type="submit">Save</button></form>`}</article>`;
  }

  function gameDetail() {
    const l = gameView.lineup;
    const grouped = new Map();
    l.players.forEach(p => {
      const key = p.group_id || p.id;
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(p);
    });
    const players = [...grouped.values()].filter(members => showMinor || members.some(p => p.role === "player"));
    const minor = l.players.filter((p) => p.role !== "player").length;
    return `<p><button class="button secondary" type="button" id="game-back">← All games</button></p>
      <h3>${escapeHTML(l.title || "Recording")} · ${time(l.start_seconds)}–${time(l.end_seconds)}</h3>
      <p class="fine-print">${l.face_count} faces from ${l.sample_count} samples${l.request.automatic ? " · found automatically" : ""}. Groups are proposals: one person can be split into two groups, and people on the next sheet can appear.</p>
      <div class="batch-actions"><button class="button secondary" type="button" data-source="${l.source_id}" data-seek="${l.start_seconds}">Open recording</button>
        <label class="lu-check"><input id="lineup-minor" type="checkbox" ${showMinor ? "checked" : ""}> Show ${minor} small or other-sheet groups</label>
        <button class="button primary" type="button" data-rescan-lineup="${l.id}">Redo face scan</button><button class="button secondary" type="button" data-regroup="${l.id}">Adjust grouping (reuse faces)</button><button class="button secondary" type="button" data-delete-lineup="${l.id}">Delete this game's face data</button></div>
      <p>Select groups belonging to the same person, then choose <strong>Group selected faces</strong>. No name is required. Show small groups to include less frequent faces.</p>${groupToolbar()}${groupingUndo(gameView.groupings || [])}
      <p class="fine-print">Redo face scan replaces this game’s face groups and name links. Adjust grouping reuses the saved face images. People’s names remain available to assign again.</p><div class="lineup-grid">${players.map(members => playerCard(l, members.find(p => p.link) || members[0], members)).join("")}</div>`;
  }

  // ---- teams & people, manual ---------------------------------------------------------------
  function peopleTab() {
    const teams = summary.teams.length
      ? summary.teams.map((t) => `<div class="batch-card"><div class="batch-heading"><strong>${escapeHTML(t.team)}</strong><span class="pill">${plural(t.games.length, "game")}</span></div>
        <p class="fine-print">${t.people.map(escapeHTML).join(", ")}</p>
        ${t.games.map((g) => `<p><button class="button secondary" type="button" data-open-game="${g.lineup_id}">${escapeHTML(g.title_date || "")} · Sheet ${g.sheet ?? "?"} · ${time(g.start_seconds)}</button> ${g.members_present.map(escapeHTML).join(", ")} ${g.confidence === "team_game" ? "" : '<span class="pill amber">one member only</span>'}</p>`).join("")}</div>`).join("")
      : `<p class="fine-print">Give a named person a team to list that team's games here.</p>`;
    const people = summary.people.map((p) => `<form data-person="${p.id}" class="batch-actions">
      <label>Name<input name="name" value="${escapeHTML(p.name)}" required maxlength="160"></label><label>Team<input name="team" list="lineup-teams" value="${escapeHTML(p.team || "")}" maxlength="120"></label>
      <span class="fine-print">${plural(p.lineup_ids.length, "game")}</span><button class="button secondary" type="submit">Save</button>
      <button class="button secondary" type="button" data-person-face="${p.id}">Games</button><button class="button secondary" type="button" data-person-forget="${p.id}">Forget</button></form>`).join("");
    return `<h3>Teams</h3>${teams}<h3>People</h3>${people || '<p class="fine-print">Nobody is named yet. Name a face under Faces.</p>'}`;
  }

  function manualTab() {
    const m = summary.face_models;
    const missing = !m.yunet.installed || !(m["arcface-r50"].installed || m.sface.installed);
    const sources = [...state.data.sources].sort((a, b) => (a.title || "").localeCompare(b.title || ""));
    const selected = recordingScope || (state.source && sources.some((s) => s.id === state.source) ? state.source : sources[0]?.id);
    const rows = summary.batches.map((b) => `<p>${escapeHTML(sourceTitle(b.lineup.source_id))} · ${time(b.lineup.start_seconds)}–${time(b.lineup.end_seconds)} · ${b.succeeded_units}/${b.units.length} segments · ${escapeHTML(b.state)}${b.lineup.automatic ? " · automatic" : ""}${b.pause_reason ? ` · ${escapeHTML(b.pause_reason)}` : ""}</p>`);
    rows.push(...summary.assemblies.map((a) => `<p>Grouping faces · ${escapeHTML(a.state)}${a.error ? ` · ${escapeHTML(a.error)}` : ""}</p>`));
    return `${missing ? '<p class="callout">Face models are not installed. Run <code>uv run hogline face-models --install</code> once (explicit download).</p>' : ""}
      ${rows.length ? `<div class="batch-card"><strong>In progress</strong>${rows.join("")}<p class="fine-print">Progress also appears under Jobs.</p></div>` : ""}
      <div class="batch-card"><strong>Find players in one game by hand</strong>
      <p class="fine-print">Analyzed recordings get faces automatically. Use this for a specific span or a denser sample (every second). Only people standing on the ice are used.</p>
      <form id="lineup-form"><label>Recording<select name="source_id">${sources.map((s) => `<option value="${s.id}" ${s.id === selected ? "selected" : ""}>${escapeHTML(s.title || s.id)}</option>`).join("")}</select></label>
      <div class="batch-actions"><label>Start<input name="start" value="00:00:00" required></label><label>End<input name="end" required placeholder="HH:MM:SS"></label>
      <label>Sampling<select name="interval"><option value="1">Every second</option><option value="2">Every 2 seconds (faster)</option></select></label></div>
      <div class="batch-actions"><button class="button secondary" type="button" id="lineup-spans">Suggest game spans from the scoreboard</button><span id="lineup-span-buttons"></span></div>
      <button class="button" type="submit" ${missing ? "disabled" : ""}>Find players</button></form></div>`;
  }

  function render() {
    if (!summary) return;
    const titleTeams=state.data.sources.find(s=>s.id===(gameView?.lineup.source_id || recordingScope))?.title_teams || [];
    const teamNames = [...new Set([...titleTeams,...summary.people.map((p) => p.team).filter(Boolean)])];
    const tabs = [["faces", "Faces"], ["games", "Games"], ["people", "Teams & people"], ["manual", "Find faces"]];
    const body = tab === "faces" ? (detail ? faceDetail() : facesTab()) : tab === "games" ? gamesTab() : tab === "people" ? peopleTab() : manualTab();
    $("#lineups-content").innerHTML = `${automatic()}
      <datalist id="lineup-people">${summary.people.map((p) => `<option value="${escapeHTML(p.name)}">${escapeHTML(p.team || "")}</option>`).join("")}</datalist>
      <datalist id="lineup-teams">${teamNames.map((t) => `<option value="${escapeHTML(t)}">`).join("")}</datalist>
      <div class="lu-tabs" role="group" aria-label="Lineup views">${tabs.map(([key, label]) => `<button class="button secondary" type="button" data-tab="${key}" aria-pressed="${tab === key}">${label}</button>`).join("")}</div>
      ${titleTeams.length ? `<p><strong>Teams from title: ${titleTeams.map(escapeHTML).join(" vs ")}</strong><br><span class="fine-print">Choose these team names when assigning players. Title order does not identify stone colors.</span></p>` : ""}<div id="lineups-body">${body}</div>`;
    $("#lineup-minor")?.addEventListener("change", (e) => { showMinor = e.target.checked; render(); });
  }

  async function openFace(playerId) {
    selectedFaces.clear();
    status("Loading…");
    detail = await api(`/faces/players/${enc(playerId)}`);
    tab = "faces";
    render();
    status("");
    $("#lineups-body")?.scrollIntoView({ block: "start" });
  }
  async function openLineup(lineupId) {
    selectedFaces.clear();
    status("Loading…");
    gameView = await api(`/lineups/${enc(lineupId)}`);
    openGame = lineupId;
    tab = "games";
    render();
    status("");
  }
  async function refreshAll() {
    selectedFaces.clear();
    await Promise.all([loadSummary(), loadFaces(), loadNames()]);
    if (detail) detail = await api(`/faces/players/${enc(detail.id)}`).catch(() => null);
    if (gameView && openGame) gameView = await api(`/lineups/${enc(openGame)}`).catch(() => null);
    strip.at = 0;
    render();
    window.hoglineFaces.render();
  }
  async function act(fn, done) {
    try {
      status("Saving…");
      await fn();
      await refreshAll();
      status(done || "");
    } catch (error) {
      status(error.message);
    }
  }

  async function open(playerId = null) {
    recordingScope = null;
    if (!dialog().open) dialog().showModal();
    status("Loading…");
    try {
      await Promise.all([loadSummary(), loadFaces()]);
      if (playerId) await openFace(playerId);
      else render();
      status("");
    } catch (error) { status(error.message); }
  }
  $("#lineups-button").addEventListener("click", () => open());

  $("#lineups-content").addEventListener("change", event => {
    const input = event.target.closest("[data-select-face]");
    if (!input) return;
    if (input.checked) selectedFaces.add(input.dataset.selectFace);
    else selectedFaces.delete(input.dataset.selectFace);
    $("[data-group-selected]").disabled = selectedFaces.size < 2;
    $("[data-selected-count]").textContent = `${selectedFaces.size} selected`;
  });

  $("#lineups-content").addEventListener("click", async (event) => {
    const t = event.target.closest("button,tr[data-game]") || event.target;
    try {
      if (t.hasAttribute("data-clear-face-selection")) { selectedFaces.clear(); render(); return; }
      if (t.hasAttribute("data-group-selected")) {
        const ids = [...selectedFaces];
        const expected = tab === "games" ? gameView.fingerprint : faces.fingerprint;
        t.disabled = true;
        try {
          return await act(() => api("/faces/groupings", "POST", {player_ids: ids, expected_fingerprint: expected, reason: "Grouped selected faces as one person"}), "Faces grouped. Use Undo grouping to reverse this.");
        } finally { t.disabled = selectedFaces.size < 2; }
      }
      if (t.dataset.groupWith) return act(() => api("/faces/groupings", "POST", {player_ids: [detail.id, t.dataset.groupWith], expected_fingerprint: detail.fingerprint, reason: "Confirmed faces as the same person"}), "Faces grouped.");
      if (t.dataset.undoGrouping) return act(() => api(`/faces/groupings/${enc(t.dataset.undoGrouping)}/delete`, "POST", {reason: "Undo manual face grouping"}), "Grouping undone.");
      if (t.id === "all-lineup-recordings") { recordingScope = null; render(); return; }
      if (t.dataset.rescanLineup) {
        if (!confirm("Replace this game’s face scan? Its face groups and name links will be removed. Saved scores and other games stay. You can assign names again after the scan.")) return;
        const l = gameView.lineup;
        t.disabled=true;
        try {
          await api(`/lineups/${enc(l.id)}/rescan`, "POST", {reason:"Redo face scan from recording workspace"});
          gameView=null; openGame=null; await refreshAll(); status("New face scan queued. Follow progress in Jobs.");
        } finally { t.disabled=false; }
        return;
      }
      if (t.dataset.tab) { selectedFaces.clear(); tab = t.dataset.tab; if (tab === "games") gameView = null; render(); return; }
      if (t.dataset.face) return await openFace(t.dataset.face);
      if (t.id === "face-back") { detail = null; render(); return; }
      if (t.id === "game-back") { gameView = null; openGame = null; render(); return; }
      if (t.id === "faces-more") { await loadFaces(true); render(); return; }
      if (t.dataset.game) return await openLineup(t.dataset.game);
      if (t.dataset.openGame) return await openLineup(t.dataset.openGame);
      if (t.dataset.source) {
        // The workbench's own handler selects the recording; jump to the face's time after it.
        const seconds = Number(t.dataset.seek || 0);
        dialog().close();
        setTimeout(() => { state.seconds = Math.max(0, Math.floor(seconds)); window.render(); }, 0);
        return;
      }
      if (t.dataset.personFace) {
        const p = summary.people.find((x) => x.id === t.dataset.personFace);
        const anyPlayer = (await api(`/faces?q=${enc(p.name)}&named=true&limit=50`)).identities.find((f) => f.person?.id === p.id);
        if (anyPlayer) return await openFace(anyPlayer.id);
        status("This person has no compared faces yet.");
        return;
      }
      if (t.dataset.backfill) {
        const ids = t.dataset.backfill === "filtered" ? filteredAnalyzedSources() : null;
        const a = summary.automatic.backfill;
        if (!ids && !confirm(`Queue ${a.backfill_candidates} recordings (about ${a.estimated_worker_hours} worker-hours, ${a.estimated_storage_gb} GB)?`)) return;
        return act(async () => {
          const r = await api("/lineups/automatic/backfill", "POST", { source_ids: ids });
          toast(`Queued ${plural(r.queued, "recording")} for face finding${r.not_analyzed ? ` (${r.not_analyzed} not analyzed yet)` : ""}.`);
        });
      }
      if (t.dataset.auto) return act(() => api(`/lineups/automatic/${enc(t.dataset.autoSource)}/${t.dataset.auto}`, "POST"));
      if (t.dataset.separate) {
        const target = detail.person ? { from_person_id: detail.person.id } : { from_player_id: detail.id };
        return act(() => api(`/faces/players/${enc(t.dataset.separate)}/separate`, "POST", { ...target, reason: "Not the same person" }), "Kept apart.");
      }
      if (t.dataset.unseparate) return act(() => api(`/faces/separations/${enc(t.dataset.unseparate)}/delete`, "POST", { reason: "Undo separation" }));
      if (t.dataset.linkPerson) {
        return act(() => api(`/lineups/players/${enc(t.dataset.linkPlayer)}/link`, "PUT", { person_id: t.dataset.linkPerson, reason: "Confirmed a possible match" }), "Named.");
      }
      if (t.dataset.nameRest) {
        const ids = detail.appearances.filter((a) => !a.link).map((a) => a.id);
        return act(() => api(`/faces/players/${enc(detail.id)}/name`, "PUT", { player_ids: ids, person_id: t.dataset.nameRest, reason: "Named the rest of a face's games" }), "Named.");
      }
      if (t.dataset.suggest) return act(() => api(`/lineups/players/${enc(t.dataset.suggest)}/link`, "PUT", { person_id: t.dataset.personId, reason: "Confirmed a suggested match" }), "Named.");
      if (t.dataset.unlink) {
        const p = gameView.lineup.players.find((x) => x.id === t.dataset.unlink);
        return act(() => api(`/lineups/players/${enc(p.id)}/unlink`, "POST", { expected_revision: p.link.revision, reason: "Removed a player name" }));
      }
      if (t.dataset.regroup) return act(() => api(`/lineups/${enc(t.dataset.regroup)}/regroup`, "POST", { distance_threshold: 0.62, reason: "Regroup with a looser threshold" }), "Regrouping queued.");
      if (t.dataset.deleteLineup) {
        if (!confirm("Delete this game's faces, groups and names? The video and other analysis stay.")) return;
        gameView = null; openGame = null;
        return act(() => api(`/lineups/${enc(t.dataset.deleteLineup)}/delete`, "POST", { reason: "Deleted game face data" }), "Deleted.");
      }
      if (t.dataset.personForget) {
        const p = summary.people.find((x) => x.id === t.dataset.personForget);
        if (!confirm(`Forget ${p.name} and remove their name from every game?`)) return;
        return act(() => api(`/lineups/people/${enc(p.id)}/delete`, "POST", { expected_revision: p.revision, reason: "Forget person" }));
      }
      if (t.id === "lineup-spans") {
        const sourceId = $("#lineup-form").source_id.value;
        status("Reading score histories…");
        const timeline = await api(`/sources/${enc(sourceId)}/end-timeline`);
        const cards = timeline.scorecard_reconstruction?.scorecards || [];
        const duration = state.data.sources.find((s) => s.id === sourceId)?.duration_seconds || 1e9;
        $("#lineup-span-buttons").innerHTML = cards.length ? cards.map((c, n) => {
          const start = Math.max(0, c.first_seconds - 1200), end = Math.min(duration, c.last_seconds + 600);
          return `<button class="button secondary" type="button" data-span="${start}:${end}">Game ${n + 1}: ${time(start)}–${time(end)}</button>`;
        }).join(" ") : "No posted score histories yet; enter times by hand.";
        status("");
        return;
      }
      if (t.dataset.span) {
        const [start, end] = t.dataset.span.split(":").map(Number);
        $("#lineup-form").start.value = time(start);
        $("#lineup-form").end.value = time(end);
      }
    } catch (error) { status(error.message); }
  });

  $("#lineups-content").addEventListener("submit", async (event) => {
    const f = event.target;
    event.preventDefault();
    const data = new FormData(f);
    if (f.id === "faces-filter") {
      selectedFaces.clear();
      Object.assign(faces, { q: data.get("q") || "", minGames: Number(data.get("min_games")), named: data.get("named") === "on" });
      try { await loadFaces(); render(); } catch (error) { status(error.message); }
      return;
    }
    if (f.id === "face-name") {
      const name = data.get("name").trim();
      const existing = summary.people.find((p) => p.name.toLowerCase() === name.toLowerCase());
      const also = $$("#lineups-body input[data-also]:checked").map((x) => x.dataset.also);
      const ids = [...detail.appearances.filter((a) => !a.link).map((a) => a.id), ...also];
      return act(() => api(`/faces/players/${enc(detail.id)}/name`, "PUT", {
        player_ids: ids, ...(existing ? { person_id: existing.id } : { new_person: { name, team: data.get("team") || null } }), reason: "Named a face across games",
      }), `Named in ${plural(ids.length, "game")}.`);
    }
    if (f.id === "lineup-form") {
      return act(async () => {
        const result = await api("/lineups", "POST", {
          source_id: data.get("source_id"), start_seconds: parseTime(data.get("start")), end_seconds: parseTime(data.get("end")),
          interval_seconds: Number(data.get("interval")),
        });
        toast(`Lineup started: ${result.units.length} segments.`);
      }, "Started. Segments run in the background.");
    }
    if (f.dataset.person) {
      const p = summary.people.find((x) => x.id === f.dataset.person);
      return act(() => api(`/lineups/people/${enc(p.id)}`, "PUT", { expected_revision: p.revision, name: data.get("name"), team: data.get("team") || null, note: p.note || "", reason: "Edited person" }), "Saved.");
    }
    if (f.dataset.link) {
      const player = gameView.lineup.players.find((x) => x.id === f.dataset.link);
      const name = data.get("name").trim();
      const existing = summary.people.find((p) => p.name.toLowerCase() === name.toLowerCase());
      return act(() => api(`/lineups/players/${enc(player.id)}/link`, "PUT", {
        ...(existing ? { person_id: existing.id } : { new_person: { name, team: data.get("team") || null } }),
        position: data.get("position") || null, expected_revision: player.link?.revision ?? null, reason: "Named a player",
      }), "Named.");
    }
  });

  // ---- faces on the selected recording, and name search in the archive list ---------------
  function stripHTML(sourceId) {
    const games = strip.games || [];
    const people = [...new Map(games.flatMap(g => g.people).map(p => [p.identity_id, p])).values()];
    if (!people.length) {
      const auto = strip.automatic;
      const text = {
        waiting: "queued for automatic face finding",
        running: "finding faces now (see Jobs)",
        attention: `face finding needs attention${auto?.problem ? `: ${auto.problem}` : ""}`,
        skipped: `skipped${auto?.skip_reason ? `: ${auto.skip_reason}` : ""}`,
        cancelled: "face finding was cancelled",
        done: "no on-ice faces were found",
      }[auto?.state] || "none found yet";
      const queue = !auto || ["skipped", "cancelled", "attention"].includes(auto.state);
      const skip = !auto || auto.state === "waiting";
      return `<div class="rf-head"><span>Faces · ${escapeHTML(text)}</span><span class="batch-actions">${queue ? `<button class="button secondary" type="button" data-rf-action="queue" data-rf-source="${sourceId}">Find faces</button>` : ""}${skip ? `<button class="button secondary" type="button" data-rf-action="skip" data-rf-source="${sourceId}" title="Never find faces in this recording automatically (for example a junior event)">Skip</button>` : ""}</span></div>`;
    }
    const tiles = people.slice(0, 24).map((p) => `<button type="button" class="rf-face" data-rf-face="${p.player_id}" title="${escapeHTML(p.link?.name || p.person?.name || "Unnamed")} · ${plural(p.games, "game")}">
      <img loading="lazy" src="${portraitURL(p.portrait, 112)}" alt=""><span>${escapeHTML(p.link?.name || p.person?.name || "Unnamed")}</span><span>${plural(p.games, "game")}</span></button>`).join("");
    return `<div class="rf-head"><span>Faces in this recording · select one to see all their games</span><button class="button secondary" type="button" data-rf-group="${sourceId}">Group faces</button></div><div class="rf-list">${tiles}</div>`;
  }

  async function renderStrip() {
    const sourceId = state.source;
    // The recording workspace builds its Results tab after this script loads; wait for it.
    const anchor = $("#recording-results");
    let host = $("#recording-faces");
    if (!sourceId || !anchor || $("#viewer-content")?.hidden) { if (host) host.hidden = true; return; }
    if (host && host.parentElement !== anchor) anchor.prepend(host);
    if (!host) {
      host = document.createElement("section");
      host.id = "recording-faces";
      host.setAttribute("aria-label", "Faces in this recording");
      anchor.prepend(host);
      host.addEventListener("click", async (event) => {
        const b = event.target.closest("button");
        if (!b) return;
        if (b.dataset.rfGroup) return window.hoglineFaces.openRecording(b.dataset.rfGroup);
        if (b.dataset.rfFace) return open(b.dataset.rfFace);
        if (b.dataset.rfAction) {
          try {
            const r = await api(`/lineups/automatic/${enc(b.dataset.rfSource)}/${b.dataset.rfAction}`, "POST");
            toast(r.state === "waiting" ? "Queued for automatic face finding." : `Face finding: ${r.state}.`);
            strip.at = 0;
            renderStrip();
          } catch (error) { toast(error.message, true); }
        }
      });
    }
    host.hidden = false;
    const show = () => {
      const key = `${strip.sourceId}:${strip.at}`;
      if (host.dataset.key !== key) { host.dataset.key = key; host.innerHTML = stripHTML(strip.sourceId); }
    };
    if (strip.sourceId === sourceId && Date.now() - strip.at < 60000) return show();
    if (strip.loading) return;
    if (strip.sourceId !== sourceId) { host.dataset.key = ""; host.innerHTML = '<div class="rf-head"><span>Faces · loading…</span></div>'; }
    strip.loading = true;
    try {
      const result = await api(`/faces/sources/${enc(sourceId)}`);
      Object.assign(strip, { sourceId, at: Date.now(), games: result.games, automatic: result.automatic });
      if (state.source === sourceId) show();
    } catch {
      host.hidden = true;
    } finally {
      strip.loading = false;
    }
  }

  window.hoglineFaces = {
    render: renderStrip,
    names: (sourceId) => names[sourceId] || [],
    open,
    openRecording: async (sourceId) => {
      selectedFaces.clear();
      recordingScope=sourceId; tab="games"; gameView=null; openGame=null;
      if (!dialog().open) dialog().showModal(); status("Loading this recording’s lineups…");
      try {
        await Promise.all([loadSummary(), loadFaces()]);
        const games = summary.lineups.filter(l => l.source_id === sourceId);
        if (games.length === 1) await openLineup(games[0].id);
        else { render(); status(""); }
      } catch(error) {status(error.message);}
    },
  };
  loadNames().then(() => { if ($("#search")?.value) $("#search").dispatchEvent(new Event("input")); });
})();
