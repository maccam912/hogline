(() => {
  let preview = null;
  const form = () => $("#plan-form");
  const invalidate = () => {
    preview = null;
    $("#start-batch").disabled = true;
    $("#plan-preview").textContent =
      "Preview the current settings before starting.";
  };
  function open(request = null) {
    const f = form(),
      src = source();
    const defaults = request || {
      start_seconds: 0,
      end_seconds: src.duration_seconds || state.seconds + 600,
      chunk_seconds: 600,
      context_seconds: 30,
      interval_seconds: 10,
      max_jobs: 2,
      acquisition_tier: "coarse",
      max_image_growth_mb: 250,
      max_worker_seconds: 1800,
    };
    f.dataset.sourceId = request?.source_ids[0] || src.id;
    f.elements.start.value = time(defaults.start_seconds);
    f.elements.end.value = time(defaults.end_seconds ?? src.duration_seconds);
    for (const [field, key] of Object.entries({
      chunk: "chunk_seconds",
      context: "context_seconds",
      interval: "interval_seconds",
      jobs: "max_jobs",
      tier: "acquisition_tier",
      growth: "max_image_growth_mb",
      worker: "max_worker_seconds",
    }))
      f.elements[field].value = defaults[key];
    invalidate();
    $("#plan-dialog").showModal();
  }
  function read() {
    const f = form(),
      data = new FormData(f);
    return {
      source_ids: [f.dataset.sourceId],
      start_seconds: parseTime(data.get("start")),
      end_seconds: parseTime(data.get("end")),
      chunk_seconds: Number(data.get("chunk")),
      context_seconds: Number(data.get("context")),
      interval_seconds: Number(data.get("interval")),
      max_jobs: Number(data.get("jobs")),
      acquisition_tier: data.get("tier"),
      max_image_growth_mb: Number(data.get("growth")),
      max_worker_seconds: Number(data.get("worker")),
    };
  }
  function render() {
    $("#batch-list").innerHTML = (state.data.batches || [])
      .filter((b) => b.request.source_ids.includes(state.source))
      .map((b) => {
        const mb = b.reserved_image_bytes / 1024 ** 2;
        return `<article class="batch-card"><div class="batch-heading"><strong>Recording batch</strong><span class="pill ${b.state === "paused" ? "amber" : ""}">${escapeHTML(b.state)}</span></div>
        <p>${b.succeeded_units} / ${b.units.length} chunks complete · ${b.deferred_units} deferred to a later plan</p>
        <p class="fine-print">${mb.toFixed(2)} / ${b.limits.max_image_growth_mb} MB reserved · ${Math.ceil(b.spent_worker_seconds)} / ${b.limits.max_worker_seconds} worker seconds · ${b.executions.length} attempts</p>
        ${b.pause_reason ? `<p class="job-error">${escapeHTML(b.pause_reason)}</p>` : ""}
        <div class="batch-actions">${b.state === "active" ? `<button class="button quiet" data-batch-action="pause" data-batch="${b.id}">Pause</button>` : ""}
        ${b.state === "paused" ? `<button class="button secondary" data-batch-action="resume" data-batch="${b.id}">Resume / budgets →</button>` : ""}
        ${["active", "paused"].includes(b.state) ? `<button class="button quiet" data-batch-action="cancel" data-batch="${b.id}">Cancel remaining</button>` : `<button class="button quiet" data-next-plan="${b.id}">Preview remaining →</button>`}</div>
        <details><summary>Chunk status and source spans</summary>${b.units.map((u) => `<p class="fine-print">${time(u.request.ownership_start_seconds)}–${time(u.request.ownership_end_seconds)} · ${escapeHTML(u.state)} · ${u.disposition === "reuse_succeeded" ? "previously completed" : escapeHTML(u.request.acquisition_tier)}</p>`).join("")}</details></article>`;
      })
      .join("");
  }
  form().addEventListener("input", invalidate);
  form().addEventListener("change", invalidate);
  form().addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    try {
      const request = read(),
        signature = JSON.stringify(request);
      const result = await api("/plans/preview", "POST", request);
      if (JSON.stringify(read()) !== signature) return;
      preview = { request, expected_plan_sha256: result.sha256 };
      const blocked = result.units.some(
        (u) => u.disposition === "existing_needs_attention",
      );
      $("#plan-preview").innerHTML =
        `<h3>${result.admitted_new_units} chunks to queue · ${result.reused_succeeded_units} already complete</h3>
      <p>${result.total_requested_units} chunks cover the requested range; ${result.deferred_units} remain outside this batch.</p>
      <p>${result.estimated_new_sample_requests} sample requests · roughly ${result.image_growth_estimate_mb} MB before cache reuse. Actual image growth is capped at ${request.max_image_growth_mb} MB.</p>
      <p class="fine-print">${escapeHTML(result.estimate_basis)}. Sparse samples can miss short events. Nothing has been queued.</p>
      ${blocked ? '<p class="job-error">Matching unfinished work exists. Resume or retry its batch before creating this plan.</p>' : ""}
      <details><summary>Preview chunk ownership</summary>${result.units
        .map((u) => {
          const r = u.payload.request;
          return `<p class="fine-print">${time(r.ownership_start_seconds)}–${time(r.ownership_end_seconds)} · context ${time(r.start_seconds)}–${time(r.end_seconds)} · ${u.sample_count} frames · ${escapeHTML(u.disposition.replaceAll("_", " "))}</p>`;
        })
        .join("")}</details>`;
      $("#start-batch").disabled = blocked || result.admitted_new_units === 0;
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.id === "plan-button") return open();
    if (button.dataset.nextPlan)
      return open(
        state.data.batches.find((b) => b.id === button.dataset.nextPlan)
          .request,
      );
    if (button.id === "start-batch" && preview) {
      button.disabled = true;
      try {
        await api("/batches", "POST", preview);
        $("#plan-dialog").close();
        toast("Batch queued with retained-image and worker-time limits.");
        await refresh();
      } catch (error) {
        invalidate();
        toast(error.message, true);
      }
      return;
    }
    if (!button.dataset.batchAction) return;
    const batch = state.data.batches.find((b) => b.id === button.dataset.batch);
    if (button.dataset.batchAction === "resume") {
      const f = $("#batch-resume-form");
      f.dataset.batch = batch.id;
      f.elements.growth.value = batch.limits.max_image_growth_mb;
      f.elements.worker.value = batch.limits.max_worker_seconds;
      $("#batch-resume-dialog").showModal();
      return;
    }
    button.disabled = true;
    try {
      await api(`/batches/${batch.id}/control`, "POST", {
        action: button.dataset.batchAction,
      });
      await refresh();
    } catch (error) {
      button.disabled = false;
      toast(error.message, true);
    }
  });
  $("#batch-resume-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const f = event.target,
      data = new FormData(f),
      button = event.submitter;
    button.disabled = true;
    try {
      await api(`/batches/${f.dataset.batch}/control`, "POST", {
        action: "resume",
        max_image_growth_mb: Number(data.get("growth")),
        max_worker_seconds: Number(data.get("worker")),
      });
      $("#batch-resume-dialog").close();
      await refresh();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  window.hoglineProcessing = { render };
})();
