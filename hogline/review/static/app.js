const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const time = (n) => {
  n = Math.max(0, Math.floor(n || 0));
  return [Math.floor(n / 3600), Math.floor(n / 60) % 60, n % 60]
    .map((v) => String(v).padStart(2, "0"))
    .join(":");
};
const parseTime = (value) => {
  const parts = String(value).trim().split(":");
  if (
    !parts.length ||
    parts.length > 3 ||
    parts.some((p) => !/^\d+(\.\d+)?$/.test(p))
  )
    throw Error("Enter a time as HH:MM:SS or seconds.");
  return parts.reduce((a, p) => a * 60 + Number(p), 0);
};
const state = {
  data: {
    sources: [],
    games: [],
    scans: [],
    evidence: [],
    review_tasks: [],
    jobs: [],
    observations: [],
  },
  source: null,
  seconds: 0,
  view: "composite",
  overlay: true,
  sheet: null,
  reviewOnly: false,
  draft: null,
  audit: {},
};
let toastTimer;
let catalogSignature;
function toast(text, error = false) {
  const el = $("#toast");
  el.textContent = text;
  el.classList.toggle("error", error);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), error ? 12000 : 6000);
}
async function api(path, method = "GET", body) {
  const response = await fetch("/api" + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const error = await response
      .json()
      .catch(() => ({ detail: response.statusText }));
    throw Error(
      typeof error.detail === "string"
        ? error.detail
        : JSON.stringify(error.detail),
    );
  }
  return response.json();
}
const source = () => state.data.sources.find((s) => s.id === state.source);
const sourceGames = () =>
  state.data.games.filter((g) =>
    g.spans.some((s) => s.source_id === state.source),
  );
const frames = () =>
  state.data.evidence
    .filter((e) => e.source_id === state.source)
    .sort((a, b) => a.source_seconds - b.source_seconds);
const sourceTimeline = () =>
  state.data.timelines?.find((t) => t.source_id === state.source);
const currentFrame = () => {
  const locked = window.hoglineLabels?.evidenceId();
  if (locked) return state.data.evidence.find((e) => e.id === locked);
  return frames().reduce((best, e) => {
    if (!best) return e;
    const distance = Math.abs(e.source_seconds - state.seconds),
      previous = Math.abs(best.source_seconds - state.seconds);
    return distance < previous ||
      (Math.abs(distance - previous) < 0.001 &&
        e.image.width * e.image.height > best.image.width * best.image.height)
      ? e
      : best;
  }, null);
};
const youtube = (src, seconds) =>
  src?.platform === "youtube"
    ? `https://www.youtube.com/watch?v=${encodeURIComponent(src.platform_id)}&t=${Math.floor(Math.max(0, seconds))}s`
    : null;

