(() => {
  const stamp = (n) =>
    time(n) +
    (Number.isInteger(n) ? "" : (n % 1).toFixed(3).slice(1).replace(/0+$/, ""));
  const label = (s) => escapeHTML(s.replaceAll("_", " "));
  function pathPlot(track) {
    const color = track.color === "red" ? "#c6443c" : "#9b7400";
    const points = track.points.map((p) => p.center_px.join(",")).join(" ");
    const radius = Math.max(2, track.crop_width / 100);
    return `<svg class="track-plot" viewBox="0 0 ${track.crop_width} ${track.crop_height}" role="img" aria-label="${track.color} handle fragment in original crop pixels"><rect width="100%" height="100%" fill="#f5f3ed"/><path d="M0 ${track.crop_height / 2}H${track.crop_width} M${track.crop_width / 2} 0V${track.crop_height}" stroke="#d5d3cd" stroke-width="1"/><polyline points="${points}" fill="none" stroke="${color}" stroke-width="${radius}" stroke-opacity=".65"/>${track.points.map((p, i) => `<circle cx="${p.center_px[0]}" cy="${p.center_px[1]}" r="${radius}" fill="${color}"><title>${i + 1}: ${stamp(p.source_seconds)}</title></circle>`).join("")}<text x="8" y="20" font-size="14" fill="#555">Image coordinates · ${track.crop_width} × ${track.crop_height}</text></svg>`;
  }
  function renderResult(result) {
    const moving = result.tracks.filter(
      (t) => t.motion_class === "position_change_observed",
    ).length;
    const sorted = [...result.tracks].sort(
      (a, b) =>
        Number(b.motion_class === "position_change_observed") -
          Number(a.motion_class === "position_change_observed") ||
        a.start_seconds - b.start_seconds,
    );
    $("#tracks-content").innerHTML =
      `<p>${escapeHTML(result.notice)}</p><p><strong>${result.tracks.length} fragments · ${moving} with net position change</strong></p><p class="fine-print">Lines connect discrete observations; they do not establish the path between samples. Coordinates are pixels in each overhead crop. YouTube seeks use whole seconds; the sample list preserves precise timestamps.</p><a class="evidence-link" href="/api/window-tracks/${encodeURIComponent(result.id)}">Download all fragments and original timestamps · JSON ↓</a><details><summary>Why observations were excluded</summary>${Object.entries(
        result.diagnostics,
      )
        .map(([k, v]) => `<p class="fine-print">${label(k)}: ${v}</p>`)
        .join("")}</details>${
        sorted.length
          ? sorted
              .slice(0, 50)
              .map(
                (track, i) =>
                  `<article class="batch-card"><div class="batch-heading"><strong>Fragment ${i + 1} · ${label(track.color)} · ${track.view === "house_top" ? "Top" : "Bottom"} overhead</strong><span class="pill amber">proposed</span></div><p>${stamp(track.start_seconds)}–${stamp(track.end_seconds)} · ${track.points.length} samples · ${track.net_displacement_px.toFixed(1)} px net displacement</p>${pathPlot(track)}<p class="fine-print">${label(track.motion_class)} · ends at ${label(track.termination_reason)}</p><div class="batch-actions">${[
                    ["Check start", track.points[0]],
                    ["Check end", track.points.at(-1)],
                  ]
                    .map(([title, p]) =>
                      p.youtube_url
                        ? `<a class="evidence-link" href="${escapeHTML(p.youtube_url)}" target="_blank" rel="noopener">${title} on YouTube ↗</a>`
                        : "",
                    )
                    .join(
                      "",
                    )}</div><details><summary>Original sample timestamps and positions</summary><ol class="track-samples">${track.points.map((p) => `<li>${p.youtube_url ? `<a href="${escapeHTML(p.youtube_url)}" target="_blank" rel="noopener">${stamp(p.source_seconds)}</a>` : stamp(p.source_seconds)} · (${p.center_px.map((v) => v.toFixed(1)).join(", ")}) px · PTS ${p.pts} × ${p.time_base_num}/${p.time_base_den}</li>`).join("")}</ol></details></article>`,
              )
              .join("")
          : "<p>No fragments passed the continuity and visibility checks. This does not establish that no stones moved.</p>"
      }${sorted.length > 50 ? `<p>Showing 50 of ${sorted.length} fragments; the download includes all fragments.</p>` : ""}`;
  }
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-window-tracks]");
    if (!button) return;
    button.disabled = true;
    $("#tracks-content").textContent = "Reading cached observations…";
    $("#tracks-dialog").showModal();
    try {
      const existing = state.data.window_tracks?.find(
        (t) =>
          t.window_id === button.dataset.windowTracks &&
          t.version === state.data.window_tracking_version,
      );
      const result = existing
        ? await api(`/window-tracks/${encodeURIComponent(existing.id)}`)
        : await api(
            `/windows/${encodeURIComponent(button.dataset.windowTracks)}/tracks`,
            "POST",
          );
      renderResult(result);
      await refresh();
    } catch (error) {
      $("#tracks-content").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
})();
