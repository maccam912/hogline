(() => {
  let preview = null,
    previous = null;
  const form = () => $("#automatic-form");
  function invalidate() {
    preview = null;
    $("#start-automatic").disabled = true;
    $("#automatic-preview").textContent =
      "Preview the current limits before starting.";
  }
  function open(request = null) {
    previous = request;
    const f = form();
    f.dataset.sourceId = request?.source_ids[0] || state.source;
    f.elements.scope.options[1].textContent =
      request?.source_ids.length > 1
        ? `Previous selection (${request.source_ids.length} recordings)`
        : "All imported recordings (up to 16)";
    f.elements.scope.value = request?.source_ids.length > 1 ? "all" : "current";
    f.elements.start.value = time(request?.start_seconds ?? state.seconds ?? 0);
    f.elements.end.value = time(
      request?.end_seconds ??
        Math.min(
          (state.seconds || 0) + 600,
          source().duration_seconds || Infinity,
        ),
    );
    for (const [name, value] of Object.entries({
      scans: request?.scan_jobs ?? 2,
      interval: request?.scan_interval_seconds ?? 30,
      details: request?.detail_jobs ?? 4,
      refinements: request?.delivery_refinement_jobs ?? 2,
      duration: request?.window_seconds ?? 10,
      ocr: request?.ocr_frames ?? 24,
      growth: request?.max_image_growth_mb ?? 128,
      worker: request?.max_worker_seconds ?? 600,
    }))
      f.elements[name].value = value;
    invalidate();
    $("#automatic-dialog").showModal();
  }
  function read() {
    const f = form(),
      d = new FormData(f);
    return {
      source_ids:
        d.get("scope") === "all"
          ? previous?.source_ids.length > 1
            ? previous.source_ids
            : state.data.sources.map((s) => s.id)
          : [f.dataset.sourceId],
      start_seconds: parseTime(d.get("start")),
      end_seconds: parseTime(d.get("end")),
      scan_jobs: Number(d.get("scans")),
      scan_interval_seconds: Number(d.get("interval")),
      detail_jobs: Number(d.get("details")),
      delivery_refinement_jobs: Number(d.get("refinements")),
      window_seconds: Number(d.get("duration")),
      ocr_frames: Number(d.get("ocr")),
      max_image_growth_mb: Number(d.get("growth")),
      max_worker_seconds: Number(d.get("worker")),
    };
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
      const plan = await api("/automatic/preview", "POST", request);
      if (JSON.stringify(read()) !== signature) return;
      preview = { request, expected_plan_sha256: plan.sha256 };
      const blocked = plan.units.some(
        (u) => u.disposition === "existing_needs_attention",
      );
      $("#automatic-preview").innerHTML =
        `<h3>Scan → inspect → refine delivery brackets → read boards</h3>
        <p>${plan.admitted_new_units} new scan chunks · ${plan.reused_succeeded_units} reused · ${plan.deferred_units} scan chunks deferred.</p>
        <p>After scanning: up to ${request.detail_jobs} windows of ${request.window_seconds} seconds, with quiet audits, up to ${request.delivery_refinement_jobs} four-second native-cadence delivery refinements when supported; then up to ${request.ocr_frames} pending detailed frames read locally.</p>
        <p><strong>One shared limit: ${request.max_image_growth_mb} MB retained growth and ${request.max_worker_seconds} worker seconds.</strong> Pause or resume under Processing.</p>
        <p class="fine-print">${escapeHTML(plan.notice)} The exact later selections are recorded as each stage starts. A completed campaign means its bounded work finished; unscanned time and deferred frames remain unknown.</p>
        ${blocked ? '<p class="job-error">Matching unfinished scans exist. Resume or retry their batch before starting this campaign.</p>' : ""}
        <details><summary>Initial scan intervals</summary>${plan.units
          .map((u) => {
            const r = u.payload.request,
              src = state.data.sources.find((s) => s.id === r.source_id);
            return `<p class="fine-print"><strong>${escapeHTML(src?.title || r.source_id)}</strong><br>${time(r.start_seconds)}–${time(r.end_seconds)} · ${u.sample_count} sparse samples · ${escapeHTML(u.disposition.replaceAll("_", " "))}</p>`;
          })
          .join("")}</details>`;
      $("#start-automatic").disabled = blocked;
    } catch (error) {
      invalidate();
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (button?.id === "automatic-button") return open();
    if (button?.id !== "start-automatic" || !preview) return;
    button.disabled = true;
    try {
      await api("/automatic/batches", "POST", preview);
      $("#automatic-dialog").close();
      toast("Automatic campaign queued. All three stages share your limits.");
      await refresh();
    } catch (error) {
      invalidate();
      toast(error.message, true);
    }
  });
  window.hoglineAutomatic = { open };
})();
