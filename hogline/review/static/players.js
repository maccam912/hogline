(() => {
  let catalog = null, resumeCursor = "";
  const title = (g) => g.name || `Player ${g.id.slice(0, 8)}`;
  async function load() {
    catalog = await api("/players");
    render();
  }
  function render(query = "") {
    const groups = catalog.groups.filter((g) => g.members.length &&
      `${title(g)} ${g.id}`.toLowerCase().includes(query.toLowerCase()))
      .sort((a, b) => Number(Boolean(b.name)) - Number(Boolean(a.name)) ||
        Number(b.members.some((m) => m.preview_evidence_id)) - Number(a.members.some((m) => m.preview_evidence_id)) || a.id.localeCompare(b.id));
    $("#players-content").innerHTML = `<p class="fine-print">${escapeHTML(catalog.notice)}</p>
      <div class="batch-actions"><label>Find player<input id="player-search" value="${escapeHTML(query)}" type="search" placeholder="Name or anonymous ID"></label>
      <a class="evidence-link" href="/api/players/history.json" target="_blank" rel="noopener">Names, links and correction history · JSON ↗</a></div>
      <p>${groups.length} matching groups · naming is optional</p><div class="players-grid">${groups.slice(0, 100).map((g) => {
      const first = g.members.find((m) => m.preview_evidence_id) || g.members[0];
      return `<article class="batch-card"><div class="batch-heading"><strong>${escapeHTML(title(g))}</strong><span class="pill amber">${g.members.length} fragments</span></div>
        <p class="fine-print">Stable ID ${escapeHTML(g.id)}</p>
        <div class="player-preview">${first.preview_evidence_id ? `<img loading="lazy" src="/api/players/fragments/${encodeURIComponent(first.id)}/thumbnail" alt="Body context from an exact retained source frame">` : '<span class="fine-print">No retained image exactly matches this fragment. Inspect the original source timestamps below.</span>'}</div>
        <form data-player-name="${g.id}"><label>Optional name<input name="name" value="${escapeHTML(g.name || "")}" maxlength="160" placeholder="Keep anonymous"></label>
        <label>Notes<input name="note" value="${escapeHTML(g.note)}" maxlength="2000"></label><button class="button secondary" type="submit">Save name</button></form>
        <details><summary>Inspect or correct ${g.members.length} fragments</summary>${g.members.map((m) => `<div class="player-member"><p>${escapeHTML(m.view.replaceAll("_", " "))} · ${time(m.start_seconds)}–${time(m.end_seconds)} · ${escapeHTML(m.basis.replaceAll("_", " "))}</p>
          <button class="button secondary" type="button" data-player-inspect="${m.id}">Inspect source fragment</button>
          <form data-player-move="${m.id}"><label>Move this fragment<select name="target"><option value="">New anonymous player</option>${catalog.groups.filter((other) => other.id !== g.id).map((other) => `<option value="${other.id}">${escapeHTML(title(other))} · ${other.id.slice(0, 8)}</option>`).join("")}</select></label>
          <label>Reason<input name="reason" required maxlength="2000" placeholder="What supports this correction?"></label><button class="button secondary" type="submit">Save fragment link</button></form></div>`).join("")}</details></article>`;
    }).join("")}</div>${groups.length > 100 ? "<p>Showing the first 100 groups. Search by name or ID to narrow the list.</p>" : ""}`;
    $("#player-search").addEventListener("change", (event) => render(event.target.value));
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
        const target = catalog.groups.find((g) => g.id === data.get("target"));
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
