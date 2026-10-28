(() => {
  let current = [],
    loading = false;
  const status = (text) => escapeHTML(text.replaceAll("_", " "));
  async function load() {
    if (loading) return;
    loading = true;
    try {
      current = (await api("/discovery")).feeds;
      const expanded = new Set(
        $$("#discovery-feeds details[open]").map((d) => d.dataset.feedRuns),
      );
      $("#discovery-feeds").innerHTML =
        current
          .map(
            (f) => `<article class="batch-card discovery-card">
      <div class="batch-heading"><strong>${f.discovered_sources} recordings discovered</strong><span class="pill">${status(f.backfill_state)}</span></div>
      <p><a class="evidence-link" href="${escapeHTML(f.url)}" target="_blank" rel="noopener">Channel / playlist ↗</a> · next listing position ${f.next_index} · pass ${f.generation}</p>
      <p class="fine-print">${f.config.page_size} entries/page, ${f.config.overlap} shared · scheduled ${f.config.scheduled ? `every ${f.config.reconcile_hours}h while running` : "off"}. Discovery is separate from media processing.</p>
      ${f.pause_reason ? `<p class="job-error">${escapeHTML(f.pause_reason)}</p>` : ""}
      <div class="batch-actions">${f.backfill_state === "ready" ? `<button class="button secondary" data-feed="${f.id}" data-feed-action="next">Next page →</button>` : ""}
      ${f.backfill_state !== "paused" ? `<button class="button quiet" data-feed="${f.id}" data-feed-action="head">Check latest page</button><button class="button quiet" data-feed="${f.id}" data-feed-action="pause">Pause feed</button>` : `<button class="button quiet" data-feed="${f.id}" data-feed-action="resume">Resume feed</button>`}
      <button class="button quiet" data-feed="${f.id}" data-feed-action="restart">Restart listing pass</button>
      <button class="button quiet" data-feed="${f.id}" data-feed-action="configure">${f.config.scheduled ? "Disable" : "Enable"} schedule</button></div>
      <details data-feed-runs="${f.id}" ${expanded.has(f.id) ? "open" : ""}><summary>${f.runs.length} recent page runs</summary>${f.runs.map((r) => `<p class="fine-print"><a href="/api/discovery/runs/${r.id}" target="_blank" rel="noopener">${r.request.mode === "head" ? "Head check" : `Position ${r.request.start}`} ↗</a> · ${status(r.state)} · ${r.attempts} ${r.attempts === 1 ? "attempt" : "attempts"}${r.source_count != null ? ` · ${r.source_count} listed recordings` : ""}${r.superseded ? " · superseded pass" : ""}${r.error ? `<br><span class="job-error">${escapeHTML(r.error)}</span>` : ""}</p>`).join("")}</details></article>`,
          )
          .join("") ||
        '<p class="fine-print">No discovery feeds yet. Adding a single recording still works independently.</p>';
    } catch (error) {
      toast(error.message, true);
    } finally {
      loading = false;
    }
  }
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.id === "discovery-button") {
      $("#discovery-dialog").showModal();
      await load();
      return;
    }
    if (!button.dataset.feedAction) return;
    button.disabled = true;
    try {
      const body = { action: button.dataset.feedAction };
      if (body.action === "configure")
        body.scheduled = !current.find((f) => f.id === button.dataset.feed)
          .config.scheduled;
      await api(
        `/discovery/feeds/${button.dataset.feed}/control`,
        "POST",
        body,
      );
      await load();
    } catch (error) {
      toast(error.message, true);
      button.disabled = false;
    }
  });
  $("#discovery-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(event.target),
      button = event.submitter;
    button.disabled = true;
    try {
      await api("/discovery/feeds", "POST", {
        url: data.get("url"),
        page_size: Number(data.get("page_size")),
        overlap: Number(data.get("overlap")),
        backfill_pages_per_cycle: Number(data.get("pages")),
        reconcile_hours: Number(data.get("hours")),
        scheduled: data.has("scheduled"),
      });
      toast("Discovery feed registered. Only bounded metadata work is queued.");
      await load();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  setInterval(() => {
    if ($("#discovery-dialog").open && !document.hidden) load();
  }, 4000);
})();
