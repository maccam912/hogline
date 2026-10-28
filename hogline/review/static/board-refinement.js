(() => {
  let preview = null;
  const form = () => $("#board-refinement-form");
  const stamp = (n) =>
    time(n) +
    (Number.isInteger(n) ? "" : (n % 1).toFixed(3).slice(1).replace(/0+$/, ""));
  function invalidate() {
    preview = null;
    $("#start-refinement").disabled = true;
    $("#refinement-preview").textContent =
      "Preview the selected intervals and shared limits before starting.";
  }
  function open(request) {
    const change = state.data.broadcast?.board_timeline?.changes.find(
      (c) => c.id === request.change_id,
    );
    if (!change)
      return toast(
        "That board interval changed after more evidence was read. Choose a current interval under Posted board changes.",
        true,
      );
    const f = form();
    f.dataset.change = change.id;
    for (const [name, value] of Object.entries({
      windows: request.max_windows ?? 3,
      rounds: request.max_rounds ?? 3,
      target: request.target_bracket_seconds ?? 60,
      duration: request.window_seconds ?? 10,
      growth: request.max_image_growth_mb ?? 96,
      worker: request.max_worker_seconds ?? 300,
    }))
      f.elements[name].value = value;
    $("#refinement-context").textContent =
      `${change.color} row · ${stamp(change.before.source_seconds)}–${stamp(change.after.source_seconds)}. Inspect short windows inside the largest remaining gaps, then read their retained boards and clocks.`;
    invalidate();
    $("#board-change-dialog").close();
    $("#board-refinement-dialog").showModal();
  }
  function read() {
    const f = form(),
      d = new FormData(f);
    return {
      change_id: f.dataset.change,
      max_windows: Number(d.get("windows")),
      max_rounds: Number(d.get("rounds")),
      target_bracket_seconds: Number(d.get("target")),
      window_seconds: Number(d.get("duration")),
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
      const plan = await api("/board-refinement/preview", "POST", request);
      if (JSON.stringify(read()) !== signature) return;
      preview = { request, expected_plan_sha256: plan.sha256 };
      $("#refinement-preview").innerHTML =
        `<h3>First round: ${plan.admitted_new_units} new short window${plan.admitted_new_units === 1 ? "" : "s"} · ${plan.reused_succeeded_units} reused for reading</h3>
        <p>${escapeHTML(plan.notice)}</p><p>Up to ${request.max_rounds} round${request.max_rounds === 1 ? "" : "s"} of ${request.max_windows} window${request.max_windows === 1 ? "" : "s"} (${plan.maximum_total_windows} total), aiming for gaps of ${request.target_bracket_seconds} seconds or less in the original row and interval.</p><p><strong>Decode and OCR share ${request.max_image_growth_mb} MB and ${request.max_worker_seconds} worker seconds.</strong></p>
        ${plan.units
          .map((u) => {
            const r = u.payload.request;
            return `<article class="batch-card"><strong>${stamp(r.start_seconds)}–${stamp(r.end_seconds)}</strong><p class="fine-print">${escapeHTML(u.selection.reason.replaceAll("_", " "))}</p><a class="evidence-link" href="${escapeHTML(u.selection.youtube_url)}" target="_blank" rel="noopener">Check this source interval ↗</a></article>`;
          })
          .join("")}
        <p class="fine-print">${plan.excluded_intervals.length} existing intervals excluded from another decode · ${plan.unselected_gap_intervals.length} gaps remain outside these windows.${plan.pending_reuse_windows_deferred ? ` ${plan.pending_reuse_windows_deferred} windows with unread frames deferred.` : ""}</p>
        ${!plan.units.length ? '<p class="job-error">No new windows fit. Try a shorter window or inspect the existing work.</p>' : ""}`;
      $("#start-refinement").disabled = !plan.units.length;
    } catch (error) {
      invalidate();
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (button?.dataset.refineBoard)
      return open({ change_id: button.dataset.refineBoard });
    if (button?.id !== "start-refinement" || !preview) return;
    button.disabled = true;
    try {
      await api("/board-refinement/batches", "POST", preview);
      $("#board-refinement-dialog").close();
      toast(
        "Gap refinement queued. The board timeline will update after its retained frames are read.",
      );
      await refresh();
    } catch (error) {
      invalidate();
      toast(error.message, true);
    }
  });
  window.hoglineRefinement = { open };
})();
