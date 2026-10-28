(() => {
  const stamp = (n) =>
    time(n) +
    (Number.isInteger(n) ? "" : (n % 1).toFixed(3).slice(1).replace(/0+$/, ""));
  const label = (s) => escapeHTML(s.replaceAll("_", " "));
  const link = (p, title) =>
    p.youtube_url
      ? `<a class="evidence-link" href="${escapeHTML(p.youtube_url)}" target="_blank" rel="noopener">${title} · ${stamp(p.source_seconds)} on YouTube ↗</a>`
      : "";
  function renderResult(result, focusId) {
    const context = result.context_anchor_evidence_ids
      .map((id) => state.data.evidence.find((e) => e.id === id))
      .filter(Boolean)
      .sort((a, b) => a.source_seconds - b.source_seconds);
    const focusedFirst = (items) =>
      [...items].sort(
        (a, b) => Number(b.id === focusId) - Number(a.id === focusId),
      );
    const stateCard = (s) => {
      const exact = context.filter(
        (e) =>
          Math.abs(e.source_seconds - s.start_seconds) < 0.00001 ||
          Math.abs(e.source_seconds - s.end_seconds) < 0.00001,
      );
      return `<article class="batch-card" ${s.id === focusId ? 'data-house-focused="true"' : ""}>
        <div class="batch-heading"><strong>${s.id === focusId ? "Selected state · " : ""}${s.view === "house_top" ? "Top" : "Bottom"} overhead · ${s.candidate_count} handle detections</strong><span class="pill amber">proposed</span></div>
        <p>${stamp(s.start_seconds)}–${stamp(s.end_seconds)} · ${s.sample_indices.length} samples</p>
        <p>${
          s.candidate_count
            ? Object.entries(s.color_counts)
                .map(([color, count]) => `${count} ${label(color)}`)
                .join(" · ")
            : "No handle detections. Hidden and missed stones remain unknown."
        }</p>
        <div class="batch-actions">${link(s.first, "Check start")}${link(s.last, "Check end")}</div>
        ${exact.map((e) => `<figure class="boundary-frame"><figcaption>Retained state endpoint · ${stamp(e.source_seconds)}</figcaption><img loading="lazy" src="/api/evidence/${encodeURIComponent(e.id)}/${s.view}" width="${e.crops[s.view].width}" height="${e.crops[s.view].height}" alt="${label(s.view)} at state endpoint ${stamp(e.source_seconds)}"></figure>`).join("")}
        <details><summary>Original endpoint timestamps</summary><p class="fine-print">First: PTS ${s.first.pts} × ${s.first.time_base_num}/${s.first.time_base_den}<br>Last: PTS ${s.last.pts} × ${s.last.time_base_num}/${s.last.time_base_den}<br>Sample indices: ${s.sample_indices.join(", ")}</p></details>
      </article>`;
    };
    const changeCard = (
      c,
    ) => `<article class="batch-card" ${c.id === focusId ? 'data-house-focused="true"' : ""}>
      <div class="batch-heading"><strong>${c.id === focusId ? "Selected change · " : ""}${label(c.kind)} · ${c.view === "house_top" ? "Top" : "Bottom"} overhead</strong><span class="pill amber">proposed</span></div>
      <p>${stamp(c.earliest_seconds)}–${stamp(c.latest_seconds)} · uncertain change bracket</p><p class="fine-print">Played-end boundary and official end number remain unknown.${c.intervening_flags.length ? ` Intervening checks: ${c.intervening_flags.map(label).join(", ")}.` : ""}</p>
      <div class="batch-actions">${link(c.before, "Before")}${link(c.after, "After")}</div></article>`;
    $("#house-states-content").innerHTML = `<p>${escapeHTML(result.notice)}</p>
      <p><strong>${result.states.length} stable runs · ${result.transitions.length} change brackets</strong></p>
      <a class="evidence-link" href="/api/window-house-states/${encodeURIComponent(result.id)}" target="_blank" rel="noopener">Open full analysis · JSON ↗</a>
      ${result.transitions.length ? `<h3>Changes between sampled states</h3>${focusedFirst(result.transitions).map(changeCard).join("")}` : "<p>No change passed the continuity checks. Unobserved changes remain possible.</p>"}
      <h3>Stable sampled runs</h3>${result.states.length ? focusedFirst(result.states).map(stateCard).join("") : "<p>No run passed the current checks.</p>"}
      <details><summary>Exclusions and unresolved gaps</summary>${
        Object.entries(result.diagnostics)
          .map(([k, v]) => `<p class="fine-print">${label(k)}: ${v}</p>`)
          .join("") || "<p>No exclusions recorded.</p>"
      }</details>
      <details><summary>Retained window context · ${context.length} frames</summary><p class="fine-print">These images have their own source times and may fall outside a stable run. Check the original interval before interpreting changes.</p>${context.map((e) => `<figure class="boundary-frame"><figcaption>Window context · ${stamp(e.source_seconds)}</figcaption><img loading="lazy" src="/api/evidence/${encodeURIComponent(e.id)}/composite" width="${e.image.width}" height="${e.image.height}" alt="Window context at ${stamp(e.source_seconds)}">${link(e, "Check context")}</figure>`).join("")}</details>`;
    $("#house-states-content [data-house-focused]")?.scrollIntoView({
      block: "nearest",
    });
  }
  async function openStates(windowId, focusId = null, resultId = null) {
    $("#house-states-content").textContent = "Reading cached observations…";
    if (!$("#house-states-dialog").open) $("#house-states-dialog").showModal();
    try {
      const existing = state.data.window_house_states?.find(
        (r) =>
          r.window_id === windowId &&
          r.version === state.data.house_states_version,
      );
      const id = resultId || existing?.id;
      const result = id
        ? await api(`/window-house-states/${encodeURIComponent(id)}`)
        : await api(
            `/windows/${encodeURIComponent(windowId)}/house-states`,
            "POST",
          );
      if (
        focusId &&
        ![...result.states, ...result.transitions].some((s) => s.id === focusId)
      )
        throw Error(
          "The selected state or change is unavailable. Run the moment search again.",
        );
      renderResult(result, focusId);
      await refresh();
    } catch (error) {
      $("#house-states-content").textContent = error.message;
    }
  }
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-window-house-states]");
    if (!button) return;
    button.disabled = true;
    try {
      await openStates(button.dataset.windowHouseStates);
    } finally {
      button.disabled = false;
    }
  });
  window.hoglineHouseStates = { open: openStates };
})();
