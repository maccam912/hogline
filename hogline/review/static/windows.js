(() => {
  const form = () => $("#window-form");
  const stamp = (n) =>
    time(n) +
    (Number.isInteger(n) ? "" : (n % 1).toFixed(3).slice(1).replace(/0+$/, ""));
  const currentVersion = (w) =>
    w.version === state.data.window_version &&
    w.model_version === state.data.window_detector_version &&
    w.visibility_version === state.data.window_visibility_version &&
    w.decoder_version === state.data.window_decoder_version;
  function renderWindows() {
    const windows = (state.data.analysis_windows || [])
      .filter((w) => w.source_id === state.source)
      .sort(
        (a, b) =>
          Number(currentVersion(b)) - Number(currentVersion(a)) ||
          (b.created_at || "").localeCompare(a.created_at || ""),
      );
    $("#window-list").innerHTML = windows.length
      ? windows
          .map((w) => {
            const stale =
              w.source_metadata_sha256 !== source()?.metadata_sha256;
            const older = !currentVersion(w);
            const anchors = state.data.evidence
              .filter((e) => w.anchor_evidence_ids.includes(e.id))
              .sort((a, b) => a.source_seconds - b.source_seconds);
            return `<article class="batch-card"><div class="batch-heading"><strong>${stamp(w.request.start_seconds)}–${stamp(w.request.end_seconds)}</strong><span class="pill ${stale || older ? "amber" : ""}">${stale ? "earlier source manifest" : older ? "earlier analysis version" : "proposed"}</span></div>
          ${older ? '<p class="fine-print">This run predates the current vision checks. Reanalyze it to apply the current method.</p>' : ""}
          <p>${w.sample_count} discrete samples in each overhead view · ${w.anchor_evidence_ids.length} retained frames</p>
          ${anchors.length ? `<div class="batch-actions"><button class="button quiet" data-window-evidence="${escapeHTML(anchors[0].id)}" data-window-view="composite">View window start →</button><button class="button quiet" data-window-evidence="${escapeHTML(anchors.at(-1).id)}" data-window-view="composite">View window end →</button></div>` : ""}
          <p class="fine-print">Observed ${stamp(w.observed_first_seconds)}–${stamp(w.observed_last_seconds)} · ${w.request.interval_seconds}s minimum interval · ${(w.parquet.bytes / 1024).toFixed(1)} KB of dense observations</p>
          ${w.candidates.length ? w.candidates.map((c) => `<div class="window-candidate"><strong>${c.view === "house_top" ? "Top" : "Bottom"} overhead · ${c.candidate_count} persistent handle candidates</strong><p class="fine-print">${stamp(c.start_seconds)}–${stamp(c.end_seconds)} · ${c.sample_indices.length} samples · scoring unresolved</p><div class="batch-actions"><button class="button quiet" data-window-evidence="${escapeHTML(c.evidence_ids[0])}" data-window-view="${c.view}">Inspect first anchor →</button><button class="button quiet" data-window-evidence="${escapeHTML(c.evidence_ids.at(-1))}" data-window-view="${c.view}">Inspect last anchor →</button>${c.youtube_url ? `<a class="evidence-link" href="${escapeHTML(c.youtube_url)}" target="_blank" rel="noopener">Check on YouTube ↗</a>` : ""}</div></div>`).join("") : "<p>No configuration passed the current vision checks. Hidden or undetected stones remain unknown.</p>"}
          ${w.deferred_candidate_count ? `<p class="fine-print">${w.deferred_candidate_count} further configurations remain in the dense observations without retained candidate anchors.</p>` : ""}
          ${
            w.diagnostics
              ? `<details><summary>Visibility and motion checks</summary>${Object.entries(
                  w.diagnostics,
                )
                  .map(
                    ([view, d]) =>
                      `<p class="fine-print">${view === "house_top" ? "Top" : "Bottom"}: ${d.samples} samples · ${d.no_handle_candidates} without handle candidates · ${d.large_dark_regions} with large dark regions · ${d.ring_obstructions || 0} with blue-ring obstruction · ${d.missing_ring_reference || 0} without a usable ring reference · ${d.clipped_handle_candidates} with clipped candidates · ${d.high_motion} with high motion · ${d.rejected_color_candidates || 0} color candidates rejected by shape/size checks. Checks can overlap; absence of a flag does not establish clear ice.</p>`,
                  )
                  .join("")}</details>`
              : ""
          }
          <div class="batch-actions"><a class="evidence-link" href="/api/windows/${encodeURIComponent(w.id)}/observations.parquet">Download observations · Parquet ↓</a><button class="button quiet" data-window-rerun="${escapeHTML(w.id)}">Reanalyze this window →</button></div></article>`;
          })
          .join("")
      : '<p class="fine-print">No detailed windows yet. Pick a source time to inspect repeated observations around a moment.</p>';
  }
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.windowRerun) {
      const windowRun = state.data.analysis_windows.find(
        (w) => w.id === button.dataset.windowRerun,
      );
      button.disabled = true;
      try {
        await api("/windows", "POST", {
          ...windowRun.request,
          run_nonce: crypto.randomUUID(),
        });
        toast(
          "A new analysis run is queued. The previous window remains in history.",
        );
        await refresh();
      } catch (error) {
        toast(error.message, true);
      } finally {
        button.disabled = false;
      }
      return;
    }
    if (button.id === "window-button") {
      form().dataset.sourceId = state.source;
      form().elements.start.value = stamp(state.seconds);
      $("#window-dialog").showModal();
    }
    if (button.dataset.windowEvidence) {
      if (window.hoglineLabels && !window.hoglineLabels.beforeNavigate())
        return;
      const frame = state.data.evidence.find(
        (e) => e.id === button.dataset.windowEvidence,
      );
      if (!frame)
        return toast(
          "The retained anchor is unavailable. Reload this recording.",
          true,
        );
      state.seconds = frame.source_seconds;
      state.view = button.dataset.windowView;
      render();
      $("#frame-stage")?.scrollIntoView({
        block: "center",
        behavior: "smooth",
      });
    }
  });
  form().addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    try {
      const data = new FormData(form());
      const src = state.data.sources.find(
        (s) => s.id === form().dataset.sourceId,
      );
      const start = parseTime(data.get("start"));
      const end = Math.min(
        start + Number(data.get("duration")),
        src.duration_seconds || Infinity,
      );
      await api("/windows", "POST", {
        source_id: src.id,
        start_seconds: start,
        end_seconds: end,
        interval_seconds: Number(data.get("interval")),
        max_frames: 240,
        max_evidence_mb: Number(data.get("budget")),
        max_worker_seconds: Number(data.get("worker")),
      });
      $("#window-dialog").close();
      toast(
        "Detailed window queued. Results will appear under House configurations.",
      );
      await refresh();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  window.hoglineWindows = { render: renderWindows };
})();