async function refresh() {
  const incoming = await api("/catalog");
  const signature = JSON.stringify(incoming);
  if (signature === catalogSignature) return;
  catalogSignature = signature;
  state.data = incoming;
  if (!state.source && state.data.sources.length) {
    state.source = state.data.sources[0].id;
    state.seconds = frames()[0]?.source_seconds || 0;
  }
  render();
}
function render() {
  const { sources, games, review_tasks } = state.data;
  $("#build-label").textContent = `PILOT ${state.data.app_version || ""}`;
  $("#recording-count").textContent = sources.length;
  $("#review-count").textContent = review_tasks.filter(
    (t) => t.state === "open",
  ).length;
  $("#stat-sources").textContent = sources.length;
  const hours =
    sources.reduce((a, s) => a + (s.duration_seconds || 0), 0) / 3600;
  $("#stat-hours").textContent = `${hours.toFixed(1)}h`;
  $("#stat-games").textContent = games.length;
  $("#stat-confirmed").textContent = games.filter(
    (g) => g.acceptance === "human_confirmed",
  ).length;
  $("#stat-results").textContent = games.filter(
    (g) => g.result.acceptance === "human_confirmed",
  ).length;
  $("#archive-nav").classList.toggle("selected", !state.reviewOnly);
  $("#review-nav").classList.toggle("selected", state.reviewOnly);
  $("#sheet-filters").innerHTML = Array.from(
    { length: 8 },
    (_, i) =>
      `<button class="${state.sheet === i + 1 ? "active" : ""}" data-sheet="${i + 1}" aria-pressed="${state.sheet === i + 1}">${i + 1}</button>`,
  ).join("");
  const query = $("#search").value.toLowerCase();
  const filtered = sources.filter((s) => {
    const related = games.filter((g) =>
      g.spans.some((p) => p.source_id === s.id),
    );
    return (
      (!state.sheet || s.sheet === state.sheet) &&
      (!state.reviewOnly ||
        related.some((g) => g.acceptance !== "human_confirmed")) &&
      `${s.title} ${s.title_date} ${related.map((g) => `${g.label} ${g.red_team} ${g.yellow_team} ${g.league}`).join(" ")}`
        .toLowerCase()
        .includes(query)
    );
  });
  $("#list-heading").textContent = state.reviewOnly
    ? "Needs review"
    : "Source recordings";
  $("#source-list-count").textContent = filtered.length;
  $("#source-list").innerHTML =
    filtered
      .map((s) => {
        const count = games.filter((g) =>
          g.spans.some((p) => p.source_id === s.id),
        ).length;
        return `<button class="source-card ${s.id === state.source ? "active" : ""}" data-source="${s.id}"><span class="source-meta"><span>SHEET ${s.sheet || "?"}</span><span>${s.duration_seconds ? time(s.duration_seconds) : "DURATION ?"}</span></span><strong>${escapeHTML(s.title)}</strong><small>${escapeHTML(s.title_date || "Title date unknown")} · ${s.platform === "youtube" ? "YouTube" : "Local media"}</small><span class="source-count"><span>◷ ${count} game interval${count === 1 ? "" : "s"}</span><span>↗ source</span></span></button>`;
      })
      .join("") || '<p class="no-data">No recordings in this view.</p>';
  const src = source();
  $("#empty-viewer").hidden = !!src;
  $("#viewer-content").hidden = !src;
  $("#clock-status").hidden = !src;
  $("#source-title").textContent = src?.title || "Your archive starts here.";
  $("#source-kicker").textContent = src
    ? `SHEET ${src.sheet || "?"} / ${src.title_date || "DATE UNKNOWN"} / SOURCE RECORDING`
    : "SOURCE VIEWER";
  if (!src) return;
  const duration = src.duration_seconds || Math.max(state.seconds + 600, 3600);
  $("#scrubber").max = duration;
  $("#scrubber").value = state.seconds;
  if (document.activeElement !== $("#jump-input"))
    $("#jump-input").value = time(state.seconds);
  $("#timeline-duration").textContent =
    `${time(state.seconds)} / ${src.duration_seconds ? time(duration) : "duration unknown"}`;
  renderFrame();
  renderTimeline(duration);
  renderGames();
  renderJobs();
  window.hoglineProcessing?.render();
  window.hoglineBoundaries?.render();
  window.hoglineWindows?.render();
  window.hoglineBoardTimeline?.render();
  window.hoglineReconciliation?.render();
}
function renderFrame() {
  const src = source(),
    frame = currentFrame();
  if (!src) return;
  const observation = state.data.observations?.find(
    (o) => o.evidence_id === frame?.id,
  );
  const selectedTime = frame?.source_seconds ?? state.seconds;
  const stage = $("#frame-stage");
  const key = `${frame?.id || "none"}:${state.view}:${observation?.id || "none"}:${state.overlay}:${window.hoglineLabels?.key() || ""}`;
  if (stage.dataset.key !== key) {
    stage.dataset.key = key;
    stage.innerHTML = frame
      ? `<div class="frame-image"><img src="/api/evidence/${frame.id}/${state.view}" alt="Evidence from ${escapeHTML(src.title)} at ${time(selectedTime)}">${state.overlay && observation ? renderDetections(frame, observation) : ""}${window.hoglineLabels?.overlay(frame, state.view) || ""}</div><span class="frame-stamp">SOURCE ${time(selectedTime)} · ${state.view === "composite" ? (frame.acquisition_tier === "coarse" ? "COARSE FRAME" : "ORIGINAL FRAME") : "SEED CROP"}</span>`
      : '<div class="frame-placeholder"><span>NO CACHED FRAME</span><p>Open the source on YouTube, or cache a frame at this moment.</p></div>';
    const img = $("img", stage);
    if (img)
      img.onerror = () => {
        stage.innerHTML =
          '<div class="frame-placeholder"><span>CACHED FRAME UNAVAILABLE</span><p>The original timestamp link is still available.</p></div>';
      };
  }
  $("#frame-time").textContent = time(selectedTime);
  $("#pts-label").textContent = frame
    ? `${frame.image.width} × ${frame.image.height} · ${frame.acquisition_tier === "coarse" ? "coarse activity scan" : "detailed evidence"} · PTS ${frame.pts} × ${frame.time_base_num}/${frame.time_base_den} · wall clock unresolved`
    : "Source time · wall clock unresolved";
  $("#cache-frame").textContent = `Detailed frame at ${time(state.seconds)}`;
  const link = $("#youtube-link");
  link.href =
    youtube(src, selectedTime) || `/api/media/${src.id}#t=${selectedTime}`;
  link.textContent =
    src.platform === "youtube" ? "Check on YouTube ↗" : "Open local video ↗";
  $$(".view-tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === state.view),
  );
  $("#overlay-toggle").classList.toggle("active", state.overlay);
  $("#overlay-toggle").setAttribute("aria-pressed", String(state.overlay));
  const count = observation
    ? Object.values(observation.views).reduce(
        (n, v) => n + v.detections.length,
        0,
      )
    : 0;
  const rejected = observation
    ? Object.entries(observation.views).flatMap(([view, data]) =>
        (data.rejected_candidates || []).map((d) => ({ ...d, view })),
      )
    : [];
  $("#perception-caption").innerHTML =
    frame?.acquisition_tier === "coarse"
      ? '<p class="fine-print">Coarse scan · activity only. Request a detailed frame to inspect handles and the scoreboard.</p>'
      : observation
        ? `<span class="pill green">OPENCV BASELINE</span><span>${count} colored-handle candidates · uncalibrated · scores unknown</span>${rejected.length ? `<details class="rejected-candidates"><summary>${rejected.length} color candidates rejected · inspect reasons</summary><p class="fine-print">${escapeHTML(observation.model_version)} · size and shape checks are development priors; rejected candidates can include missed or occluded handles.</p>${rejected.map((d) => `<p class="fine-print">${d.view === "house_top" ? "Top" : "Bottom"} · ${escapeHTML(d.color)} at ${d.center_px.map((v) => Math.round(v)).join(", ")} px · ${d.rejection_reasons.map((r) => escapeHTML(r.replaceAll("_", " "))).join(", ")}</p>`).join("")}</details>` : ""}`
        : `<span class="fine-print">${frame ? "This cached frame has not been analyzed." : "Evidence will be analyzed with OpenCV when cached."}</span>${frame ? '<button class="button quiet" id="analyze-cached">Analyze cached frames →</button>' : ""}`;
  renderBroadcast(frame);
}
function renderBroadcast(frame) {
  const panel = $("#broadcast-content"),
    data = state.data.broadcast;
  if (frame?.acquisition_tier === "coarse") {
    panel.innerHTML =
      '<p class="fine-print">Clock and scoreboard reading need a detailed frame. This coarse image is retained for activity screening.</p>';
    return;
  }
  const boardLocation = state.data.board_locations?.find(
    (o) => o.evidence_id === frame?.id,
  );
  const labelButton = frame
    ? `<button class="button secondary" data-label-board="${frame.id}">Label visible board cards →</button>`
    : "";
  const locationPanel = boardLocation
    ? `<section class="board-location"><h4>Automatic board location</h4><p class="fine-print">${boardLocation.location.status === "proposed" ? "Red, blue-header, and yellow bands form a board candidate. Point columns and card meanings still need verification." : boardLocation.location.status === "ambiguous" ? "Multiple boards fit the color layout. No board has been selected." : "Board color bands could not be located in this frame; obstruction, lighting, or a different layout may be responsible."}</p><div class="board-location-crops">${boardLocation.crops.map((c, i) => `<figure><img src="/api/board-locations/${encodeURIComponent(boardLocation.id)}/${i}.png" alt="Automatically located scoreboard candidate ${i + 1}"><figcaption>${c.width} × ${c.height} original source pixels · unverified location</figcaption></figure>`).join("")}</div></section>`
    : "";
  const observation = data?.observations.find(
    (o) => o.evidence_id === frame?.id,
  );
  const proposals =
    data?.score_proposals.filter((p) => p.source_id === state.source) || [];
  const width = frame?.image.width,
    height = frame?.image.height;
  if (!observation) {
    $("#broadcast-summary").textContent =
      boardLocation?.location.status === "proposed"
        ? "Board location proposed"
        : "Not analyzed";
    panel.innerHTML = `${labelButton}${locationPanel}<p class="fine-print">${width ? `${width} × ${height} source pixels. ` : ""}Clock and board templates are checked per sheet. Read the retained frames to generate evidence-linked proposals.</p>`;
    return;
  }
  const clock = observation.clock,
    board = observation.scoreboard;
  $("#broadcast-summary").textContent =
    board.status === "unconfigured"
      ? "Board template needed"
      : "Proposals · review required";
  const registration = board.header_registration;
  const crops = Object.keys(observation.crops)
    .map(
      (name) =>
        `<figure class="${name.startsWith("scoreboard") ? "board-evidence" : ""}"><img src="/api/broadcast/${observation.id}/${name}" alt="Source evidence crop: ${escapeHTML(name)}">${name === "scoreboard" && registration?.status === "proposed" ? `<div class="point-column-labels" aria-label="Proposed cumulative point columns 1 through 15">${registration.point_centers.map((x, i) => `<span style="left:${x * 100}%">${i + 1}</span>`).join("")}</div>` : ""}<figcaption>${escapeHTML(name.replaceAll("_", " "))}${name === "scoreboard" && registration?.status === "proposed" ? " · proposed cumulative columns" : ""}</figcaption></figure>`,
    )
    .join("");
  const layoutInfo = board.layout_alignment;
  const clockDetails = [
    ...(clock.date_reads || []).map((r) => ["Date", r]),
    ...(clock.time_reads || []).map((r) => ["Time", r]),
  ];
  const diagnostics = `<details class="broadcast-diagnostics"><summary>Clock readings and layout checks</summary>${clock.ocr_conflict ? '<p class="fine-print">Competing valid OCR readings remain. Temporal mappings can support an alternative without rewriting this frame.</p>' : ""}${clockDetails.map(([kind, r]) => `<p class="fine-print">${kind}: ${escapeHTML(r.text || "unreadable")} · ${escapeHTML(r.crop || "seed crop")} · ${escapeHTML(r.preprocessing || "otsu")}</p>`).join("")}${layoutInfo ? `<p class="fine-print">Board alignment: ${escapeHTML(layoutInfo.status.replaceAll("_", " "))}${layoutInfo.translation_source_px ? ` · proposed crop translation ${layoutInfo.translation_source_px.join(", ")} pixels` : ""}. Experimental check; all scores remain proposals.</p>` : ""}${
    board.card_visibility_version
      ? `<p class="fine-print">Card visibility: white patches must reach the upper row and span most of its height. Dark or discolored slots remain unreadable.</p>${(
          board.unreadable_slots || []
        )
          .filter((s) => s.reason)
          .map(
            (s) =>
              `<p class="fine-print">${escapeHTML(s.color)} column ${s.cumulative_points}: ${escapeHTML(s.reason.replaceAll("_", " "))}</p>`,
          )
          .join("")}`
      : ""
  }${board.missing_reason ? `<p class="fine-print">${escapeHTML(board.missing_reason.replaceAll("_", " "))}</p>` : ""}</details>`;
  const headerInfo = registration
    ? `<p class="fine-print">Automatic point columns: ${escapeHTML(registration.status.replaceAll("_", " "))}.${registration.header_correlation != null ? ` Header similarity ${registration.header_correlation.toFixed(2)}, weakest section ${Math.min(...registration.regional_correlations).toFixed(2)}. These are experimental image checks, not confidence probabilities.` : ""} ${registration.reference?.youtube_url ? `<a class="evidence-link" href="${escapeHTML(registration.reference.youtube_url)}" target="_blank" rel="noopener">Compare fixed-header reference ↗</a>` : ""}</p>`
    : "";
  const cards = board.cards
    .map(
      (c) =>
        `<span class="board-card ${c.color}">End ${c.end_number ?? "?"} → ${c.cumulative_points} cumulative</span>`,
    )
    .join("");
  panel.innerHTML = `${labelButton}${locationPanel}${diagnostics}<p class="fine-print">${width} × ${height} source pixels · seed views uncalibrated · sheet ${observation.sheet}</p><div class="broadcast-crops">${crops}</div><h4>Observed clock</h4><p>${escapeHTML(clock.date_text || (clock.candidates.length ? "Date alternatives unresolved" : "Date unreadable"))} · ${escapeHTML(clock.time_text || (clock.candidates.length ? "Time alternatives unresolved" : "Time unreadable"))}</p><p class="fine-print">${clock.candidates.length ? `${clock.candidates.length} wall-time alternative${clock.candidates.length === 1 ? "" : "s"} · ${clock.ambiguous ? "OCR, AM/PM or DST alternatives remain" : "mapping still proposed"}` : escapeHTML(clock.missing_reason || "Clock not resolved")}. The recording title is not substituted for an unreadable overlay.</p><h4>Traditional board</h4>${headerInfo}${board.status === "unconfigured" ? '<p class="fine-print">Board reading is unavailable for this recording, time, or alignment. Its source crop and missing reason remain available.</p>' : `<div class="board-cards">${cards || '<p class="fine-print">No legible scoring cards.</p>'}</div><p class="fine-print">Numbers on movable cards identify ends; board position is cumulative points. ${board.unreadable_slots?.length || 0} slots unreadable or obstructed. Storage and blank markers remain uninterpreted.</p>`}${proposals.length ? `<h4>Score proposals from this recording</h4>${proposals.map((p) => `<div class="score-proposal"><span>End ${p.official_number}: ${p.color} +${p.points}</span><a class="evidence-link" target="_blank" rel="noopener" href="${escapeHTML(p.youtube_links[0])}">${p.evidence_ids.length} frames · ${time(p.source_start_seconds)} ↗</a><button class="button quiet" data-score-proposal="${p.id}">Add to game review →</button></div>`).join("")}` : '<p class="fine-print">No score proposal has passed the temporal consistency checks.</p>'}`;
}

