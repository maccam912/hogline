(() => {
  const stamp = (n) =>
    time(n) +
    (Number.isInteger(n) ? "" : (n % 1).toFixed(3).slice(1).replace(/0+$/, ""));
  const label = (s) => escapeHTML(s.replaceAll("_", " "));
  function renderActors(result, focusId) {
    const source = state.data.sources.find((s) => s.id === result.source_id);
    const sourceLink = (point, title) => {
      const url =
        point.youtube_url ||
        (source?.platform === "local"
          ? `/api/media/${encodeURIComponent(source.id)}#t=${point.source_seconds}`
          : null);
      return url
        ? `<a class="evidence-link" href="${escapeHTML(url)}" target="_blank" rel="noopener">${title} · ${stamp(point.source_seconds)} ${point.youtube_url ? "on YouTube" : "in local video"} ↗</a>`
        : "";
    };
    const context = result.context_anchor_evidence_ids
      .map((id) => state.data.evidence.find((e) => e.id === id))
      .filter(Boolean)
      .sort((a, b) => a.source_seconds - b.source_seconds);
    const tracks = [...result.tracks].sort(
      (a, b) =>
        Number(b.id === focusId) - Number(a.id === focusId) ||
        Number(b.motion_class === "view_position_change_observed") -
          Number(a.motion_class === "view_position_change_observed") ||
        a.start_seconds - b.start_seconds ||
        a.id.localeCompare(b.id),
    );
    $("#actors-content").innerHTML =
      `<p>${escapeHTML(result.notice)}</p><p><strong>${tracks.length} anonymous body fragments</strong></p><p class="fine-print">One person may have several fragments. Thrower, delivery and named identity remain unresolved.</p><a class="evidence-link" href="/api/window-actors/${encodeURIComponent(result.id)}" target="_blank" rel="noopener">Full analysis and source timestamps · JSON ↗</a>
      ${
        tracks
          .slice(0, 40)
          .map((track, i) => {
            const first = track.points[0],
              last = track.points.at(-1);
            const exact = context
              .map((e) => ({
                frame: e,
                point: track.points.find(
                  (p) =>
                    Math.abs(p.source_seconds - e.source_seconds) < 0.00001 &&
                    p.pts === e.pts &&
                    p.time_base_num === e.time_base_num &&
                    p.time_base_den === e.time_base_den,
                ),
              }))
              .filter(
                ({ frame, point }) =>
                  point &&
                  frame.crops[track.view].width === track.crop_width &&
                  frame.crops[track.view].height === track.crop_height,
              )
              .slice(0, 2);
            return `<article class="batch-card"><div class="batch-heading"><strong>${track.id === focusId ? "Selected fragment" : `Body fragment ${i + 1}`} · ${track.view === "perspective_left" ? "Left" : "Right"} perspective</strong><span class="pill amber">proposed</span></div><p>${stamp(track.start_seconds)}–${stamp(track.end_seconds)} · ${track.points.length} samples</p><p class="fine-print">${label(track.motion_class)} · ${track.low_posture_candidate_samples} low-posture cues · ends at ${label(track.termination_reason)}. Sheet membership remains unverified.</p><div class="batch-actions">${sourceLink(first, "Check start")}${sourceLink(last, "Check end")}</div>
          <details><summary>Exact retained frame matches · ${exact.length}</summary>${exact.map(({ frame, point }) => `<figure class="boundary-frame"><figcaption>Body proposal at ${stamp(point.source_seconds)} · same original frame</figcaption><div class="actor-evidence"><img loading="lazy" src="/api/evidence/${encodeURIComponent(frame.id)}/${track.view}" width="${track.crop_width}" height="${track.crop_height}" alt="Exact perspective context for anonymous body fragment"><svg viewBox="0 0 ${track.crop_width} ${track.crop_height}" role="img" aria-label="Proposed person bounding box"><rect x="${point.bbox_px[0]}" y="${point.bbox_px[1]}" width="${point.bbox_px[2]}" height="${point.bbox_px[3]}" fill="none" stroke="#52ef89" stroke-width="3"/></svg></div></figure>`).join("") || "<p>No retained frame exactly matches these samples. Use the original source links.</p>"}</details>
          <details><summary>Original sample timestamps</summary><ol class="track-samples">${track.points.map((p) => `<li>${stamp(p.source_seconds)} · PTS ${p.pts} × ${p.time_base_num}/${p.time_base_den} · box (${p.bbox_px.map((v) => v.toFixed(1)).join(", ")}) px</li>`).join("")}</ol></details></article>`;
          })
          .join("") ||
        "<p>No body association passed the current continuity checks. This does not establish that no players were present.</p>"
      }
      ${tracks.length > 40 ? `<p>Showing 40 of ${tracks.length} fragments; the full analysis includes all fragments.</p>` : ""}
      <details><summary>Association exclusions</summary>${Object.entries(
        result.diagnostics,
      )
        .map(([k, v]) => `<p class="fine-print">${label(k)}: ${v}</p>`)
        .join("")}</details>`;
  }
  async function openActors(windowId, focusId = null, resultId = null) {
    $("#actors-content").textContent =
      "Reading cached perspective observations…";
    if (!$("#actors-dialog").open) $("#actors-dialog").showModal();
    try {
      const existing = state.data.window_actors?.find(
        (r) =>
          r.window_id === windowId && r.version === state.data.actor_version,
      );
      const id = resultId || existing?.id;
      const result = id
        ? await api(`/window-actors/${encodeURIComponent(id)}`)
        : await api(`/windows/${encodeURIComponent(windowId)}/actors`, "POST");
      if (focusId && !result.tracks.some((t) => t.id === focusId))
        throw Error("The selected body fragment is unavailable. Search again.");
      renderActors(result, focusId);
      await refresh();
    } catch (error) {
      $("#actors-content").textContent = error.message;
    }
  }
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-window-actors]");
    if (!button) return;
    button.disabled = true;
    try {
      await openActors(button.dataset.windowActors);
    } finally {
      button.disabled = false;
    }
  });
  window.hoglineActors = { open: openActors };
})();
