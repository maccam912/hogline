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
    <div><span class="eyebrow">RECORDING ANALYSIS</span><h3>Find the games. Follow every end.</h3><p id="analysis-description">Survey the whole recording, read scores and inspect game and end evidence.</p></div>
    <div class="analysis-start"><button class="button primary" id="analyze-recording">Analyze recording <span aria-hidden="true">↗</span></button><button class="button quiet" id="recording-limits-button">Analysis settings</button></div>
  </section><div id="recording-progress"></div>
  <div class="recording-tabs" role="tablist" aria-label="Recording workspace"><button role="tab" aria-selected="true" data-recording-tab="results">Results</button><button role="tab" aria-selected="false" data-recording-tab="video">Video &amp; evidence</button><button role="tab" aria-selected="false" data-recording-tab="tools">Advanced tools</button></div>
  <section id="recording-results" role="tabpanel"><div class="results-heading"><div><span class="eyebrow">GAME BY GAME</span><h3>Games, ends &amp; scores</h3></div><button class="button quiet" id="refresh-recording-results">Refresh results</button></div><div id="recording-results-content"></div></section>`;
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
  $(".page-heading .eyebrow").textContent = "YOUR CURLING ARCHIVE";
  $(".page-heading h1").textContent = "From recording to results.";
  $(".page-heading .muted").textContent =
    "Choose a recording. Start an analysis. Follow the progress here.";
  const banner = document.createElement("section");
  banner.id = "processing-banner";
  banner.setAttribute("aria-label", "Processing status");
  $(".page-heading").after(banner);
  const dialogs = document.createElement("div");
  dialogs.innerHTML = `<dialog id="jobs-dialog" class="workspace-dialog"><div class="workspace-dialog-header"><div><span class="eyebrow">PROCESSING</span><h2>Jobs &amp; progress</h2><p id="jobs-worker-status"></p></div><button class="close-dialog" aria-label="Close jobs">×</button></div><div id="jobs-content"></div></dialog>
    <dialog id="recording-limits-dialog"><button class="close-dialog" aria-label="Close analysis settings">×</button><span class="eyebrow">ONE RECORDING · ALL STAGES</span><h2>Analysis settings</h2><p>The full recording is surveyed. Detailed inspection focuses on selected moments. These settings apply to your next analysis. Change existing limits in Jobs.</p><form id="recording-limits-form"><label>Processing time limit (minutes)<input name="minutes" type="number" min="1" max="1440" placeholder="Automatic" /></label><label>Additional retained data (MB)<input name="mb" type="number" min="32" max="4096" placeholder="Automatic" /></label><button class="button primary" type="submit">Save for next analysis</button></form></dialog>
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
      completed: "Finished",
      cancelled: "Cancelled",
    };
  const batches = () => state.data.batches || [];
  const sortedBatches = () =>
    [...batches()].sort(
      (a, b) =>
        (a.state === "active" ? -2 : a.state === "paused" ? -1 : 0) -
          (b.state === "active" ? -2 : b.state === "paused" ? -1 : 0) ||
        (b.created_at || "").localeCompare(a.created_at || ""),
    );
  const related = (sid) =>
    sortedBatches().filter((b) => b.request.source_ids.includes(sid));
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
  const clock = (seconds, updated) =>
    `<span data-elapsed="${Number(seconds) || 0}" data-live-updated="${esc(state.data.worker?.running === false ? "" : updated || "")}">${duration(seconds)}</span>`;
  const percent = (value) =>
    `${Math.round(100 * Math.max(0, Math.min(1, value || 0)))}%`;
  function tick() {
    $$("[data-elapsed]").forEach((el) => {
      const start = Date.parse(el.dataset.liveUpdated),
        extra = Number.isFinite(start)
          ? Math.max(0, (Date.now() - start) / 1000)
          : 0;
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
      return `${worker.alive_workers || 1} of ${worker.configured_workers || 1} workers online. Recordings can process in parallel; keep the server running.`;
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
    return `<article class="analysis-progress-card ${batch.state}" data-analysis-card="${esc(batch.id)}"><div class="analysis-card-heading"><div><span class="eyebrow">${whole ? "RECORDING ANALYSIS" + (compact ? ` · STEP ${p.stage_index} OF ${p.stage_count}` : "") : "EARLIER BOUNDED ANALYSIS"}</span><h4>${compact ? esc(p.phase_label || "Recording analysis") : esc(batchTitle(batch))}</h4></div><span class="analysis-state ${batch.state}"><i></i>${esc(status)}</span></div>${!compact ? `<p>${esc(p.phase_label || batch.phase || "Processing")}${whole ? ` · stage ${p.stage_index} of ${p.stage_count}` : ""}</p>` : ""}<div class="analysis-progress-line"><progress max="1" value="${fraction}" aria-label="${whole ? "Overall analysis progress" : "Current stage progress"}"></progress><strong>${percent(fraction)}</strong></div><div class="analysis-metrics"><span><small>ELAPSED PROCESSING</small><strong>${clock(p.processing_elapsed_seconds ?? batch.spent_worker_seconds, p.live_updated_at)}</strong><span class="budget-hint">of ${duration(batch.limits.max_worker_seconds)} limit</span></span><span><small>THIS STAGE</small><strong>${p.completed_tasks ?? batch.succeeded_units} / ${p.total_tasks ?? batch.units.length} tasks</strong></span><span><small>RETAINED DATA</small><strong>${(batch.reserved_image_bytes / 1024 ** 2).toFixed(1)} / ${batch.limits.max_image_growth_mb} MB</strong></span></div>${running.map((job) => `<p class="running-task"><span class="status-dot"></span>${esc(jobLabel(job))} · ${time(job.request.start_seconds)}–${time(job.request.end_seconds)} · ${job.acquisition === "downloading_temporary_section" ? "Downloading video section" : `${job.completed_frames || 0} frames · ${percent(job.progress)}`}</p>`).join("")}${batch.pause_reason ? `<div class="analysis-problem"><strong>${failed.length ? "A task failed. Analysis is paused." : "Analysis is paused."}</strong><p>${esc(failure || batch.pause_reason)}</p><span>${p.queued_jobs || 0} queued tasks will wait until this analysis resumes.</span></div>` : ""}${batch.state === "completed" ? `<p class="fine-print">${whole ? "Analysis pipeline finished. Review uncertain game boundaries, ends and scores in Results." : "This earlier run covered its selected range; it did not analyze the full recording."}</p>` : ""}${controls}${
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
      `${active.length ? active.map((b) => progressCard(b)).join("") : `<div class="results-empty"><h3>No analysis is running</h3><p>Choose a recording and select Analyze recording to start.</p></div>`}${standalone.length ? `<h3>Other queued and running tasks</h3>${standalone.map(individualJob).join("")}` : ""}<details data-history="history" class="analysis-history"><summary>Previous analyses · ${history.length}</summary>${history.map((b) => progressCard(b)).join("")}</details>`;
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
  const score = (points) => `${points?.red ?? "?"}–${points?.yellow ?? "?"}`;
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
    if (key === resultKey && !force) return;
    resultKey = key;
    const current = ++resultGeneration;
    $("#recording-results-content").innerHTML =
      '<p class="muted">Loading game and score evidence…</p>';
    try {
      const report = await api(
        `/sources/${encodeURIComponent(sid)}/end-timeline`,
      );
      if (current !== resultGeneration || sid !== state.source) return;
      const temporal = report.scorecard_reconstruction?.scorecards || [];
      const ledgers = temporal.length ? temporal.map((book) => ({
        ...book,
        highest_observed_end_number: book.highest_posted_end,
        ends: book.ends.map((end) => ({...end, board_posting: end.board_evidence ? {
          first_observation: end.board_evidence.first,
        } : null})),
      })) : report.board_ledgers.filter((b) => b.ends.length);
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
        ? `<article class="recording-game"><div class="recording-game-heading"><div><span class="eyebrow">PARTIAL SCORE EVIDENCE</span><h4>Scoring rows found · game boundaries unresolved</h4></div><span class="pill amber">Needs review</span></div><p class="results-note">${latest(sid)?.state === "completed" ? "Analysis finished. " : ""}Individual board rows support these point proposals. A complete game scorecard and final result are still unresolved.</p><div class="end-table-wrap"><table class="end-table"><thead><tr><th>Posted end</th><th>Scoring row</th><th>Proposed points</th><th>Evidence</th></tr></thead><tbody>${partial.map((p) => `<tr><td>${p.official_number}</td><td>${esc(p.color)}</td><td>+${p.points}</td><td>${p.youtube_links?.[0] ? `<a class="evidence-link" target="_blank" rel="noopener" href="${esc(p.youtube_links[0])}">Check board ↗</a>` : "Recorded frame"}</td></tr>`).join("")}</tbody></table></div><p class="fine-print">These observations are not assigned to a verified game. Unreadable opponent rows and initial totals remain unknown.</p></article>`
        : "";
      const confirmed = sourceGames().filter(
        (g) => g.acceptance === "human_confirmed",
      );
      $("#recording-results-content").innerHTML =
        `${confirmed.map((g) => `<article class="recording-game"><div class="recording-game-heading"><h4>${esc(g.label)}</h4><span class="pill green">Reviewed</span></div><p>${g.ends.length} saved ends · ${esc(g.completion)}</p><button class="button quiet" data-edit="${esc(g.id)}">Review game →</button></article>`).join("")}${ledgers.length ? `<p class="results-note">${ledgers.length} candidate ${ledgers.length === 1 ? "game history" : "game histories"} from repeated board readings. Boundaries and final results need review.</p>${ledgers.map((book, i) => `<article class="recording-game"><div class="recording-game-heading"><div><span class="eyebrow">CANDIDATE GAME ${i + 1}</span><h4>Board evidence · ${time(book.first_seconds)}–${time(book.last_seconds)}</h4></div><span class="pill amber">Needs review</span></div><div class="game-summary-strip"><span>Highest visible end <strong>${book.highest_observed_end_number ?? "?"}</strong></span><span>Score after that end <strong>${score(book.ends.at(-1)?.score_after_end)}</strong></span><span>Final result <strong>Unresolved</strong></span></div><div class="end-table-wrap"><table class="end-table"><thead><tr><th>End</th><th>Points · red–yellow</th><th>Score after end</th><th>Evidence</th></tr></thead><tbody>${book.ends.map((end) => `<tr><td>${end.official_number}</td><td>${score(end.points)}</td><td>${score(end.score_after_end)}</td><td>${end.board_posting?.first_observation?.youtube_url ? `<a class="evidence-link" target="_blank" rel="noopener" href="${esc(end.board_posting.first_observation.youtube_url)}">Check board ↗</a>` : "Unresolved"}</td></tr>`).join("")}</tbody></table></div><p class="fine-print">Ends played and first/final throw times remain unknown. A missing end number is not treated as a blank.</p></article>`).join("")}` : partialCard ? partialCard : !confirmed.length ? `<div class="results-empty"><div class="empty-house-mark">◎</div><h3>${latest(sid)?.state === "active" ? "Analysis is working through the recording" : latest(sid)?.state === "completed" ? "Analysis finished · scorecard unresolved" : "No game scorecard reconstructed yet"}</h3><p>${latest(sid)?.state === "paused" ? "Resume the paused analysis in Jobs, or start a full-recording analysis above." : latest(sid)?.state === "completed" ? "The pipeline finished, but the available evidence did not support a complete game scorecard. Inspect the retained evidence or run further targeted analysis." : "Start an analysis to search the whole recording for game, end and score evidence."}</p><p class="fine-print">Uncertain boundaries and unreadable scores stay unresolved.</p></div>` : ""}<button class="button quiet" data-recording-open-evidence>Inspect detailed end evidence →</button>`;
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
      `<div><span class="status-dot ${paused.length && !active.length ? "paused" : ""}"></span><strong>${state.data.worker?.connection_lost ? "Server unavailable" : running && state.data.worker?.running === false ? "Local worker offline" : running ? `${running} task${running === 1 ? "" : "s"} running` : active.length ? "Analysis queued" : paused.length ? `${paused.length} ${paused.every((b) => b.campaign_purpose !== "recording_analysis") ? "earlier run" : "analysis"} paused` : "Nothing running"}</strong><span>${state.data.worker?.connection_lost ? "Restart the local server to reconnect." : running || active.length ? "Progress updates automatically." : paused.length ? "A task or limit needs attention." : "Ready when you are."}</span></div><button class="button quiet" data-open-jobs>Jobs &amp; progress <span aria-hidden="true">→</span></button>`;
    $$(".source-card[data-source]").forEach((card) => {
      const b = latest(card.dataset.source),
        el = $(".source-count", card);
      if (el)
        el.innerHTML = `<span class="source-status ${b?.state || ""}">${b ? (b.campaign_purpose === "recording_analysis" || ["active", "paused"].includes(b.state) ? labels[b.state] : "Evidence available") : "Ready to analyze"}</span><span>→</span>`;
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
          ? "Analyze again ↗"
          : "Analyze recording ↗";
      $("#analysis-description").textContent =
        `${source()?.duration_seconds ? time(source().duration_seconds) + " recording. " : ""}Survey the full duration, read boards and inspect selected game and end evidence.`;
      $("#recording-progress").innerHTML = b
        ? b.state === "completed"
          ? `<details class="completed-analysis"><summary>Analysis finished · ${duration(b.spent_worker_seconds)} processing · view details</summary>${progressCard(b, true)}</details>`
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
  window.hoglineRecordings = { render, showTab, openJobs, connectionLost };
  showTab("results");
  setInterval(tick, 1000);
  render();
})();