function useScoreProposal(id) {
  const proposal = state.data.broadcast.score_proposals.find(
    (p) => p.id === id,
  );
  if (!proposal) return;
  if (state.draft) readEditor();
  else {
    const matching = sourceGames().filter((g) =>
      proposal.evidence_ids.every((id) => {
        const e = state.data.evidence.find((e) => e.id === id);
        return g.spans.some(
          (s) =>
            s.source_id === e.source_id &&
            e.source_seconds >= s.start_seconds &&
            e.source_seconds <= s.end_seconds,
        );
      }),
    );
    if (matching.length !== 1)
      return toast(
        "Open the intended game review and link the supporting source span first.",
        true,
      );
    state.draft = {
      revision: matching[0].revision,
      game: cleanGame(matching[0]),
    };
    state.audit = {};
  }
  const g = state.draft.game;
  if (g.ends.some((e) => e.official_number === proposal.official_number))
    return toast(
      "That official end already exists. Compare the evidence in its review; the proposal will not overwrite it.",
      true,
    );
  g.ends.push({
    id: crypto.randomUUID(),
    observed_index: Math.max(0, ...g.ends.map((e) => e.observed_index)) + 1,
    official_number: proposal.official_number,
    state: "played",
    red_points: proposal.color === "red" ? proposal.points : 0,
    yellow_points: proposal.color === "yellow" ? proposal.points : 0,
    acceptance: "proposed",
    evidence_ids: proposal.evidence_ids,
    note: "Scoreboard-history proposal from cumulative column differences; end play and template require review.",
  });
  renderEditor();
  toast(
    "Proposed end added to the review draft. Inspect its evidence and save a revision when ready.",
  );
}
function renderDetections(frame, observation) {
  const asset =
    state.view === "composite" ? frame.image : frame.crops[state.view];
  let shapes = "";
  for (const [view, data] of Object.entries(observation.views)) {
    if (state.view !== "composite" && state.view !== view) continue;
    const bounds = frame.crops[view].normalized_bounds;
    data.detections.forEach((d, i) => {
      let [x, y, w, h] = d.bbox_px;
      if (state.view === "composite") {
        x = bounds[0] * asset.width + x;
        y = bounds[1] * asset.height + y;
      }
      const color = d.color === "red" ? "#ff7467" : "#ffe67c";
      shapes += `<rect x="${x - 3}" y="${y - 3}" width="${w + 6}" height="${h + 6}" rx="3" fill="none" stroke="${color}" stroke-width="2"/><text x="${x - 3}" y="${Math.max(12, y - 7)}" fill="${color}" font-size="12" font-family="monospace" stroke="#000" stroke-width=".35">${d.color === "red" ? "R" : "Y"}${i + 1}</text>`;
    });
  }
  return `<svg viewBox="0 0 ${asset.width} ${asset.height}" aria-label="Unverified OpenCV colored-handle candidates">${shapes}</svg>`;
}
function renderTimeline(duration) {
  const spans = sourceGames().flatMap((g) =>
    g.spans
      .filter((s) => s.source_id === state.source)
      .map((s) => ({ ...s, label: g.label, kind: "game" })),
  );
  const timeline = sourceTimeline();
  const scans = (timeline?.sparse_scanned_intervals || []).map(
    ([start_seconds, end_seconds]) => ({
      start_seconds,
      end_seconds,
      label: "Sparse sampled span · time between samples is unobserved",
      kind: "scan",
    }),
  );
  const activity = (timeline?.activity_runs || []).map((r) => ({
    ...r,
    label: "Motion activity · boundaries unconfirmed",
    kind: "activity",
  }));
  $("#timeline-track").innerHTML = [...scans, ...activity, ...spans]
    .map(
      (s) =>
        `<span class="time-segment ${s.kind}" style="left:${(100 * s.start_seconds) / duration}%;width:${(100 * (s.end_seconds - s.start_seconds)) / duration}%" title="${escapeHTML(s.label)} · ${time(s.start_seconds)}–${time(s.end_seconds)}"></span>`,
    )
    .join("");
  $("#coverage-summary").textContent = timeline
    ? `${time(timeline.sparse_scanned_seconds)} of requested spans sampled · ${timeline.unique_owned_samples} sample times · gaps remain unknown${timeline.stale_scans_excluded ? ` · ${timeline.stale_scans_excluded} outdated scans excluded` : ""}`
    : "No sampled coverage yet.";
  $("#activity-list").innerHTML =
    activity
      .map(
        (r) =>
          `<article class="activity-card"><div><strong>${time(r.start_seconds)}–${time(r.end_seconds)}</strong><p class="fine-print">Motion activity · ±${r.boundary_uncertainty_seconds}s sample spacing · ${r.evidence_ids.length} supporting frames</p></div>${r.youtube_url ? `<a class="evidence-link" href="${escapeHTML(r.youtube_url)}" target="_blank" rel="noopener">YouTube ↗</a>` : ""}<button class="button quiet" data-activity="${r.id}">Review interval →</button></article>`,
      )
      .join("") ||
    '<p class="fine-print">No motion runs yet. A quiet sample does not prove the sheet is empty.</p>';
}
function renderGames() {
  $("#game-list").innerHTML =
    sourceGames()
      .map((g) => {
        const span = g.spans.find((s) => s.source_id === state.source);
        return `<article class="game-card"><span class="game-icon">◎</span><div><h3>${escapeHTML(g.label)}</h3><p>${time(span.start_seconds)}—${time(span.end_seconds)} · ${g.coverage.toUpperCase()} · ${g.ends.length} ENDS</p></div><span class="pill ${g.acceptance === "human_confirmed" ? "green" : "amber"}">${g.acceptance === "human_confirmed" ? "Reviewed" : "Needs review"}</span><button class="button quiet" data-edit="${g.id}">Review →</button></article>`;
      })
      .join("") ||
    '<p class="no-data">No game intervals yet. Mark one from the recording, or scan a section to propose activity.</p>';
}
function renderJobs() {
  $("#job-list").innerHTML =
    state.data.jobs
      .filter((j) => j.request.source_id === state.source)
      .map(
        (j) =>
          `<div class="job-card"><span class="pill ${j.state === "failed" ? "amber" : ""}">${escapeHTML(j.state)}</span><code>${time(j.request.start_seconds)}–${time(j.request.end_seconds)}</code><span>${j.completed_frames || 0} frames · ${j.cache_hits || 0} reused</span>${j.state === "running" ? `<progress max="1" value="${j.progress || 0}"></progress>` : ""}${j.state === "failed" && j.attempts < 3 && !(j.kind === "dense_window" && (j.stage_version !== state.data.window_version || j.perception_version !== state.data.window_detector_version || j.visibility_version !== state.data.window_visibility_version || j.decoder_version !== state.data.window_decoder_version || j.retention_version !== state.data.window_retention_version)) ? `<button class="button quiet" data-retry="${j.id}">Retry</button>` : ""}${j.error ? `<span class="job-error">${escapeHTML(j.error)}</span>` : ""}</div>`,
      )
      .join("") ||
    '<p class="no-data">No work queued. Metadata imports never start a video backfill.</p>';
}

