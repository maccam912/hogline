(() => {
  let selectedSource = null, generation = 0;
  const text = (value) => escapeHTML(String(value));
  const label = (value) => text(value.replaceAll("_", " "));
  const score = (value) => `${value?.red ?? "?"}–${value?.yellow ?? "?"}`;
  const stamp = (value) => value === null || value === undefined ? "unknown" :
    time(value) + (Number.isInteger(value) ? "" : (value % 1).toFixed(3).slice(1).replace(/0+$/, ""));
  const link = (anchor, title) => anchor?.youtube_url
    ? `<a class="evidence-link" target="_blank" rel="noopener" href="${text(anchor.youtube_url)}">${title} · ${stamp(anchor.source_seconds)} ↗</a>` : "";
  const range = (a, b) => `${stamp(a)}–${stamp(b)}`;
  function renderTimeline(result) {
    $("#end-timeline-content").innerHTML = `<p>${text(result.notice)}</p>
      <p>${result.coverage.board_observation_frames} board frames · ${result.coverage.paired_row_epochs} repeated two-row readings · ${result.end_candidates.length} possible clearings</p>
      <p><a class="evidence-link" href="/api/sources/${encodeURIComponent(selectedSource)}/end-timeline" target="_blank" rel="noopener">Open complete end timeline JSON ↗</a></p>
      <h3>End and score histories</h3>
      ${result.board_ledgers.length ? result.board_ledgers.map((book, i) => `<article class="batch-card">
        <h4>Board history ${i + 1} · ${range(book.first_seconds, book.last_seconds)}</h4>
        <p>Highest observed end: ${book.highest_observed_end_number ?? "unknown"}. Ends played: unknown. Scheduled ends: unknown. Final result: unknown.</p>
        <p class="fine-print">History starts at ${label(book.boundary_reason)}. Review game boundaries before assigning these scores.</p>
        ${book.ends.length ? `<div class="end-table-wrap"><table class="end-table"><thead><tr><th>End</th><th>End points R–Y</th><th>Score after R–Y</th><th>Board posted/readable</th><th>Play start / finish</th></tr></thead><tbody>${book.ends.map((end) => `<tr><td>${end.official_number}</td><td>${score(end.points)}</td><td>${score(end.score_after_end)}</td><td>${end.board_posting ? `${end.board_posting.left_censored ? "Already visible by " + stamp(end.board_posting.latest_seconds) : range(end.board_posting.earliest_seconds, end.board_posting.latest_seconds)}<br>${link(end.board_posting.first_observation, "Check board")}` : "Unknown; blank or missing"}</td><td>Unknown / unknown</td></tr>`).join("")}</tbody></table></div>` : "<p>No numbered scoring cards; zero scores are not inferred.</p>"}
        </article>`).join("") : "<p>Both board rows need three shared readable frames before a score history can be proposed.</p>"}
      <h3>First visible house arrivals</h3>
      ${result.arrival_candidates.length ? result.arrival_candidates.map((arrival) => `<article class="batch-card"><h4>${label(arrival.view)} · ${range(arrival.before.source_seconds, arrival.after.source_seconds)}</h4><p>House handles became visible between these observations. This may be an arrival, placement or detection recovery. The first throw and official end number remain unknown.</p>${link(arrival.before, "Before")}${link(arrival.after, "First visible handles")}</article>`).join("") : "<p>No first-handle appearance bracket is supported yet.</p>"}
      <h3>Play and clearing candidates</h3>
      ${result.end_candidates.length ? result.end_candidates.map((candidate) => `<article class="batch-card">
        <h4>${label(candidate.view)} · possible clearing ${range(candidate.clearing.earliest_seconds, candidate.clearing.latest_seconds)}</h4>
        <p>Pre-clearing stability observed ${range(candidate.settled_house_observed.first.source_seconds, candidate.settled_house_observed.last.source_seconds)}. First delivery, final stone stopping and score agreement remain unverified.</p>
        ${link(candidate.settled_house_observed.last, "Inspect pre-clearing house")}${link(candidate.clearing.after, "Inspect after")}
        ${candidate.board_comparisons.map((c) => `<p>End ${c.official_number} association: ${label(c.association)}. Vision ${score(c.vision_points)} / board ${score(c.board_points)} · ${label(c.comparison)}. Posting lag remains unknown.</p>`).join("") || "<p>No unique later board update can be associated yet.</p>"}</article>`).join("") : "<p>No clearing passed the detailed continuity checks. More end-transition footage is needed.</p>"}`;
  }
  function renderScores(result) {
    const observations = [...result.observations].sort((a, b) => a.source_seconds - b.source_seconds);
    $("#house-score-content").innerHTML = `<h3>Experimental house scores · ${observations.length} frames</h3><p>These are image-space proposals. Detection completeness, physical calibration and final-end phase need review. A mid-end house score is not the played result.</p>${observations.length ? observations.map((observation) => `<details><summary>${stamp(observation.source_seconds)} · ${Object.entries(observation.views).map(([view, data]) => `${view === "house_top" ? "Top" : "Bottom"}: ${score(data.scoring.provisional_score)}`).join(" · ")}</summary>${Object.entries(observation.views).map(([view, data]) => {
      const asset = observation.input_crops[view];
      const circles = data.footprints.filter((f) => f.status === "candidate").map((f) => `<circle cx="${Number(f.center_px[0])}" cy="${Number(f.center_px[1])}" r="${Number(f.radius_px)}" fill="none" stroke="${f.color === "red" ? "#ff5555" : "#ffee55"}" stroke-width="2"/>`).join("");
      return `<figure><figcaption>${label(view)} · candidate score ${score(data.scoring.provisional_score)}${data.scoring.reasons.length ? " · " + data.scoring.reasons.map(label).join(", ") : " · proposed"}</figcaption><div class="house-score-image"><img loading="lazy" src="/api/evidence/${encodeURIComponent(observation.evidence_id)}/${view}" width="${asset.width}" height="${asset.height}" alt="${label(view)} with experimental granite boundaries"><svg viewBox="0 0 ${asset.width} ${asset.height}" aria-hidden="true">${circles}</svg></div></figure>`;
    }).join("")}${link(observation, "Check original frame")} <a class="evidence-link" target="_blank" rel="noopener" href="/api/house-scores/${encodeURIComponent(observation.id)}">Inspect measurements JSON ↗</a></details>`).join("") : "<p>Choose Analyze cached houses to inspect retained frames.</p>"}`;
  }
  async function load() {
    const source = selectedSource, current = ++generation;
    const [timeline, scores] = await Promise.all([
      api(`/sources/${encodeURIComponent(source)}/end-timeline`),
      api(`/sources/${encodeURIComponent(source)}/house-scores`),
    ]);
    if (current !== generation || source !== selectedSource) return;
    renderTimeline(timeline); renderScores(scores);
  }
  $("#end-timeline-button").addEventListener("click", async () => {
    if (!state.source) return;
    selectedSource = state.source;
    $("#end-timeline-content").textContent = "Reconstructing cached end evidence…";
    $("#house-score-content").textContent = "";
    $("#end-analysis-status").textContent = "";
    $("#end-timeline-dialog").showModal();
    try { await load(); } catch (error) { $("#end-timeline-content").textContent = error.message; }
  });
  $("#house-score-analyze").addEventListener("click", async (event) => {
    const button = event.currentTarget, source = selectedSource;
    button.disabled = true;
    $("#end-analysis-status").textContent = "Inspecting up to 24 cached frames under a 30-second budget…";
    try {
      const result = await api(`/sources/${encodeURIComponent(source)}/house-scores`, "POST", {limit: 24, max_seconds: 30});
      if (source === selectedSource) {
        $("#end-analysis-status").textContent = `${result.processed_frames} new frames · ${result.remaining_frames} pending · ${result.skipped_frames.length} skipped for missing provenance. Run again to continue.`;
        await load();
      }
    } catch (error) { $("#end-analysis-status").textContent = error.message; }
    finally { button.disabled = false; }
  });
})();
