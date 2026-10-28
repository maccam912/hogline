(() => {
  let annotation = null,
    benchmark = null;
  const labelForm = () => $("#house-label-form");
  const stamp = (n) =>
    time(n) +
    (Number.isInteger(n) ? "" : (n % 1).toFixed(3).slice(1).replace(/0+$/, ""));
  const human = (s) => escapeHTML(s.replaceAll("_", " "));
  const percentage = (n) =>
    n == null ? "unavailable" : `${(100 * n).toFixed(1)}%`;
  const sourceLink = (sample, title) => {
    const src = state.data.sources.find((s) => s.id === annotation?.source_id);
    const url =
      sample.youtube_url ||
      (src?.platform === "local"
        ? `/api/media/${encodeURIComponent(src.id)}#t=${sample.source_seconds}`
        : null);
    return url
      ? `<a class="evidence-link" href="${escapeHTML(url)}" target="_blank" rel="noopener">${title} · ${stamp(sample.source_seconds)} ${sample.youtube_url ? "on YouTube" : "in local video"} ↗</a>`
      : "";
  };
  const sampleText = (index) => {
    const sample = annotation?.samples.find(
      (s) => s.sample_index === Number(index),
    );
    return sample ? stamp(sample.source_seconds) : "outside this window";
  };
  function updateRange(row) {
    const kind = $("[data-segment-state]", row).value;
    for (const color of ["red", "yellow"]) {
      const field = $(`[data-segment-${color}]`, row);
      field.disabled = kind !== "stable_handles";
      field.required = kind === "stable_handles";
      if (kind === "stable_no_handles") field.value = 0;
      else if (kind !== "stable_handles") field.value = "";
    }
    $("[data-segment-times]", row).textContent =
      `${sampleText($("[data-segment-first]", row).value)}–${sampleText($("[data-segment-last]", row).value)}`;
  }
  function addSegment(segment = {}) {
    const row = document.createElement("div");
    row.className = "batch-card house-segment";
    const last = annotation.samples.at(-1).sample_index;
    row.innerHTML = `<div class="form-grid"><label>First sample<input type="number" data-segment-first min="0" max="${last}" value="${segment.first_sample ?? 0}" required step="1"></label><label>Last sample<input type="number" data-segment-last min="0" max="${last}" value="${segment.last_sample ?? last}" required step="1"></label><label>Observed state<select data-segment-state><option value="unknown">Unknown</option><option value="stable_handles">Stable visible handles</option><option value="stable_no_handles">Stable, no visible handles</option><option value="changing">Changing</option><option value="unobservable">Unobservable / obstructed</option></select></label><label>Red visible handles<input type="number" data-segment-red min="0" max="128" step="1" value="${segment.red_handles ?? ""}"></label><label>Yellow visible handles<input type="number" data-segment-yellow min="0" max="128" step="1" value="${segment.yellow_handles ?? ""}"></label></div><p class="fine-print" data-segment-times></p><button type="button" class="button quiet" data-remove-house-segment>Remove range</button>`;
    $("[data-segment-state]", row).value = segment.state || "unknown";
    $("#house-label-segments").appendChild(row);
    updateRange(row);
  }
  async function openLabels(windowId, view) {
    $("#house-label-status").textContent =
      "Loading original sample references…";
    $("#house-label-form").hidden = true;
    if (!$("#house-label-dialog").open) $("#house-label-dialog").showModal();
    try {
      annotation = await api(
        `/house-labels/${encodeURIComponent(windowId)}/${view}`,
      );
      const saved = annotation.labels;
      const src = state.data.sources.find((s) => s.id === annotation.source_id);
      $("#house-label-context").textContent =
        `${src?.title || "Recording"} · ${view === "house_top" ? "Top" : "Bottom"} overhead · ${annotation.samples.length} original samples`;
      $("#house-label-source").innerHTML =
        `<div class="batch-actions">${sourceLink(annotation.samples[0], "Source start")}${sourceLink(annotation.samples.at(-1), "Source end")}</div>`;
      labelForm().elements.author.value = annotation.history[0]?.author || "";
      labelForm().elements.group.value = saved?.evaluation_group || "";
      labelForm().elements.provenance.value = saved?.provenance || "human";
      labelForm().elements.basis.value =
        saved?.inspection_basis || "retained_frames_only";
      labelForm().elements.status.value = saved?.review_status || "draft";
      labelForm().elements.note.value = saved?.note || "";
      labelForm().elements.reason.value = "";
      $("#house-label-segments").innerHTML = "";
      for (const segment of saved?.segments || [{}]) addSegment(segment);
      $("#house-label-samples").innerHTML =
        `<ol class="track-samples">${annotation.samples.map((s) => `<li>Sample ${s.sample_index} · ${stamp(s.source_seconds)} · PTS ${s.pts} × ${s.time_base_num}/${s.time_base_den}${s.youtube_url ? ` · <a href="${escapeHTML(s.youtube_url)}" target="_blank" rel="noopener">YouTube ↗</a>` : ""}</li>`).join("")}</ol>`;
      $("#house-label-frames").innerHTML =
        annotation.context_anchor_evidence_ids
          .map((id) => state.data.evidence.find((e) => e.id === id))
          .filter(Boolean)
          .sort((a, b) => a.source_seconds - b.source_seconds)
          .map(
            (e) =>
              `<figure class="boundary-frame"><figcaption>Context only · ${stamp(e.source_seconds)}</figcaption><img loading="lazy" src="/api/evidence/${encodeURIComponent(e.id)}/${view}" width="${e.crops[view].width}" height="${e.crops[view].height}" alt="${human(view)} context at ${stamp(e.source_seconds)}"></figure>`,
          )
          .join("");
      $("#house-label-history").innerHTML =
        annotation.history
          .map(
            (h) =>
              `<p><strong>Revision ${h.revision}</strong> · ${escapeHTML(h.author)}<br>${escapeHTML(h.reason)}<br><span class="fine-print">${escapeHTML(h.created_at)}</span></p>`,
          )
          .join("") || "<p>No saved revisions.</p>";
      $("#house-label-status").textContent = saved
        ? `Loaded revision ${saved.revision}. Saving keeps the earlier revision in history.`
        : "No labels yet. Start from your source inspection.";
      labelForm().hidden = false;
    } catch (error) {
      annotation = null;
      $("#house-label-context").textContent = error.message;
      $("#house-label-status").textContent = error.message;
    }
  }
  labelForm().addEventListener("input", (event) => {
    const row = event.target.closest(".house-segment");
    if (row) updateRange(row);
  });
  labelForm().addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!annotation) return;
    const button = event.submitter;
    button.disabled = true;
    try {
      const values = new FormData(labelForm());
      const segments = $$(".house-segment", labelForm()).map((row) => {
        const kind = $("[data-segment-state]", row).value;
        const count = (color) =>
          kind === "stable_no_handles"
            ? 0
            : kind !== "stable_handles"
              ? null
              : $(`[data-segment-${color}]`, row).value === ""
                ? null
                : Number($(`[data-segment-${color}]`, row).value);
        return {
          first_sample: Number($("[data-segment-first]", row).value),
          last_sample: Number($("[data-segment-last]", row).value),
          state: kind,
          red_handles: count("red"),
          yellow_handles: count("yellow"),
        };
      });
      await api(
        `/house-labels/${encodeURIComponent(annotation.window_id)}/${annotation.view}`,
        "PUT",
        {
          expected_revision: annotation.labels?.revision || 0,
          author: values.get("author"),
          reason: values.get("reason"),
          evaluation_group: values.get("group"),
          provenance: values.get("provenance"),
          inspection_basis: values.get("basis"),
          review_status: values.get("status"),
          note: values.get("note"),
          segments,
        },
      );
      await openLabels(annotation.window_id, annotation.view);
      $("#house-label-status").textContent =
        "Interval labels saved with exact original sample references.";
    } catch (error) {
      $("#house-label-status").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
  async function loadBenchmark() {
    const evaluateForm = $("#house-evaluate-form");
    const previousDataset = evaluateForm.elements.dataset.value;
    const previousMethod = evaluateForm.elements.method.value;
    const previousGroups = new Map(
      $$("[data-house-group]").map((s) => [s.dataset.houseGroup, s.value]),
    );
    benchmark = await api("/house-evaluation");
    const groups = [
      ...new Set(benchmark.labels.map((l) => l.evaluation_group)),
    ].sort();
    $("#house-benchmark-labels").innerHTML = benchmark.labels.length
      ? benchmark.labels
          .map((l) => {
            const w = state.data.analysis_windows.find(
              (w) => w.id === l.window_id,
            );
            const src = state.data.sources.find((s) => s.id === l.source_id);
            return `<article class="batch-card"><strong>${escapeHTML(src?.title || "Recording")} · ${l.view === "house_top" ? "Top" : "Bottom"} overhead</strong><p>${w ? `${stamp(w.observed_first_seconds)}–${stamp(w.observed_last_seconds)} · ` : ""}revision ${l.revision} · ${human(l.provenance)} · ${human(l.review_status)}<br>${human(l.inspection_basis)} · ${escapeHTML(l.evaluation_group)} · ${l.segments.length} ranges</p><button class="button quiet" data-house-label-window="${escapeHTML(l.window_id)}" data-house-label-view="${l.view}">Inspect interval labels →</button></article>`;
          })
          .join("")
      : "<p>No interval labels yet. Choose Label top or Label bottom on a detailed window.</p>";
    $("#house-group-partitions").innerHTML = groups
      .map(
        (g) =>
          `<label>${escapeHTML(g)}<select data-house-group="${escapeHTML(g)}"><option value="">Exclude from this cohort</option><option value="train">Training / development</option><option value="validation">Validation</option><option value="test">Test</option></select></label>`,
      )
      .join("");
    $("#house-evaluate-form").elements.dataset.innerHTML = benchmark.datasets
      .map(
        (d) =>
          `<option value="${escapeHTML(d.id)}">${escapeHTML(d.name)} · ${d.mode} · ${d.record_count} views</option>`,
      )
      .join("");
    if (benchmark.datasets.some((d) => d.id === previousDataset))
      evaluateForm.elements.dataset.value = previousDataset;
    evaluateForm.elements.method.value = previousMethod || benchmark.version;
    $("button[type=submit]", evaluateForm).disabled =
      !benchmark.datasets.length;
    $("#house-freeze-form button[type=submit]").disabled = !groups.length;
    for (const select of $$("[data-house-group]"))
      select.value = previousGroups.get(select.dataset.houseGroup) || "";
    $("#house-benchmark-datasets").innerHTML =
      benchmark.datasets
        .map(
          (d) =>
            `<p><a href="/api/house-evaluation/datasets/${encodeURIComponent(d.id)}.json" target="_blank" rel="noopener">${escapeHTML(d.name)} · ${d.record_count} views · ${d.mode} · JSON ↗</a></p>`,
        )
        .join("") || "<p>No frozen cohorts.</p>";
    $("#house-benchmark-runs").innerHTML =
      benchmark.runs
        .map(
          (r) =>
            `<article class="batch-card"><strong>${escapeHTML(r.model_version)} · ${r.partition}</strong><p>${r.summary.evaluated_samples} evaluated samples · ${r.summary.exact_stable_samples} exact stable · ${r.summary.incorrect_stable_samples} incorrect stable · ${r.summary.abstained_samples} abstained</p><p class="fine-print">Stable prediction precision ${percentage(r.summary.stable_prediction_precision)} · stable sample recall ${percentage(r.summary.stable_sample_recall)} · coverage ${percentage(r.summary.prediction_coverage)}<br>${r.summary.unlabeled_or_unknown_samples} unknown/unlabeled · ${r.summary.duplicate_samples_excluded} overlapping samples excluded</p><a href="/api/house-evaluation/runs/${encodeURIComponent(r.id)}.json" target="_blank" rel="noopener">Full report, confusion matrix and provenance · JSON ↗</a></article>`,
        )
        .join("") || "<p>No evaluation reports.</p>";
  }
  $("#house-freeze-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    event.submitter.disabled = true;
    try {
      const values = new FormData(event.target);
      const partitions = Object.fromEntries(
        $$("[data-house-group]")
          .filter((s) => s.value)
          .map((s) => [s.dataset.houseGroup, s.value]),
      );
      const result = await api("/house-evaluation/datasets", "POST", {
        name: values.get("name"),
        author: values.get("author"),
        reason: values.get("reason"),
        mode: values.get("mode"),
        partitions,
      });
      await loadBenchmark();
      $("#house-evaluate-form").elements.dataset.value = result.id;
      $("#house-benchmark-status").textContent =
        `Frozen ${result.records.length} exact window/view label revisions.`;
    } catch (error) {
      $("#house-benchmark-status").textContent = error.message;
    } finally {
      event.submitter.disabled = false;
    }
  });
  $("#house-evaluate-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    event.submitter.disabled = true;
    try {
      const values = new FormData(event.target);
      const result = await api("/house-evaluation/runs", "POST", {
        dataset_id: values.get("dataset"),
        partition: values.get("partition"),
        model_version: values.get("method"),
      });
      await loadBenchmark();
      $("#house-benchmark-status").textContent =
        `Saved report for ${result.summary.evaluated_samples} unique labeled samples. Adjacent frames remain correlated.`;
    } catch (error) {
      $("#house-benchmark-status").textContent = error.message;
    } finally {
      event.submitter.disabled = false;
    }
  });
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.houseLabelWindow)
      await openLabels(
        button.dataset.houseLabelWindow,
        button.dataset.houseLabelView,
      );
    if (button.hasAttribute("data-remove-house-segment"))
      button.closest(".house-segment").remove();
    if (button.id === "house-add-segment" && annotation) addSegment();
    if (button.id === "house-benchmark-button") {
      $("#house-benchmark-dialog").showModal();
      $("#house-benchmark-status").textContent =
        "Loading saved labels and frozen reports…";
      try {
        await loadBenchmark();
        $("#house-benchmark-status").textContent = "";
      } catch (error) {
        $("#house-benchmark-status").textContent = error.message;
      }
    }
  });
  $("#house-label-dialog").addEventListener("close", async () => {
    if (!$("#house-benchmark-dialog").open) return;
    try {
      await loadBenchmark();
    } catch (error) {
      $("#house-benchmark-status").textContent = error.message;
    }
  });
})();
