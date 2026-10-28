(() => {
  let catalog = null, resumeCursor = "", currentQuery = "", onlyUncertain = false;
  const needsReview = (g) => g.members.some((m) => m.matching_review_needed);
  const title = (g) => g.name || `Player ${g.id.slice(0, 8)}`;
  async function load() {
    catalog = await api("/players");
    render();
  }
  function render(query = currentQuery) {
    currentQuery = query;
    const groups = catalog.groups.filter((g) => g.members.length && (!onlyUncertain || needsReview(g)) &&
      `${title(g)} ${g.id}`.toLowerCase().includes(query.toLowerCase()))
      .sort((a, b) => Number(needsReview(b)) - Number(needsReview(a)) || Number(Boolean(b.name)) - Number(Boolean(a.name)) ||
        Number(b.members.some((m) => m.preview_evidence_id)) - Number(a.members.some((m) => m.preview_evidence_id)) || a.id.localeCompare(b.id));
    $("#players-content").innerHTML = `<p class="fine-print">Grouping uses body continuity. One person may appear in several groups. Check uncertain matches below; names are optional.</p>
      <div class="batch-actions"><label>Find player<input id="player-search" value="${escapeHTML(query)}" type="search" placeholder="Name or anonymous ID"></label>
      <a class="evidence-link" href="/api/players/history.json" target="_blank" rel="noopener">Names, links and correction history · JSON ↗</a></div>
      <label class="check-row"><input id="player-uncertain" type="checkbox" ${onlyUncertain ? "checked" : ""}> Only uncertain matches · ${catalog.groups.filter(needsReview).length}</label>
      <datalist id="player-targets">${catalog.groups.map((g) => `<option value="${g.id}">${escapeHTML(title(g))}</option>`).join("")}</datalist>
      <p>${groups.length} matching groups · naming is optional</p><div class="players-grid">${groups.slice(0, 100).map((g) => {
      const first = g.members.find((m) => m.preview_evidence_id) || g.members[0];
      return `<article class="batch-card"><div class="batch-heading"><strong>${escapeHTML(title(g))}</strong><span class="pill amber">${needsReview(g) ? "Match uncertain" : `${g.members.length} fragments`}</span></div>
        <p class="fine-print">Stable ID ${escapeHTML(g.id)}</p>
        <div class="player-preview">${first.preview_evidence_id ? `<img loading="lazy" src="/api/players/fragments/${encodeURIComponent(first.id)}/thumbnail" alt="Body context from an exact retained source frame">` : '<span class="fine-print">No retained image exactly matches this fragment. Inspect the original source timestamps below.</span>'}</div>
        <details><summary>Optional name and notes</summary><form data-player-name="${g.id}"><label>Optional name<input name="name" value="${escapeHTML(g.name || "")}" maxlength="160" placeholder="Keep anonymous"></label>
        <label>Notes<input name="note" value="${escapeHTML(g.note)}" maxlength="2000"></label><button class="button secondary" type="submit">Save name</button></form></details>
        <details ${needsReview(g) ? "open" : ""}><summary>Inspect or correct ${g.members.length} fragments</summary>${g.members.map((m) => `<div class="player-member"><p>${escapeHTML(m.view.replaceAll("_", " "))} · ${time(m.start_seconds)}–${time(m.end_seconds)} · ${escapeHTML(m.basis.replaceAll("_", " "))}</p>
          <button class="button secondary" type="button" data-player-inspect="${m.id}">Inspect source fragment</button>
          ${m.matching_candidates.length ? `<p class="fine-print">Possible matching groups:</p><div class="batch-actions">${m.matching_candidates.map((c) => `<button class="button secondary" type="button" data-player-inspect="${c.assignment_id}">Compare ${escapeHTML(c.name || `Player ${c.group_id.slice(0, 8)}`)}</button>`).join("")}</div>` : ""}
          <form data-player-move="${m.id}"><label>Move this fragment to<input name="target" list="player-targets" placeholder="Name or player ID; blank creates new"></label>
          <label>Reason<input name="reason" required maxlength="2000" placeholder="What supports this correction?"></label><button class="button secondary" type="submit">Save fragment link</button></form></div>`).join("")}</details></article>`;
    }).join("")}</div>${groups.length > 100 ? "<p>Showing the first 100 groups. Search by name or ID to narrow the list.</p>" : ""}`;
    $("#player-search").addEventListener("change", (event) => render(event.target.value));
    $("#player-uncertain").addEventListener("change", (event) => { onlyUncertain = event.target.checked; render(); });
    document.querySelectorAll(".player-preview img").forEach((img) => {
      img.addEventListener("error", () => img.remove());
    });
  }
  $("#players-button").addEventListener("click", async () => {
    $("#players-dialog").showModal();
    $("#players-status").textContent = "Indexing anonymous fragments…";
    try {
      let cursor = resumeCursor, batches = 0;
      do {
        const result = await api(`/players/sync?after_id=${encodeURIComponent(cursor)}&limit=100`, "POST");
        cursor = result.next_cursor;
      } while (cursor && ++batches < 10);
      resumeCursor = cursor || "";
      await load();
      $("#players-status").textContent = cursor ? "More historical fragments remain. Reopen Players to continue indexing." : "";
    } catch (error) {
      $("#players-status").textContent = error.message;
    }
  });
  $("#players-content").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-player-inspect]");
    if (!button) return;
    const member = catalog.groups.flatMap((g) => g.members).find((m) => m.id === button.dataset.playerInspect);
    await window.hoglineActors.open(member.window_id, member.track_id, member.actor_result_id);
  });
  $("#players-content").addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form.matches("[data-player-name], [data-player-move]")) return;
    event.preventDefault();
    const data = new FormData(form), button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      if (form.dataset.playerName) {
        const group = catalog.groups.find((g) => g.id === form.dataset.playerName);
        await api(`/players/groups/${encodeURIComponent(group.id)}`, "PUT", {
          expected_revision: group.revision, name: data.get("name") || null, note: data.get("note"),
          author: "Local operator", reason: "Set optional player display name and notes",
        });
      } else {
        const source = catalog.groups.find((g) => g.members.some((m) => m.id === form.dataset.playerMove));
        const member = source.members.find((m) => m.id === form.dataset.playerMove);
        const targetText = data.get("target").trim();
        const targets = targetText ? catalog.groups.filter((g) => g.id === targetText || title(g) === targetText) : [];
        if (targetText && targets.length !== 1) throw Error("Choose one matching player from the suggestions, or leave the target blank for a new anonymous group.");
        const target = targets[0];
        await api(`/players/fragments/${encodeURIComponent(member.id)}`, "PUT", {
          expected_revision: member.revision, expected_source_revision: source.revision,
          target_group_id: target?.id || null, expected_target_revision: target?.revision || null,
          author: "Local operator", reason: data.get("reason"),
        });
      }
      await load();
      $("#players-status").textContent = "Saved. The correction history retains the previous grouping.";
    } catch (error) {
      $("#players-status").textContent = error.message;
      button.disabled = false;
    }
  });
})();
