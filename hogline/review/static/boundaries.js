(() => {
  const links = () => state.data.source_boundary_links || [];
  const label = (link) =>
    link.kind === "possible_continuation"
      ? "Possible continuation"
      : "Nearby recording gap";
  function render() {
    const related = links().filter((p) =>
      [p.left_source_id, p.right_source_id].includes(state.source),
    );
    $("#source-boundary-panel").hidden = !related.length;
    $("#source-boundary-list").innerHTML = related
      .map(
        (p) =>
          `<article class="activity-card"><div><strong>${label(p)}</strong><p class="fine-print">${time(p.estimated_recording_gap_seconds)} estimated recording gap · ${p.clock_alternatives.length} clock alternatives · ${p.board_comparison.agreeing_colors.length ? `posted ${p.board_comparison.agreeing_colors.join(" / ")} row agrees` : "board continuity unresolved"}</p></div><button class="button secondary" data-boundary="${p.id}">Compare recordings →</button></article>`,
      )
      .join("");
  }
  function compare(id) {
    const p = links().find((p) => p.id === id);
    if (!p)
      return toast("This proposal changed. Reload its source evidence.", true);
    $("#boundary-comparison").innerHTML =
      `<h3>${label(p)}</h3><p>${time(p.estimated_recording_gap_seconds)} estimated gap between recordings (±${p.gap_uncertainty_seconds.toFixed(1)}s conditional clock/duration uncertainty). ${time(p.unobserved_between_nearest_anchors_seconds)} between the nearest retained samples remains unobserved.</p><p class="fine-print">The mapping extrapolates ${p.boundary_extrapolation_seconds.left}s after the earlier sample and ${p.boundary_extrapolation_seconds.right}s before the later sample. AM/PM and source clocks remain proposed; a clock change in the unsampled boundary interval is unknown. Deliveries and end boundaries inside the missing footage are unknown.</p>
    ${[
      ["Earlier recording", p.left, p.left_source_id],
      ["Later recording", p.right, p.right_source_id],
    ]
      .map(([caption, span, sourceId]) => {
        const s = state.data.sources.find((s) => s.id === sourceId);
        return `<figure class="boundary-frame"><figcaption><strong>${caption} · ${escapeHTML(s.title)}</strong><span class="fine-print">${time(s.duration_seconds)} recording · sampled ${time(span.source_start_seconds)}–${time(span.source_end_seconds)}</span></figcaption><img src="/api/evidence/${span.nearest_evidence_id}/composite" alt="${caption} nearest boundary evidence">${span.youtube_url ? `<a class="evidence-link" href="${escapeHTML(span.youtube_url)}" target="_blank" rel="noopener">Check this sample on YouTube ↗</a>` : ""}</figure>`;
      })
      .join(
        "",
      )}<p class="fine-print">${p.board_comparison.agreeing_colors.length ? `Readable ${p.board_comparison.agreeing_colors.join(" / ")} posted rows agree across at least two distinct samples per recording.` : "Readable board states do not establish continuity."} This is source evidence for review; no game, delivery count, or final result has been accepted.</p><button class="button primary" data-use-boundary="${p.id}">Use these spans in a game draft →</button>`;
    $("#boundary-dialog").showModal();
  }
  function use(id) {
    if (window.hoglineLabels && !window.hoglineLabels.beforeNavigate()) return;
    const p = links().find((p) => p.id === id);
    if (!p) return toast("Proposal changed; reopen it before editing.", true);
    const existing = !!state.draft;
    if (existing) {
      readEditor();
      if (state.draft.game.sheet !== p.sheet)
        return toast(`Open a game on sheet ${p.sheet} to use this link.`, true);
    } else newGame();
    const left = state.data.sources.find((s) => s.id === p.left_source_id);
    const right = state.data.sources.find((s) => s.id === p.right_source_id);
    const spans = [
      {
        source_id: left.id,
        start_seconds: p.left.source_start_seconds,
        end_seconds: left.duration_seconds,
        relationship: existing ? "unresolved" : "primary",
        boundary_uncertainty_seconds: p.boundary_extrapolation_seconds.left,
      },
      {
        source_id: right.id,
        start_seconds: 0,
        end_seconds: Math.min(
          right.duration_seconds,
          Math.max(1, p.right.source_end_seconds),
        ),
        relationship: "unresolved",
        boundary_uncertainty_seconds: p.boundary_extrapolation_seconds.right,
      },
    ];
    if (!existing) {
      state.draft.game.spans = [];
      state.draft.game.label = `Possible shared game · sheet ${p.sheet}`;
    }
    for (const span of spans)
      if (
        !state.draft.game.spans.some(
          (s) =>
            s.source_id === span.source_id &&
            s.start_seconds <= span.start_seconds &&
            s.end_seconds >= span.end_seconds,
        )
      )
        state.draft.game.spans.push(span);
    state.draft.game.acceptance = "proposed";
    state.draft.game.coverage = "partial";
    state.draft.game.note += `\nRecording link ${p.id}: proposed ${p.estimated_recording_gap_seconds}s gap; missing deliveries and end boundaries remain unknown. These are short evidence spans; review and extend the game boundaries separately.`;
    $("#boundary-dialog").close();
    renderEditor();
    toast(
      "Spans added to the draft. Review membership and boundaries before saving.",
    );
  }
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.boundary) compare(button.dataset.boundary);
    if (button.dataset.useBoundary) use(button.dataset.useBoundary);
  });
  window.hoglineBoundaries = { render };
  render();
})();
