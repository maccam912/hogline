(() => {
  let preview = null,
    original = null;
  const form = () => $("#adaptive-form");
  function invalidate() {
    preview = null;
    $("#start-adaptive").disabled = true;
    $("#adaptive-preview").textContent =
      "Preview the current settings before starting.";
  }
  function open(request = null) {
    original = request;
    const f = form();
    f.dataset.sourceId = request?.source_ids[0] || state.source;
    f.elements.scope.options[1].textContent =
      request?.source_ids.length > 1
        ? `Previous selection (${request.source_ids.length} recordings)`
        : "All imported recordings (up to 16)";
    f.elements.scope.value = request?.source_ids.length > 1 ? "all" : "current";
    f.elements.start.value = time(request?.start_seconds || 0);
    f.elements.end.value = request?.end_seconds
      ? time(request.end_seconds)
      : "";
    for (const [field, value] of Object.entries({
      jobs: request?.max_jobs || 4,
      duration: request?.window_seconds || 10,
      audit: request?.quiet_audit_fraction ?? 0.2,
      growth: request?.max_image_growth_mb || 64,
      worker: request?.max_worker_seconds || 300,
    }))
      f.elements[field].value = value;
    invalidate();
    $("#adaptive-dialog").showModal();
  }
  function read() {
    const f = form(),
      d = new FormData(f);
    const ids =
      d.get("scope") === "all"
        ? original?.source_ids.length > 1
          ? original.source_ids
          : state.data.sources.map((s) => s.id)
        : [f.dataset.sourceId];
    return {
      ...(original || {}),
      source_ids: ids,
      start_seconds: parseTime(d.get("start")),
      end_seconds: d.get("end").trim() ? parseTime(d.get("end")) : null,
      window_seconds: Number(d.get("duration")),
      max_jobs: Number(d.get("jobs")),
      quiet_audit_fraction: Number(d.get("audit")),
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
      const result = await api("/adaptive/preview", "POST", request);
      if (JSON.stringify(read()) !== signature) return;
      preview = { request, expected_plan_sha256: result.sha256 };
      $("#adaptive-preview").innerHTML =
        `<h3>${result.admitted_new_units} windows selected · ${result.selected_quiet_audits} quiet audits</h3>
      <p>${result.eligible_sparse_samples} eligible sparse samples · ${result.deferred_units} other candidate windows deferred or overlapping. No media has been queued.</p>
      <p class="fine-print">Shared limits: ${request.max_image_growth_mb} MB retained growth and ${request.max_worker_seconds} worker seconds. Unscanned gaps are not inspected by this plan.</p>
      ${
        Object.keys(result.exclusions).length
          ? `<p class="fine-print">Excluded: ${Object.entries(result.exclusions)
              .map(([k, v]) => `${v} ${escapeHTML(k.replaceAll("_", " "))}`)
              .join(" · ")}</p>`
          : ""
      }
      ${result.units
        .map((u) => {
          const r = u.payload.request,
            src = state.data.sources.find((s) => s.id === r.source_id);
          return `<article class="batch-card"><strong>Sheet ${escapeHTML(src?.sheet ?? "?")} · ${time(r.start_seconds)}–${time(r.end_seconds)}</strong><p>${escapeHTML(u.selection.reason.replaceAll("_", " "))} · ${u.sample_count} samples per view</p><p class="fine-print">${escapeHTML(src?.title || r.source_id)}</p>${u.selection.youtube_url ? `<a class="evidence-link" href="${escapeHTML(u.selection.youtube_url)}" target="_blank" rel="noopener">Check selection evidence on YouTube ↗</a>` : ""}</article>`;
        })
        .join("")}`;
      $("#start-adaptive").disabled = !result.units.length;
    } catch (error) {
      invalidate();
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (button?.id === "adaptive-button") return open();
    if (button?.id !== "start-adaptive" || !preview) return;
    button.disabled = true;
    try {
      await api("/adaptive/batches", "POST", preview);
      $("#adaptive-dialog").close();
      toast(
        "Selected windows queued with shared budgets. Pause them under Processing.",
      );
      await refresh();
    } catch (error) {
      invalidate();
      toast(error.message, true);
    }
  });
  window.hoglineAdaptive = { open };
})();