function cleanGame(raw) {
  const game = structuredClone(raw);
  delete game.revision;
  game.spans.forEach((s) => delete s.youtube_url);
  return game;
}
function options(items, value) {
  return items
    .map(
      ([v, label]) =>
        `<option value="${escapeHTML(v)}" ${v === value ? "selected" : ""}>${escapeHTML(label)}</option>`,
    )
    .join("");
}
const acceptanceOptions = [
  ["unknown", "Unknown"],
  ["proposed", "Proposed"],
  ["human_confirmed", "Human confirmed"],
  ["disputed", "Disputed"],
];
function input(label, name, value, type = "text", extra = "") {
  return `<label>${label}<input name="${name}" value="${escapeHTML(value ?? "")}" type="${type}" ${extra}></label>`;
}
function select(label, name, items, value) {
  return `<label>${label}<select name="${name}">${options(items, value)}</select></label>`;
}
function evidenceOptions(game) {
  return [
    ["", "No evidence selected"],
    ...state.data.evidence
      .filter((e) =>
        game.spans.some(
          (s) =>
            s.source_id === e.source_id &&
            e.source_seconds >= s.start_seconds &&
            e.source_seconds <= s.end_seconds,
        ),
      )
      .map((e) => [
        e.id,
        `${time(e.source_seconds)} · sheet ${state.data.sources.find((s) => s.id === e.source_id)?.sheet || "?"} · ${e.id.slice(0, 6)}`,
      ]),
  ];
}
function newGame(activity = null) {
  const src = source();
  if (!src?.sheet)
    return toast(
      "Import a recording with a known sheet first, or assign its sheet through a local import.",
      true,
    );
  const start = Math.min(
    activity?.start_seconds ?? state.seconds,
    Math.max(0, (src.duration_seconds || 3600) - 1),
  );
  state.audit = {};
  state.draft = {
    revision: 0,
    game: {
      id: crypto.randomUUID(),
      sheet: src.sheet,
      label: "Game · sheet " + src.sheet,
      league: null,
      red_team: "Red · unidentified",
      yellow_team: "Yellow · unidentified",
      acceptance: "proposed",
      coverage: "unknown",
      completion: "unknown",
      spans: [
        {
          source_id: src.id,
          start_seconds: start,
          end_seconds: Math.min(
            activity?.end_seconds ?? start + 600,
            src.duration_seconds || start + 600,
          ),
          relationship: "primary",
          boundary_uncertainty_seconds:
            activity?.boundary_uncertainty_seconds ?? null,
        },
      ],
      ends: [],
      result: {
        outcome: "unknown",
        red_points: null,
        yellow_points: null,
        acceptance: "unknown",
        evidence_ids: [],
      },
      note: activity
        ? "Proposed from sparse motion activity; game boundaries still need review."
        : "",
      retired_into: [],
    },
  };
  renderEditor();
}
function editGame(id) {
  const raw = state.data.games.find((g) => g.id === id);
  state.audit = {};
  state.draft = { revision: raw.revision, game: cleanGame(raw) };
  const span = raw.spans.find((s) => s.source_id === state.source);
  if (span) state.seconds = span.start_seconds;
  render();
  renderEditor();
}
function renderEditor() {
  const box = $("#game-editor");
  box.hidden = !state.draft;
  if (!state.draft) return;
  const { game, revision } = state.draft,
    ev = evidenceOptions(game);
  box.innerHTML = `<form class="editor" id="edit-form"><div class="section-heading"><h3>Review game interval</h3><span class="pill">${revision ? "REVISION " + revision : "NEW INTERPRETATION"}</span></div><p>Confirm boundaries separately from scores and final results. Only attached evidence can support a score.</p>
    <div class="form-grid">${input("Game label", "label", game.label)}${input("League · leave blank if unknown", "league", game.league)}${input("Red team", "red_team", game.red_team)}${input("Yellow team", "yellow_team", game.yellow_team)}</div>
    <div class="form-grid three">${select("Boundary review", "acceptance", acceptanceOptions, game.acceptance)}${select(
      "Recording coverage",
      "coverage",
      [
        ["unknown", "Unknown"],
        ["partial", "Partial game"],
        ["complete", "Complete game"],
      ],
      game.coverage,
    )}${select(
      "Game completion",
      "completion",
      [
        ["unknown", "Unknown"],
        ["complete", "Complete"],
        ["conceded", "Conceded"],
        ["forfeit", "Forfeit"],
      ],
      game.completion,
    )}</div>
    <h4>Linked source spans</h4><p class="fine-print">Add another recording to represent a continuation or overlap. These spans describe one canonical game.</p>
    ${game.spans
      .map(
        (s, i) =>
          `<div class="span-row" data-span="${i}">${select(
            "Recording",
            `span-${i}-source`,
            state.data.sources
              .filter((src) => src.sheet === game.sheet)
              .map((src) => [
                src.id,
                `${src.title} · ${time(src.duration_seconds)} · ${src.platform_id.slice(0, 8)}`,
              ]),
            s.source_id,
          )}<div class="form-grid three">${input("Start · HH:MM:SS", `span-${i}-start`, time(s.start_seconds))}${input("End · HH:MM:SS", `span-${i}-end`, time(s.end_seconds))}${select(
            "Relationship",
            `span-${i}-relationship`,
            [
              "primary",
              "overlap",
              "continuation",
              "alternate_view",
              "unresolved",
            ].map((v) => [v, v]),
            s.relationship,
          )}</div><div class="row-actions"><a class="evidence-link" href="${
            youtube(
              state.data.sources.find((src) => src.id === s.source_id),
              s.start_seconds,
            ) || `/api/media/${s.source_id}#t=${s.start_seconds}`
          }" target="_blank" rel="noopener">Check boundary at ${time(s.start_seconds)} ↗</a>${game.spans.length > 1 ? `<button type="button" class="button quiet" data-remove-span="${i}">Remove span</button>` : ""}</div></div>`,
      )
      .join("")}
    <button type="button" class="button quiet" id="add-span">＋ Link another recording</button>
    <h4>End-by-end observations</h4>${
      game.ends
        .map(
          (e, i) =>
            `<div class="end-row" data-end="${i}"><div class="row-actions"><strong class="fine-print">OBSERVED END ${e.observed_index}</strong><button class="button quiet" type="button" data-remove-end="${i}">Remove</button></div><div class="form-grid three">${input("Official end · unknown allowed", `end-${i}-official`, e.official_number, "number", 'min="1"')}${select(
              "State",
              `end-${i}-state`,
              [
                ["unknown", "Unknown"],
                ["played", "Played / scored"],
                ["blank", "Blank / 0–0"],
                ["unplayed", "Unplayed"],
              ],
              e.state,
            )}${select("Review", `end-${i}-acceptance`, acceptanceOptions, e.acceptance)}</div><div class="form-grid">${input("Red points · null until known", `end-${i}-red`, e.red_points, "number", 'min="0" max="8" class="red-score"')}${input("Yellow points · null until known", `end-${i}-yellow`, e.yellow_points, "number", 'min="0" max="8" class="yellow-score"')}</div>${select("Supporting frame", `end-${i}-evidence`, ev, e.evidence_ids[0] || "")}${e.evidence_ids[0] ? `<a class="evidence-link" target="_blank" rel="noopener" href="${state.data.evidence.find((f) => f.id === e.evidence_ids[0])?.youtube_url || "#"}">Check score on YouTube ↗</a>` : ""}</div>`,
        )
        .join("") ||
      '<p class="fine-print">No ends recorded. A missing score is never zero.</p>'
    }<button type="button" class="button quiet" id="add-end">＋ Add an observed end</button>
    <h4>Final result · independent confirmation</h4><p class="fine-print">A partial recording or a leading score does not establish a winner.</p><div class="form-grid">${select(
      "Outcome",
      "result-outcome",
      [
        ["unknown", "Unknown"],
        ["red_win", "Red wins"],
        ["yellow_win", "Yellow wins"],
        ["tie", "Tie"],
        ["forfeit", "Forfeit"],
      ],
      game.result.outcome,
    )}${select("Result review", "result-acceptance", acceptanceOptions, game.result.acceptance)}${input("Final red points", "result-red", game.result.red_points, "number", 'min="0"')}${input("Final yellow points", "result-yellow", game.result.yellow_points, "number", 'min="0"')}</div>${select("Final-result evidence", "result-evidence", ev, game.result.evidence_ids[0] || "")}
    <label>Review notes<textarea name="note" rows="2">${escapeHTML(game.note)}</textarea></label><div class="form-grid">${input("Reviewer", "author", state.audit.author || localStorage.getItem("hogline-reviewer") || "", "text", "required")}${input("Reason for this revision", "reason", state.audit.reason || "", "text", 'required placeholder="What changed, and why?"')}</div><div id="revision-history"></div><div class="editor-actions"><button type="button" class="button quiet" id="history-button">Revision history</button>${revision ? `<button type="button" class="button quiet" data-reconcile="${game.id}">Split or merge →</button>` : ""}<button type="button" class="button secondary" id="cancel-edit">Close</button><button type="submit" class="button primary">Save revision →</button></div></form>`;
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}
function readEditor() {
  const form = $("#edit-form");
  if (!form || !state.draft) return;
  const values = new FormData(form),
    g = state.draft.game,
    get = (name) => values.get(name),
    number = (name) => (get(name) === "" ? null : Number(get(name)));
  [
    "label",
    "red_team",
    "yellow_team",
    "acceptance",
    "coverage",
    "completion",
    "note",
  ].forEach((k) => (g[k] = get(k)));
  g.league = get("league") || null;
  g.spans = g.spans.map((s, i) => ({
    ...s,
    source_id: get(`span-${i}-source`),
    start_seconds:
      get(`span-${i}-start`) === time(s.start_seconds)
        ? s.start_seconds
        : parseTime(get(`span-${i}-start`)),
    end_seconds:
      get(`span-${i}-end`) === time(s.end_seconds)
        ? s.end_seconds
        : parseTime(get(`span-${i}-end`)),
    relationship: get(`span-${i}-relationship`),
  }));
  g.ends = g.ends.map((e, i) => ({
    ...e,
    official_number: number(`end-${i}-official`),
    state: get(`end-${i}-state`),
    red_points: number(`end-${i}-red`),
    yellow_points: number(`end-${i}-yellow`),
    acceptance: get(`end-${i}-acceptance`),
    evidence_ids: get(`end-${i}-evidence`)
      ? [
          get(`end-${i}-evidence`),
          ...e.evidence_ids
            .slice(1)
            .filter((id) => id !== get(`end-${i}-evidence`)),
        ]
      : [],
  }));
  g.result = {
    outcome: get("result-outcome"),
    acceptance: get("result-acceptance"),
    red_points: number("result-red"),
    yellow_points: number("result-yellow"),
    evidence_ids: get("result-evidence")
      ? [
          get("result-evidence"),
          ...g.result.evidence_ids
            .slice(1)
            .filter((id) => id !== get("result-evidence")),
        ]
      : [],
  };
  state.audit = { author: get("author"), reason: get("reason") };
  return state.audit;
}
async function queueFrame() {
  const src = source();
  const start = Math.min(
    state.seconds,
    (src.duration_seconds || Infinity) - 0.1,
  );
  await api("/scans", "POST", {
    source_id: state.source,
    start_seconds: start,
    end_seconds: Math.min(start + 1, src.duration_seconds || start + 1),
    interval_seconds: 1,
    max_frames: 2,
    max_evidence_mb: 20,
    acquisition_tier: "evidence",
  });
  toast("Frame queued. Its original PTS and YouTube link will be retained.");
  await refresh();
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  try {
    if (button.dataset.source) {
      if (window.hoglineLabels && !window.hoglineLabels.beforeNavigate())
        return;
      state.source = button.dataset.source;
      state.seconds = frames()[0]?.source_seconds || 0;
      state.draft = null;
      $("#game-editor").hidden = true;
      render();
    }
    if (button.dataset.sheet) {
      state.sheet =
        state.sheet === Number(button.dataset.sheet)
          ? null
          : Number(button.dataset.sheet);
      render();
    }
    if (button.dataset.view) {
      if (window.hoglineLabels && !window.hoglineLabels.beforeNavigate())
        return;
      state.view = button.dataset.view;
      renderFrame();
    }
    if (button.dataset.edit) {
      if (window.hoglineLabels && !window.hoglineLabels.beforeNavigate())
        return;
      editGame(button.dataset.edit);
    }
    if (button.dataset.scoreProposal)
      useScoreProposal(button.dataset.scoreProposal);
    if (button.dataset.activity) {
      if (window.hoglineLabels && !window.hoglineLabels.beforeNavigate())
        return;
      newGame(
        sourceTimeline().activity_runs.find(
          (r) => r.id === button.dataset.activity,
        ),
      );
    }
    if (button.dataset.retry) {
      await api(`/jobs/${button.dataset.retry}/retry`, "POST");
      await refresh();
    }
    if (button.matches(".close-dialog")) button.closest("dialog").close();
    switch (button.id) {
      case "import-button":
      case "empty-import":
        $("#import-dialog").showModal();
        break;
      case "export-button":
        $("#export-dialog").showModal();
        break;
      case "scan-button": {
        const f = $("#scan-form");
        f.elements.start.value = time(state.seconds);
        f.elements.end.value = time(
          Math.min(
            state.seconds + 600,
            source().duration_seconds || state.seconds + 600,
          ),
        );
        $("#scan-dialog").showModal();
        break;
      }
      case "archive-nav":
        state.reviewOnly = false;
        render();
        break;
      case "overlay-toggle":
        state.overlay = !state.overlay;
        renderFrame();
        break;
      case "analyze-cached":
        await api("/perception/analyze", "POST");
        await refresh();
        toast(
          "OpenCV observations added from cached evidence. No video fetched.",
        );
        break;
      case "read-broadcast":
        button.disabled = true;
        try {
          const pass = await api("/perception/broadcast", "POST", {
            source_id: state.source,
          });
          await refresh();
          toast(
            `${pass.processed_frames} frames read. ${pass.remaining_frames ? `${pass.remaining_frames} pending; run again to continue this bounded pass.` : "All retained detailed frames are up to date."}`,
          );
        } finally {
          button.disabled = false;
        }
        break;
      case "review-nav":
        state.reviewOnly = true;
        render();
        break;
      case "cache-frame":
        button.disabled = true;
        try {
          await queueFrame();
        } finally {
          button.disabled = false;
        }
        break;
      case "add-game":
        newGame();
        break;
      case "cancel-edit":
        state.draft = null;
        $("#game-editor").hidden = true;
        break;
      case "add-span":
        readEditor();
        state.draft.game.spans.push({
          source_id: state.source,
          start_seconds: 0,
          end_seconds: Math.min(600, source().duration_seconds || 600),
          relationship: "continuation",
          boundary_uncertainty_seconds: null,
        });
        renderEditor();
        break;
      case "add-end": {
        readEditor();
        const g = state.draft.game;
        const index = Math.max(0, ...g.ends.map((e) => e.observed_index)) + 1;
        g.ends.push({
          id: crypto.randomUUID(),
          observed_index: index,
          official_number: null,
          state: "unknown",
          red_points: null,
          yellow_points: null,
          acceptance: "unknown",
          evidence_ids: [],
          note: "",
        });
        renderEditor();
        break;
      }
      case "history-button": {
        if (!state.draft.revision) {
          toast("Save the first revision to begin its history.");
          break;
        }
        const rows = await api(`/games/${state.draft.game.id}/revisions`);
        $("#revision-history").innerHTML = rows
          .map(
            (r) =>
              `<p class="revision-list">r${r.revision} · ${escapeHTML(r.author)} · ${escapeHTML(r.created_at)}<br>${escapeHTML(r.reason)}</p>`,
          )
          .join("");
        break;
      }
    }
    if (button.dataset.removeSpan !== undefined) {
      readEditor();
      state.draft.game.spans.splice(Number(button.dataset.removeSpan), 1);
      renderEditor();
    }
    if (button.dataset.removeEnd !== undefined) {
      readEditor();
      state.draft.game.ends.splice(Number(button.dataset.removeEnd), 1);
      renderEditor();
    }
  } catch (error) {
    toast(error.message, true);
  }
});
$("#search").addEventListener("input", render);
$("#scrubber").addEventListener("input", (event) => {
  if (window.hoglineLabels && !window.hoglineLabels.beforeNavigate()) {
    event.target.value = state.seconds;
    return;
  }
  state.seconds = Number(event.target.value);
  $("#timeline-duration").textContent =
    `${time(state.seconds)} / ${time(source().duration_seconds)}`;
  renderFrame();
});
document.addEventListener("submit", async (event) => {
  if (
    [
      "plan-form",
      "batch-resume-form",
      "discovery-form",
      "window-form",
      "adaptive-form",
    ].includes(event.target.id)
  )
    return;
  event.preventDefault();
  const form = event.target,
    submit = $('button[type="submit"]', form);
  if (submit) submit.disabled = true;
  try {
    const data = new FormData(form);
    if (form.id === "jump-form") {
      if (window.hoglineLabels && !window.hoglineLabels.beforeNavigate())
        return;
      const seconds = parseTime(data.get("seconds"));
      if (
        !Number.isFinite(seconds) ||
        seconds < 0 ||
        seconds > (source().duration_seconds ?? Infinity)
      )
        throw Error("Choose a time inside this recording.");
      state.seconds = seconds;
      render();
    }
    if (form.id === "import-form") {
      toast("Fetching metadata only…");
      const result = await api("/sources", "POST", { url: data.get("url") });
      state.source = result.id;
      state.seconds = 0;
      form.closest("dialog").close();
      form.reset();
      toast("Recording added. No video downloaded.");
      await refresh();
    }
    if (form.id === "scan-form") {
      await api("/scans", "POST", {
        source_id: state.source,
        start_seconds: parseTime(data.get("start")),
        end_seconds: parseTime(data.get("end")),
        interval_seconds: Number(data.get("interval")),
        max_frames: 120,
        max_evidence_mb: Number(data.get("budget")),
        acquisition_tier: data.get("tier"),
      });
      form.closest("dialog").close();
      toast("Section queued. Existing matching work is reused.");
      await refresh();
    }
    if (form.id === "edit-form") {
      const audit = readEditor();
      await api(`/games/${state.draft.game.id}`, "PUT", {
        expected_revision: state.draft.revision,
        ...audit,
        game: state.draft.game,
      });
      localStorage.setItem("hogline-reviewer", audit.author);
      state.draft = null;
      $("#game-editor").hidden = true;
      toast(
        "Revision saved. Its evidence and previous interpretation are preserved.",
      );
      await refresh();
    }
    if (form.id === "export-form") {
      const release = await api("/releases", "POST", {
        mode: data.get("mode"),
      });
      const a = document.createElement("a");
      a.href = `/api/releases/${release.id}.${data.get("format")}`;
      a.download = "";
      a.click();
      form.closest("dialog").close();
      toast(
        `Snapshot created · ${release.coverage.exported_games} game intervals · ${release.coverage.confirmed_results} confirmed results.`,
      );
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (submit) submit.disabled = false;
  }
});
refresh().catch((error) => toast(error.message, true));
setInterval(() => {
  if (!document.hidden) refresh().catch((error) => toast(error.message, true));
}, 4000);
