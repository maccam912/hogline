(() => {
  const esc = escapeHTML;
  const host = $("#viewer-content"),
    old = [...host.children];
  const divider = old.findIndex((el) => el.classList.contains("scan-bar"));
  const video = document.createElement("section");
  video.id = "recording-video";
  video.hidden = true;
  const tools = document.createElement("section");
  tools.id = "recording-tools";
  tools.hidden = true;
  old.slice(0, divider).forEach((el) => video.appendChild(el));
  old.slice(divider).forEach((el) => tools.appendChild(el));
  host.innerHTML = `<section class="analysis-hero">
    <div><h3>Recording workspace</h3><p class="fine-print">Analyze, review decisions, then compare players.</p></div>
    <div class="analysis-start"><button class="button primary" id="analyze-recording">Analyze recording <span aria-hidden="true">↗</span></button><button class="button quiet" id="recording-limits-button">Analysis settings</button></div>
  </section><div id="recording-progress"></div><section id="recording-stage-status" aria-live="polite"></section>
  <div class="recording-tabs" role="tablist" aria-label="Recording workspace"><button role="tab" aria-selected="true" data-recording-tab="results">Results</button><button role="tab" aria-selected="false" data-recording-tab="video">Video &amp; evidence</button><button role="tab" aria-selected="false" data-recording-tab="tools">More tools</button></div>
  <section id="recording-results" role="tabpanel"><div class="results-heading"><div><h3>Games, ends &amp; scores</h3></div><button class="button quiet" id="refresh-recording-results">Refresh results</button></div><div id="recording-results-content"></div></section>`;
  host.append(video, tools);
  const sidebarJobs = document.createElement("button");
  sidebarJobs.id = "jobs-nav";
  sidebarJobs.className = "nav-item";
  sidebarJobs.innerHTML = `◷ <span>Jobs</span><b id="jobs-nav-count">0</b>`;
  $("#archive-nav").after(sidebarJobs);
  const archiveTools = document.createElement("details");
  archiveTools.className = "archive-tools";
  archiveTools.innerHTML = `<summary>Archive tools</summary><div class="archive-tools-menu"></div>`;
  [
    "moments-button",
    "evaluation-button",
    "board-evaluation-button",
    "house-benchmark-button",
    "discovery-button",
    "score-disagreements-button",
    "players-button",
    "export-button",
  ].forEach((id) => {
    const button = $("#" + id);
    if (button) $(".archive-tools-menu", archiveTools).appendChild(button);
  });
  $(".heading-actions").prepend(archiveTools);
  const banner = document.createElement("section");
  banner.id = "processing-banner";
  banner.setAttribute("aria-label", "Processing status");
  $(".page-heading").after(banner);
  const dialogs = document.createElement("div");
  dialogs.innerHTML = `<dialog id="jobs-dialog" class="workspace-dialog"><div class="workspace-dialog-header"><div><h2>Jobs &amp; progress</h2><p id="jobs-worker-status"></p></div><button class="close-dialog" aria-label="Close jobs">×</button></div><div id="jobs-content"></div></dialog>
    <dialog id="recording-limits-dialog"><button class="close-dialog" aria-label="Close analysis settings">×</button><h2>Analysis settings</h2><p>Applies to the next analysis. Change existing limits in Jobs.</p><form id="recording-limits-form"><label>Processing time limit (minutes)<input name="minutes" type="number" min="1" max="1440" placeholder="Automatic" /></label><label>Additional retained data (MB)<input name="mb" type="number" min="32" max="4096" placeholder="Automatic" /></label><button class="button primary" type="submit">Save for next analysis</button></form></dialog>
    <dialog id="analysis-resume-dialog"><button class="close-dialog" aria-label="Close resume settings">×</button><h2>Resume analysis</h2><p>Completed work is kept. Failed tasks are retried within the three-attempt limit.</p><form id="analysis-resume-form"><label>Total processing limit (minutes)<input name="minutes" required type="number" min="1" max="1440" /></label><label>Total additional data limit (MB)<input name="mb" required type="number" min="1" max="4096" /></label><button class="button primary" type="submit">Retry failed tasks &amp; resume</button></form></dialog>`;
  [...dialogs.children].forEach((el) => document.body.appendChild(el));

  let limits = {},
    selectedTab = "results",
    sourceKey = null,
    resultKey = null,
    resultGeneration = 0;
  const busy = new Set(),
    labels = {
      active: "Running",
      paused: "Paused",
      failed: "Needs attention",
      completed: "Processing finished",
      cancelled: "Cancelled",
    };
  const batches = () => state.data.batches || [];
  let batchInput = null, batchOrder = [], batchesBySource = new Map();
  const sortedBatches = () => {
    if (batchInput === batches()) return batchOrder;
    batchInput = batches();
    const running = new Set(state.data.jobs.filter((j) => j.state === "running").map((j) => j.batch_id));
    const rank = (b) => b.state === "active" ? running.has(b.id) ? -3 : -1 : b.state === "paused" ? -2 : 0;
    batchOrder = [...batchInput].sort(
      (a, b) =>
        rank(a) - rank(b) ||
        (b.created_at || "").localeCompare(a.created_at || ""),
    );
    batchesBySource = new Map();
    for (const batch of batchOrder) for (const sid of batch.request.source_ids) {
      if (!batchesBySource.has(sid)) batchesBySource.set(sid, []);
      batchesBySource.get(sid).push(batch);
    }
    return batchOrder;
  };
  const related = (sid) => {
    sortedBatches();
    return batchesBySource.get(sid) || [];
  };
  const latest = (sid) => {
    const list = related(sid);
    const whole = [...list]
      .filter((b) => b.campaign_purpose === "recording_analysis")
      .sort((a, b) =>
        (b.created_at || "").localeCompare(a.created_at || ""),
      )[0];
    const live = list.find((b) => ["active", "paused"].includes(b.state));
    return whole &&
      (!live || (whole.created_at || "") >= (live.created_at || ""))
      ? whole
      : live || whole;
  };
  const duration = (seconds) => {
    seconds = Math.max(0, Math.floor(seconds || 0));
    return seconds >= 3600
      ? `${Math.floor(seconds / 3600)}h ${Math.floor(seconds / 60) % 60}m ${seconds % 60}s`
      : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  };
  const clock = (seconds, updated, updates = null) =>
    `<span data-elapsed="${Number(seconds) || 0}" data-live-updates="${esc(JSON.stringify(state.data.worker?.running === false ? [] : updates || [updated]))}" data-live-updated="${esc(state.data.worker?.running === false ? "" : updated || "")}">${duration(seconds)}</span>`;
  const percent = (value) =>
    `${Math.round(100 * Math.max(0, Math.min(1, value || 0)))}%`;
  function tick() {
    $$("[data-elapsed]").forEach((el) => {
      const updates = JSON.parse(el.dataset.liveUpdates || "[]"),
        extra = updates.reduce((sum, value) => {
          const start = Date.parse(value);
          return sum + (Number.isFinite(start) ? Math.max(0, (Date.now() - start) / 1000) : 0);
        }, 0);
      el.textContent = duration(Number(el.dataset.elapsed) + extra);
    });
  }
  function showTab(tab) {
    selectedTab = tab;
    document.body.dataset.recordingTab = tab;
    ["results", "video", "tools"].forEach((name) => {
      const panel = $("#recording-" + name),
        button = $(`button[data-recording-tab="${name}"]`);
      panel.hidden = name !== tab;
      panel.setAttribute("role", "tabpanel");
      button.id = `recording-tab-${name}`;
      button.setAttribute("aria-selected", String(name === tab));
      button.setAttribute("aria-controls", panel.id);
      button.tabIndex = name === tab ? 0 : -1;
      panel.setAttribute("aria-labelledby", button.id);
    });
  }
  function workerStatus() {
    const worker = state.data.worker;
    if (!worker) return "Checking local worker…";
    if (worker.running)
      return `${worker.alive_workers || 1} of ${worker.configured_workers || 1} workers online. Keep the server running to process jobs.`;
    return worker.error
      ? "Worker stopped: " + worker.error
      : "Local worker is offline. Start the server to process queued work.";
  }
  function jobLabel(job) {
    return (
      {
        dense_window: "Inspecting a detailed window",
        broadcast_ocr: "Reading a scoreboard",
        scorecard_read: "Reading end plates and checking ice evidence",
        recording_reconstruction: "Assembling results",
        metadata_refresh: "Refreshing recording metadata",
      }[job.kind] || "Surveying the recording"
    );
  }
  function batchTitle(batch) {
    const names = batch.request.source_ids.map(
      (id) => state.data.sources.find((s) => s.id === id)?.title || "Recording",
    );
    return names.length === 1 ? names[0] : `${names.length} recordings`;
  }
  function progressCard(batch, compact = false) {
    if (batch.summary_only) {
      const running = state.data.jobs.some((j) => j.batch_id === batch.id && j.state === "running");
      const status = batch.state === "active" ? running ? "Running" : "Queued" : labels[batch.state];
      return `<article class="analysis-progress-card ${esc(batch.state)}" data-analysis-card="${esc(batch.id)}"><div class="analysis-card-heading"><h4>${esc(batchTitle(batch))}</h4><span class="analysis-state ${esc(batch.state)}">${esc(status)}</span></div>${batch.pause_reason ? `<p class="job-error">${esc(batch.pause_reason)}</p>` : ""}<p>${duration(batch.spent_worker_seconds)} processing</p><button class="button quiet" data-source="${esc(batch.request.source_ids[0])}">Open recording &amp; task details →</button></article>`;
    }
    const p = batch.progress_summary || {},
      whole = batch.campaign_purpose === "recording_analysis";
    const running = batch.units
      .map((u) => state.data.jobs.find((j) => j.id === u.job_id))
      .filter((j) => j?.state === "running");
    const retrying = batch.state === "active" ? batch.units
      .map((u) => state.data.jobs.find((j) => j.id === u.job_id))
      .filter((j) => j?.state === "queued" && j.retry_pending) : [];
    const fraction = p.overall_fraction ?? p.stage_fraction ?? 0;
    const status =
      batch.state === "active"
        ? state.data.worker?.running === false
          ? "Local worker offline"
          : running.length
            ? "Running"
            : retrying.length ? "Retrying automatically" : "Queued"
        : labels[batch.state];
    const failed = batch.units
      .map((u) => state.data.jobs.find((j) => j.id === u.job_id))
      .filter((j) => j?.state === "failed");
    const failure = failed[0]?.error;
    const controls = `<div class="analysis-controls">${batch.state === "active" ? `<button class="button secondary" data-analysis-control="pause" data-analysis="${esc(batch.id)}">Pause</button>` : ""}${batch.state === "paused" ? `<button class="button primary" data-analysis-control="resume" data-analysis="${esc(batch.id)}">${failed.length ? "Retry & resume" : "Resume analysis"}</button><button class="button quiet" data-analysis-limits="${esc(batch.id)}">Adjust limits</button>` : ""}${["active", "paused"].includes(batch.state) ? `<button class="button quiet" data-analysis-control="cancel" data-analysis="${esc(batch.id)}">Cancel remaining</button>` : ""}${compact ? `<button class="button quiet" data-open-jobs>View all jobs →</button>` : ""}</div>`;
    return `<article class="analysis-progress-card ${batch.state}" data-analysis-card="${esc(batch.id)}"><div class="analysis-card-heading"><div><span class="eyebrow">${whole ? "RECORDING ANALYSIS" + (compact ? ` · STEP ${p.stage_index} OF ${p.stage_count}` : "") : "EARLIER ANALYSIS"}</span><h4>${compact ? esc(p.phase_label || "Recording analysis") : esc(batchTitle(batch))}</h4></div><span class="analysis-state ${batch.state}"><i></i>${esc(status)}</span></div>${!compact ? `<p>${esc(p.phase_label || batch.phase || "Processing")}${whole ? ` · stage ${p.stage_index} of ${p.stage_count}` : ""}</p>` : ""}<div class="analysis-progress-line"><progress max="1" value="${fraction}" aria-label="${whole ? "Overall analysis progress" : "Current stage progress"}"></progress><strong>${percent(fraction)}</strong></div>${batch.progress_refreshing ? '<p class="fine-print" role="status">Showing last reported progress while refreshing…</p>' : ""}<div class="analysis-metrics"><span><small>ELAPSED PROCESSING</small><strong>${clock(p.processing_elapsed_seconds ?? batch.spent_worker_seconds, p.live_updated_at, p.live_worker_updates)}</strong><span class="budget-hint">of ${duration(batch.limits.max_worker_seconds)} limit</span></span><span><small>THIS STAGE</small><strong>${p.completed_tasks ?? batch.succeeded_units} / ${p.total_tasks ?? batch.units.length} tasks</strong></span><span><small>RETAINED DATA</small><strong>${(batch.reserved_image_bytes / 1024 ** 2).toFixed(1)} / ${batch.limits.max_image_growth_mb} MB</strong></span></div>${running.map((job) => `<p class="running-task"><span class="status-dot"></span>${esc(jobLabel(job))} · ${time(job.request.start_seconds)}–${time(job.request.end_seconds)} · ${job.acquisition === "downloading_temporary_section" ? "Downloading video section" : `${job.completed_frames || 0} frames · ${percent(job.progress)}`}</p>`).join("")}${batch.pause_reason ? `<div class="analysis-problem"><strong>${batch.state === "failed" ? "Analysis needs attention. Other recordings will continue." : failed.length ? "A task failed. Analysis is paused." : "Analysis is paused."}</strong><p>${esc(failure || batch.pause_reason)}</p><span>${batch.state === "failed" ? "Saved observations and error details are kept. Start a new analysis to try this recording again." : `${p.queued_jobs || 0} queued tasks will wait until this analysis resumes.`}</span></div>` : ""}${batch.state === "completed" ? `<p class="fine-print">${whole ? "Review proposed results in the Results tab." : "This earlier run covered its selected range; it did not analyze the full recording."}</p>` : ""}${controls}${
      !compact
        ? `<details class="analysis-task-list"><summary>Tasks and attempts · ${batch.units.length}</summary>${batch.units
            .map((u) => {
              const j = state.data.jobs.find((j) => j.id === u.job_id),
                execution = j?.execution;
              return `<div class="analysis-task"><div><strong>${esc(j ? jobLabel(j) : u.kind)}</strong><small>${esc(state.data.sources.find((s) => s.id === u.request.source_id)?.title || "Recording")} · ${time(u.request.start_seconds)}–${time(u.request.end_seconds)}</small>${j?.error ? `<p class="job-error">${esc(j.error)}</p>` : ""}</div><span>${esc(u.state)}${u.disposition === "reuse_succeeded" ? " · reused" : ""}<br>${clock(execution?.wall_seconds || 0, execution?.state === "running" ? execution.updated_at || execution.started_at : null)} · ${j?.attempts || 0} attempts</span></div>`;
            })
            .join("")}</details>`
        : ""
    }</article>`;
  }
  function individualJob(job) {
    const e = job.execution;
    return `<article class="analysis-progress-card"><div class="analysis-card-heading"><div><span class="eyebrow">INDIVIDUAL TASK</span><h4>${esc(jobLabel(job))}</h4></div><span class="analysis-state ${esc(job.state)}">${esc(job.state)}</span></div><p>${esc(state.data.sources.find((s) => s.id === job.request.source_id)?.title || "Recording")} · ${time(job.request.start_seconds)}–${time(job.request.end_seconds)}</p><div class="analysis-progress-line"><progress max="1" value="${job.state === "succeeded" ? 1 : job.progress || 0}" aria-label="Task progress"></progress><strong>${percent(job.progress)}</strong></div><div class="analysis-metrics"><span><small>ELAPSED PROCESSING</small><strong>${clock(e?.wall_seconds || job.wall_seconds || 0, e?.state === "running" ? e.updated_at || e.started_at : null)}</strong></span><span><small>FRAMES PROCESSED</small><strong>${job.completed_frames || 0}</strong></span><span><small>ATTEMPTS</small><strong>${job.attempts || 0}</strong></span></div>${job.error ? `<p class="job-error">${esc(job.error)}</p>` : ""}</article>`;
  }
  function renderJobs() {
    const active = sortedBatches().filter((b) =>
        ["active", "paused"].includes(b.state),
      ),
      history = sortedBatches().filter(
        (b) => !["active", "paused"].includes(b.state),
      );
    const expanded = new Set(
      $$("#jobs-content details[open]").map(
        (d) =>
          d.dataset.history ||
          d.closest("[data-analysis-card]")?.dataset.analysisCard,
      ),
    );
    $("#jobs-worker-status").textContent = workerStatus();
    const standalone = state.data.jobs.filter(
      (j) => ["running", "queued"].includes(j.state) && !j.batch_id,
    );
    $("#jobs-content").innerHTML =
      `${active.length ? active.map((b) => progressCard(b)).join("") : `<div class="results-empty"><h3>No active analyses</h3></div>`}${standalone.length ? `<h3>Other queued and running tasks</h3>${standalone.map(individualJob).join("")}` : ""}<details data-history="history" class="analysis-history"><summary>Previous analyses · ${history.length}</summary>${expanded.has("history") ? history.map((b) => progressCard(b)).join("") : ""}</details>`;
    $$("#jobs-content details").forEach((d) => {
      if (
        expanded.has(
          d.dataset.history ||
            d.closest("[data-analysis-card]")?.dataset.analysisCard,
        )
      )
        d.open = true;
    });
    tick();
  }
  $("#jobs-content").addEventListener("toggle", (event) => {
    if (event.target.matches("details[data-history]") && event.target.open && event.target.children.length === 1) renderJobs();
  }, true);
  const score = (points) => `${points?.red ?? "?"}–${points?.yellow ?? "?"}`;
  const scoreRange = (bounds, points) => bounds ? ["red", "yellow"].map((color) => {
    const value = bounds[color];
    return value.min === value.max ? String(value.min) : `${value.min}–${value.max}`;
  }).join(" : ") : score(points);
  let resultReviews = [], scoreBooks = [];
  function renderResultReview(report) {
    const review = report.result_review;
    resultReviews = review?.histories || [];
    if (!review) return "";
    const label = {
      complete_results: "Complete proposed results",
      scores_extracted: "Scores extracted · game review needed",
      partial_scores: "Partial score evidence",
      no_scores: "No scores recovered",
    }[review.status];
    const notes = review.reviews.map((item) => `<details><summary>End ${item.official_end_number ?? "?"} · ${esc(item.reviewer_kind === "assistant_visual" ? "Assistant visual review" : "Human review")}</summary><p>${esc(item.note)}</p>${item.evidence.map((frame) => `<a class="evidence-link" target="_blank" rel="noopener" href="/api/evidence/${encodeURIComponent(frame.evidence_id)}/composite">Frame · ${time(frame.source_seconds)} ↗</a>`).join("")}</details>`).join("");
    return `<section class="recording-game"><h4>${esc(label)}</h4><p>${review.known_end_scores} proposed end scores · ${review.proposed_complete_games} complete proposed ${review.proposed_complete_games === 1 ? "game" : "games"}</p>${review.status === "no_scores" ? "<p>Processing can finish without recovering scores. Review board visibility and the final played ends before running more analysis.</p>" : ""}${notes}</section>`;
  }
  function renderBlockers(book) {
    const review = resultReviews.find((item) => item.history_id === book.id);
    if (!review?.blockers.length) return "";
    return `<details open><summary>What is still needed</summary>${review.blockers.map((blocker) => `<p>${esc(blocker.message)}${blocker.end_numbers?.length ? ` Ends: ${blocker.end_numbers.join(", ")}.` : ""}</p>`).join("")}${review.last_score_evidence?.youtube_url ? `<a class="evidence-link" target="_blank" rel="noopener" href="${esc(review.last_score_evidence.youtube_url)}">Review from the last posted score ↗</a>` : ""}</details>`;
  }
  function renderScoreBook(book, index) {
    const complete = book.game_completion === "complete";
    let scoredThrough = null;
    for (const end of book.ends) {
      if (end.points?.red == null || end.points?.yellow == null || end.score_after_end?.red == null || end.score_after_end?.yellow == null) break;
      scoredThrough = end;
    }
    const rows = book.ends.map((end) => {
      const ice = state.data.evidence.find((f) => f.id === end.ice_evidence_ids?.[0]);
      const board = end.board_posting?.first_observation;
      const links = [
        board?.youtube_url ? `<a class="evidence-link" target="_blank" rel="noopener" href="${esc(board.youtube_url)}">Board ↗</a>` : "",
        ice?.youtube_url ? `<a class="evidence-link" target="_blank" rel="noopener" href="${esc(ice.youtube_url)}">Ice ↗</a>` : "",
      ].filter(Boolean).join(" · ");
      const awardedOnConcession = end.concession_decisions?.some((decision) => decision.status === "scored");
      const basis = end.comparison === "recording_cutoff_blank" ? "Blank · recording cutoff" : end.comparison === "lower_bound_estimate" ? "Lower-count estimate" : end.comparison === "scoreboard_corrected_estimate" ? "Board corrected earlier estimate" : end.comparison === "scoreboard_confirms_estimate" ? "Board confirms earlier estimate" : end.comparison === "scoreboard_corrected_ice" ? "Board corrected ice estimate" : end.comparison === "disagreement" ? "Board / ice disagree" : end.comparison === "concession_not_scored" ? "Conceded · end not scored" : awardedOnConcession ? "Points awarded on concession" : end.lying_ice_points && !end.end_completion_verified ? `Lying ${score(end.lying_ice_points)} · end completion unresolved` : end.comparison === "agreement" ? "Board + ice agree" : end.comparison === "ice_only" ? "Ice estimate · unconfirmed by board" : end.comparison === "scoreboard_only" ? "Scoreboard" : "Unresolved";
      return `<tr><td>${end.official_number ?? end.observed_index ?? "?"}</td><td>${end.state === "not_scored_concession" ? "X–X" : scoreRange(end.points_range, end.points)}</td><td>${scoreRange(end.score_after_end_range, end.score_after_end)}</td><td>${esc(basis)}${end.score_options?.length > 1 ? `<br><strong>Uncertain: ${end.score_options.map(score).join(" or ")}</strong>` : ""}<br>${links || "No linked frame"}<br><button class="button secondary" data-review-book="${index}" data-review-end="${book.ends.indexOf(end)}">Review / adjust score</button></td></tr>`;
    }).join("");
    return `<article class="recording-game"><div class="recording-game-heading"><div><span class="eyebrow">CANDIDATE GAME ${index + 1}</span><h4>Evidence · ${time(book.first_seconds)}–${time(book.last_seconds)}</h4></div><span class="pill amber">Proposed</span></div><div class="game-summary-strip"><span>Scores through end <strong>${scoredThrough?.official_number ?? scoredThrough?.observed_index ?? "?"}</strong></span><span>Score after that end <strong>${scoreRange(scoredThrough?.score_after_end_range, scoredThrough?.score_after_end)}</strong></span><span>Final result <strong>${complete ? `${scoreRange(book.final_result_range, book.final_result)} · ${book.result_is_estimate ? "estimated" : "proposed"}` : "Unresolved"}</strong></span></div>${book.uncertain_end_numbers?.length ? `<p><strong>Uncertain ends: ${book.uncertain_end_numbers.join(", ")}.</strong> Ranges carry through to the total. A later posted score replaces the ice estimate.</p>` : ""}${renderBlockers(book)}<div class="end-table-wrap"><table class="end-table"><thead><tr><th>End</th><th>Points · red–yellow</th><th>Score after end</th><th>Basis and evidence</th></tr></thead><tbody>${rows}</tbody></table></div></article>`;
  }
  async function loadResults(force = false) {
    const sid = state.source;
    if (!sid) return;
    const done = related(sid)
      .filter((b) => b.state === "completed")
      .map((b) => b.id)
      .join(":");
    const currentAnalysis = latest(sid);
    const key =
      sid +
      ":" +
      done +
      ":" +
      (currentAnalysis
        ? `${currentAnalysis.id}:${currentAnalysis.phase}:${currentAnalysis.state}`
        : "idle");
    try {
      const stages = await api(`/recordings/${encodeURIComponent(sid)}/status`);
      if (sid === state.source) {
        const reasons = Object.entries(stages.scores.blockers).map(([reason, count]) => `${reason.replaceAll('_', ' ')} (${count} frames)`).join('; ');
        $('#recording-stage-status').innerHTML = `<article class="recording-game"><h3>Analysis stages</h3>
          <p><strong>Scores:</strong> ${esc((stages.scores.status || stages.scores.state).replaceAll("_", " "))} · ${stages.scores.known_end_scores || 0} end scores${reasons ? `<br><span class="fine-print">${esc(reasons)}</span>` : ''}${stages.scores.problem ? `<br>${esc(stages.scores.problem)}` : ''}</p>
          <p><strong>Faces:</strong> ${esc(stages.faces.state)} · ${stages.faces.lineups} lineups${stages.faces.blocker ? `<br>${esc(stages.faces.blocker)}` : ''}</p>
          <p><strong>Skills:</strong> ${esc((stages.skills_followup?.state || 'waiting').replaceAll('_', ' '))} · ${esc(stages.skills_followup?.reason || (stages.skills.length ? 'Delivery survey and local motion tracking' : 'Waiting for recording analysis'))}</p>
          ${stages.skills.map(g => `<p><a href="/static/skills.html?game=${encodeURIComponent(g.game_id)}">${esc(g.label)} — open skills report & review</a><br>${g.delivery_proposals} delivery proposals · ${g.motion_clips} tracked clips${g.coverage_gaps?` · ${g.coverage_gaps} coverage gaps`:''} · ${esc(Object.entries(g.campaigns).map(([k,v]) => `${v} ${k}`).join(', '))}${g.blockers.length ? `<br>${esc(g.blockers.join('; '))}` : ''}</p>`).join('')}
          <p class="fine-print">${esc(stages.review_required)}</p></article>`;
      }
    } catch (error) { if(sid === state.source) $('#recording-stage-status').textContent = error.message; }
    if (key === resultKey && !force) return;
    resultKey = key;
    const current = ++resultGeneration;
    scoreBooks = [];
    $("#recording-results-content").innerHTML =
      '<p class="muted">Loading game and score evidence…</p>';
    try {
      const report = await api(
        `/sources/${encodeURIComponent(sid)}/end-timeline`,
      );
      if (current !== resultGeneration || sid !== state.source) return;
      const temporal = report.scorecard_reconstruction?.scorecards || [];
      const combined = report.reconciled_score_history?.histories || [];
      const candidates = combined.length ? combined : temporal;
      const ledgers = candidates.length ? candidates.map((book) => ({
        ...book,
        highest_observed_end_number: book.ends.at(-1)?.official_number ?? book.highest_posted_end,
        ends: book.ends.map((end) => ({...end, board_posting: end.board_evidence ? {
          first_observation: end.board_evidence.first,
        } : null})),
      })) : report.board_ledgers.filter((b) => b.ends.length);
      scoreBooks = ledgers;
      const partial = [
        ...new Map(
          (report.partial_score_proposals || []).map((p) => [
            [
              p.source_metadata_sha256,
              p.model_version,
              p.official_number,
              p.color,
              p.points,
              p.previous_scoring_end,
            ].join(":"),
            p,
          ]),
        ).values(),
      ].sort(
        (a, b) =>
          a.official_number - b.official_number ||
          a.color.localeCompare(b.color),
      );
      const partialCard = partial.length
        ? `<article class="recording-game"><div class="recording-game-heading"><div><span class="eyebrow">PARTIAL SCORE EVIDENCE</span><h4>Scoring rows found · game boundaries unresolved</h4></div><span class="pill amber">Needs review</span></div><div class="end-table-wrap"><table class="end-table"><thead><tr><th>Posted end</th><th>Scoring row</th><th>Proposed points</th><th>Evidence</th></tr></thead><tbody>${partial.map((p) => `<tr><td>${p.official_number}</td><td>${esc(p.color)}</td><td>+${p.points}</td><td>${p.youtube_links?.[0] ? `<a class="evidence-link" target="_blank" rel="noopener" href="${esc(p.youtube_links[0])}">Check board ↗</a>` : "Recorded frame"}</td></tr>`).join("")}</tbody></table></div><p class="fine-print">Game assignment is unverified. Opponent scores and initial totals are unknown.</p></article>`
        : "";
      const confirmed = sourceGames().filter(
        (g) => g.acceptance === "human_confirmed",
      );
      $("#recording-results-content").innerHTML =
        `${renderResultReview(report)}${confirmed.map((g) => `<article class="recording-game"><div class="recording-game-heading"><h4>${esc(g.label)}</h4><span class="pill green">Reviewed</span></div><p>${g.ends.length} saved ends · ${esc(g.completion)}</p><button class="button quiet" data-edit="${esc(g.id)}">Review game →</button></article>`).join("")}${ledgers.length ? ledgers.map(renderScoreBook).join("") : partialCard ? partialCard : !confirmed.length ? `<div class="results-empty"><div class="empty-house-mark">◎</div><h3>${latest(sid)?.state === "active" ? "Analysis in progress" : latest(sid)?.state === "completed" ? "Analysis finished · scorecard unresolved" : "No scorecard available"}</h3>${latest(sid)?.state === "active" ? "" : `<p>${latest(sid)?.state === "paused" ? "Resume analysis in Jobs." : latest(sid)?.state === "completed" ? "Inspect saved frames or run additional analysis." : "Select Analyze recording to begin."}</p>`}</div>` : ""}<button class="button quiet" data-recording-open-evidence>Inspect detailed end evidence →</button>`;
    } catch (error) {
      if (current === resultGeneration) {
        resultKey = null;
        $("#recording-results-content").textContent = error.message;
      }
    }
  }
  function render() {
    const active = sortedBatches().filter((b) => b.state === "active"),
      paused = sortedBatches().filter((b) => b.state === "paused");
    const running = state.data.jobs.filter((j) => j.state === "running").length;
    $("#jobs-nav-count").textContent = active.length + paused.length;
    $("#processing-banner").className =
      paused.length && !active.length ? "attention" : "";
    $("#processing-banner").innerHTML =
      `<div><span class="status-dot ${paused.length && !active.length ? "paused" : ""}"></span><strong>${state.data.worker?.connection_lost ? "Server unavailable" : running && state.data.worker?.running === false ? "Local worker offline" : running ? `${running} task${running === 1 ? "" : "s"} running` : active.length ? "Analysis queued" : paused.length ? `${paused.length} ${paused.every((b) => b.campaign_purpose !== "recording_analysis") ? "earlier run" : "analysis"} paused` : "Nothing running"}</strong>${state.data.worker?.connection_lost ? "<span>Restart the local server to reconnect.</span>" : paused.length ? "<span>Review paused jobs.</span>" : ""}</div><button class="button quiet" data-open-jobs>Jobs &amp; progress <span aria-hidden="true">→</span></button>`;
    $$(".source-card[data-source]").forEach((card) => {
      const b = latest(card.dataset.source),
        el = $(".source-count", card);
      if (el)
        el.innerHTML = `<span class="source-status ${b?.state || ""}">${b ? (b.state === "active" ? state.data.jobs.some((j) => j.batch_id === b.id && j.state === "running") ? "Running" : "Queued" : b.campaign_purpose === "recording_analysis" || b.state === "paused" ? labels[b.state] : "Evidence available") : "Ready to analyze"}</span><span>→</span>`;
    });
    if (state.source) {
      if (state.source !== sourceKey) {
        sourceKey = state.source;
        showTab("results");
        localStorage.setItem("hogline-source", state.source);
      }
      const b = latest(state.source),
        full = related(state.source).find(
          (b) =>
            b.campaign_purpose === "recording_analysis" &&
            ["active", "paused"].includes(b.state),
        );
      $("#analyze-recording").disabled = busy.has("start");
      $("#analyze-recording").textContent = full
        ? "View analysis progress →"
        : b?.campaign_purpose === "recording_analysis" &&
            b.state === "completed"
          ? "Analyze again (reuse saved work) ↗"
          : "Analyze recording ↗";
      $("#recording-progress").innerHTML = b
        ? b.state === "completed"
          ? `<details class="completed-analysis"><summary>Processing finished · ${duration(b.spent_worker_seconds)} processing · view details</summary>${progressCard(b, true)}</details>`
          : progressCard(b, true)
        : "";
      loadResults();
    }
    if ($("#jobs-dialog").open) renderJobs();
    tick();
  }
  async function control(id, action, extra = {}) {
    if (busy.has(id)) return false;
    busy.add(id);
    try {
      await api(`/batches/${encodeURIComponent(id)}/control`, "POST", {
        action,
        retry_failed: action === "resume",
        ...extra,
      });
      await refresh();
      render();
      return true;
    } catch (error) {
      toast(error.message, true);
      return false;
    } finally {
      busy.delete(id);
    }
  }
  function openJobs() {
    renderJobs();
    if (!$("#jobs-dialog").open) $("#jobs-dialog").showModal();
  }
  document.addEventListener("keydown", (event) => {
    const tab = event.target.closest("button[data-recording-tab]");
    if (!tab || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
      return;
    const names = ["results", "video", "tools"],
      index = names.indexOf(tab.dataset.recordingTab);
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? 2
          : (index + (event.key === "ArrowRight" ? 1 : 2)) % 3;
    event.preventDefault();
    showTab(names[next]);
    $(`button[data-recording-tab="${names[next]}"]`).focus();
  });
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.hasAttribute("data-review-book")) {
      const book=scoreBooks[Number(button.dataset.reviewBook)];
      return window.hoglineWorkspace.reviewProposal(book,book.ends[Number(button.dataset.reviewEnd)]);
    }
    if (button.dataset.recordingTab)
      return showTab(button.dataset.recordingTab);
    if (button.id === "jobs-nav" || button.hasAttribute("data-open-jobs"))
      return openJobs();
    if (button.id === "refresh-recording-results") return loadResults(true);
    if (button.hasAttribute("data-recording-open-evidence"))
      return $("#end-timeline-button").click();
    if (
      button.dataset.edit ||
      button.dataset.activity ||
      button.dataset.scoreProposal
    )
      showTab("tools");
    if (button.dataset.windowEvidence || button.dataset.windowView)
      showTab("video");
    if (button.id === "recording-limits-button")
      return $("#recording-limits-dialog").showModal();
    if (button.dataset.analysisLimits) {
      const b = batches().find((b) => b.id === button.dataset.analysisLimits),
        form = $("#analysis-resume-form");
      form.dataset.analysisId = b.id;
      form.elements.minutes.value = Math.ceil(b.limits.max_worker_seconds / 60);
      form.elements.mb.value = b.limits.max_image_growth_mb;
      return $("#analysis-resume-dialog").showModal();
    }
    if (button.dataset.analysisControl)
      return control(button.dataset.analysis, button.dataset.analysisControl);
    if (button.id !== "analyze-recording" || busy.has("start")) return;
    if (
      related(state.source).some(
        (b) =>
          b.campaign_purpose === "recording_analysis" &&
          ["active", "paused"].includes(b.state),
      )
    )
      return openJobs();
    busy.add("start");
    button.disabled = true;
    const startingSource = state.data.sources.find(s => s.id === state.source);
    if (startingSource?.live_status === "post_live" && !startingSource.verified_download) {
      toast("Downloading and checking the ended stream before analysis. This may take several minutes.");
    }
    try {
      const result = await api(
        `/recordings/${encodeURIComponent(state.source)}/analyze`,
        "POST",
        limits,
      );
      toast(
        result.already_exists
          ? "This recording already has an analysis."
          : "Recording analysis queued.",
      );
      await refresh();
      render();
    } catch (error) {
      toast(error.message, true);
    } finally {
      busy.delete("start");
      button.disabled = false;
    }
  });
  $("#recording-limits-form").addEventListener("submit", (event) => {
    event.preventDefault();
    event.stopPropagation();
    const form = event.currentTarget;
    limits = {};
    if (form.elements.minutes.value)
      limits.max_worker_seconds = Number(form.elements.minutes.value) * 60;
    if (form.elements.mb.value)
      limits.max_image_growth_mb = Number(form.elements.mb.value);
    $("#recording-limits-dialog").close();
    toast("Analysis settings saved for the next recording.");
  });
  $("#analysis-resume-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    const form = event.currentTarget;
    if (
      await control(form.dataset.analysisId, "resume", {
        max_worker_seconds: Number(form.elements.minutes.value) * 60,
        max_image_growth_mb: Number(form.elements.mb.value),
      })
    )
      $("#analysis-resume-dialog").close();
  });
  function connectionLost() {
    state.data.worker = {
      enabled: true,
      running: false,
      connection_lost: true,
      error: "Cannot reach the local server. Restart it to reconnect.",
    };
    render();
  }
  window.hoglineRecordings = { render, showTab, openJobs, connectionLost, reloadResults: () => loadResults(true), proposedGames: () => scoreBooks };
  showTab("results");
  setInterval(tick, 1000);
  render();
})();
