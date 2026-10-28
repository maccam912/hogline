(() => {
  let workspace = null,
    revisionKey = 0,
    evaluationState = null,
    drag = null;
  const scratch = new Map();
  const labelKey = (evidence, view) => `${evidence}:${view}`;
  const color = (value) =>
    ({ red: "#f97868", yellow: "#ffe381", unknown: "#c8e5f7" })[value];
  const clean = (row) => {
    const result = structuredClone(row);
    result.negative_regions ||= [];
    delete result.id;
    delete result.revision;
    return result;
  };
  const currentObservation = () =>
    state.data.observations.find(
      (o) => o.evidence_id === workspace?.labels.evidence_id,
    );
  const asset = () =>
    state.data.evidence.find((e) => e.id === workspace.labels.evidence_id)
      .crops[workspace.labels.view];
  const countText = () =>
    `${workspace.labels.points.length} handle centers · ${workspace.labels.ignore_regions.length} ignored regions · ${workspace.labels.negative_regions.length} non-stone regions`;

  function rememberForm() {
    const form = $("#label-form");
    if (!workspace || !form) return;
    const data = new FormData(form),
      labels = workspace.labels;
    for (const key of ["coverage", "provenance", "evaluation_group", "note"])
      labels[key] = data.get(key);
    labels.review_status = data.get("reviewed") ? "reviewed" : "draft";
    workspace.author = data.get("author");
    workspace.reason = data.get("reason");
  }
  function change(mutate) {
    rememberForm();
    workspace.undo.push(structuredClone(workspace.labels));
    if (workspace.undo.length > 50) workspace.undo.shift();
    mutate(workspace.labels);
    workspace.labels.review_status = "draft";
    workspace.dirty = true;
    revisionKey++;
    renderStudio();
    renderFrame();
  }
  async function openStudio() {
    const frame = currentFrame();
    if (!frame) return toast("Cache a frame before labeling.", true);
    if (workspace) return;
    const view = state.view === "composite" ? "house_top" : state.view;
    evaluationState = await api("/evaluation");
    const saved = await api(`/labels/${frame.id}/${view}`);
    const restored = scratch.get(labelKey(frame.id, view));
    workspace = restored || {
      revision: saved?.revision || 0,
      labels: saved
        ? clean(saved)
        : {
            evidence_id: frame.id,
            view,
            object_kind: "colored_handle_center",
            coverage: "partial",
            review_status: "draft",
            provenance: "human",
            evaluation_group:
              evaluationState.suggested_source_groups[state.source],
            points: [],
            ignore_regions: [],
            negative_regions: [],
            copied_from_model_version: null,
            note: "",
          },
      author: localStorage.getItem("hogline-reviewer") || "",
      reason: "",
      undo: [],
      dirty: false,
    };
    state.view = view;
    revisionKey++;
    renderStudio();
    renderFrame();
    $("#frame-stage").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  function closeStudio() {
    if (!workspace) return;
    rememberForm();
    if (workspace.dirty)
      scratch.set(
        labelKey(workspace.labels.evidence_id, workspace.labels.view),
        workspace,
      );
    workspace = null;
    revisionKey++;
    $("#label-studio").hidden = true;
    renderFrame();
  }
  function renderStudio() {
    const root = $("#label-studio");
    root.hidden = !workspace;
    if (!workspace) return;
    const labels = workspace.labels,
      dimensions = asset();
    root.innerHTML = `<form class="editor label-form" id="label-form"><div class="section-heading"><h3>Label visible handles</h3><span class="pill">${labels.view} · ${dimensions.width}×${dimensions.height}</span></div><p>Click handle centers in the image. Drag an ignored region over an occlusion. Mark known clothing or equipment as a non-stone area; detections there count as errors even inside an ignored region. Labels describe colored handles, not granite footprints.</p><div class="label-modes">${["red", "yellow", "unknown", "remove", "ignore", "negative"].map((mode) => `<button type="button" class="button ${workspace.mode === mode || (!workspace.mode && mode === "red") ? "primary" : "secondary"}" data-label-mode="${mode}" aria-pressed="${workspace.mode === mode || (!workspace.mode && mode === "red")}">${{ red: "Red", yellow: "Yellow", unknown: "Color unknown", remove: "Remove mark", ignore: "Ignore region", negative: "Mark non-stone area" }[mode]}</button>`).join("")}</div><p class="fine-print">${countText()} · ${workspace.dirty ? "unsaved changes" : workspace.revision ? "saved revision " + workspace.revision : "new draft"}</p><div class="label-points">${labels.points.map((p, i) => `<span class="label-chip"><button type="button" data-label-visibility="${i}" title="Toggle full / partial visibility">${i + 1} · ${p.color} · ${Math.round(p.center_px[0])},${Math.round(p.center_px[1])} · ${p.visibility}</button><button type="button" data-remove-label="${i}" aria-label="Remove label ${i + 1}">×</button></span>`).join("") || '<span class="fine-print">No labeled handles.</span>'}</div><div class="form-grid"><label>Coverage<select name="coverage">${options(
      [
        ["partial", "Partial labels"],
        ["complete_visible", "All visible handles labeled"],
        ["unobservable", "View unobservable"],
      ],
      labels.coverage,
    )}</select></label><label>Label provenance<select name="provenance">${options(
      [
        ["human", "Human reviewer"],
        ["assistant_visual", "Assistant visual audit"],
        ["synthetic", "Synthetic construction"],
        ["model_seeded", "Unverified model seed"],
      ],
      labels.provenance,
    )}</select></label></div><label>Recording/day group<input name="evaluation_group" value="${escapeHTML(labels.evaluation_group)}" required></label><p class="fine-print">Keep a recording day, adjacent frames, and duplicate-upload family in the same evaluation group. Title-day suggestions still need verification.</p><label class="check-label"><input type="checkbox" name="reviewed" ${labels.review_status === "reviewed" ? "checked" : ""}> I checked the visible handles, ignored regions, and non-stone areas.</label><label>Annotation notes<textarea name="note" rows="2">${escapeHTML(labels.note)}</textarea></label><div class="form-grid"><label>Reviewer<input name="author" value="${escapeHTML(workspace.author)}" required></label><label>Reason for this revision<input name="reason" value="${escapeHTML(workspace.reason)}" required></label></div><div class="label-actions"><button type="button" class="button quiet" id="label-seed">Use model candidates as a draft</button><button type="button" class="button quiet" id="label-undo" ${workspace.undo.length ? "" : "disabled"}>Undo mark</button><button type="button" class="button quiet" id="label-clear">Clear marks</button><button type="button" class="button secondary" id="label-close">Close draft</button><button type="submit" class="button primary">Save label revision →</button></div></form>`;
  }
  function overlay(frame, view) {
    if (
      !workspace ||
      workspace.labels.evidence_id !== frame.id ||
      workspace.labels.view !== view
    )
      return "";
    const labels = workspace.labels,
      { width, height } = frame.crops[view];
    const regions = labels.ignore_regions
      .map(
        (r) =>
          `<rect x="${r.x}" y="${r.y}" width="${r.width}" height="${r.height}" fill="#1c302c66" stroke="#fff" stroke-dasharray="5 4"/>`,
      )
      .join("");
    const negatives = labels.negative_regions
      .map(
        (r) =>
          `<rect x="${r.x}" y="${r.y}" width="${r.width}" height="${r.height}" fill="#ff5fa044" stroke="#ff5fa0" stroke-width="2"/>`,
      )
      .join("");
    const marks = labels.points
      .map(
        (p, i) =>
          `<circle cx="${p.center_px[0]}" cy="${p.center_px[1]}" r="6" fill="${color(p.color)}" stroke="#fff" stroke-width="2" ${p.visibility === "partial" ? 'stroke-dasharray="3 2"' : ""}/><text x="${p.center_px[0] + 9}" y="${p.center_px[1] - 7}" fill="#fff" stroke="#15291d" stroke-width=".4" font-size="13">${i + 1}</text>`,
      )
      .join("");
    return `<svg class="label-overlay" viewBox="0 0 ${width} ${height}" aria-label="Editable handle labels">${regions}${negatives}${marks}</svg>`;
  }
  function position(event, svg, clamp = false) {
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const transformed = point.matrixTransform(svg.getScreenCTM().inverse());
    const dimensions = asset();
    if (
      !clamp &&
      (transformed.x < 0 ||
        transformed.y < 0 ||
        transformed.x >= dimensions.width ||
        transformed.y >= dimensions.height)
    )
      return null;
    return [
      Math.max(0, Math.min(dimensions.width - 0.001, transformed.x)),
      Math.max(0, Math.min(dimensions.height - 0.001, transformed.y)),
    ];
  }
  document.addEventListener("pointerdown", (event) => {
    const svg = event.target.closest(".label-overlay");
    if (!svg || !workspace) return;
    const start = position(event, svg);
    if (!start) return;
    event.preventDefault();
    drag = { svg, start, pointer: event.pointerId };
    svg.setPointerCapture(event.pointerId);
  });
  document.addEventListener("pointerup", (event) => {
    if (!drag || !workspace) return;
    const { svg, start } = drag;
    drag = null;
    if (!svg.isConnected) return;
    const mode = workspace.mode || "red",
      end = position(event, svg, mode === "ignore" || mode === "negative");
    if (!end) return;
    if (mode === "ignore" || mode === "negative") {
      const x = Math.min(start[0], end[0]),
        y = Math.min(start[1], end[1]),
        width = Math.abs(start[0] - end[0]),
        height = Math.abs(start[1] - end[1]);
      if (width < 3 || height < 3) return;
      change((l) =>
        l[mode === "negative" ? "negative_regions" : "ignore_regions"].push({
          x,
          y,
          width,
          height,
          reason:
            mode === "negative" ? "known_nonstone" : "occluded_or_unobservable",
        }),
      );
    } else if (mode === "remove") {
      const nearest = workspace.labels.points
        .map((p, i) => ({
          i,
          d: Math.hypot(p.center_px[0] - end[0], p.center_px[1] - end[1]),
        }))
        .sort((a, b) => a.d - b.d)[0];
      if (nearest?.d < 18) change((l) => l.points.splice(nearest.i, 1));
      else {
        // Remove a known negative before the larger occlusion region underneath it.
        for (const key of ["negative_regions", "ignore_regions"]) {
          const i = workspace.labels[key].findIndex(
            (r) =>
              end[0] >= r.x &&
              end[0] <= r.x + r.width &&
              end[1] >= r.y &&
              end[1] <= r.y + r.height,
          );
          if (i >= 0) {
            change((l) => l[key].splice(i, 1));
            break;
          }
        }
      }
    } else
      change((l) =>
        l.points.push({
          id: crypto.randomUUID(),
          color: mode,
          center_px: end,
          visibility: "visible",
          note: "",
        }),
      );
  });
  document.addEventListener("pointercancel", () => {
    drag = null;
  });
  document.addEventListener("input", (event) => {
    if (event.target.closest("#label-form") && workspace) {
      rememberForm();
      workspace.dirty = true;
    }
  });
  document.addEventListener("change", (event) => {
    if (event.target.closest("#label-form") && workspace) {
      rememberForm();
      workspace.dirty = true;
    }
  });

  document.addEventListener("keydown", (event) => {
    if (
      !workspace ||
      event.target.closest('input,textarea,select,[contenteditable="true"]')
    )
      return;
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z") {
      event.preventDefault();
      $("#label-undo").click();
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const mode = {
      r: "red",
      y: "yellow",
      u: "unknown",
      x: "remove",
      i: "ignore",
      n: "negative",
    }[event.key.toLowerCase()];
    if (mode) {
      event.preventDefault();
      rememberForm();
      workspace.mode = mode;
      renderStudio();
    }
  });

  async function loadEvaluation() {
    const existing = $("#evaluate-form");
    const selected = existing
      ? Object.fromEntries(new FormData(existing))
      : null;
    evaluationState = await api("/evaluation");
    renderEvaluation();
    if (selected)
      for (const [name, value] of Object.entries(selected)) {
        const field = $("#evaluate-form").elements.namedItem(name);
        if (field) field.value = value;
      }
  }
  function renderEvaluation() {
    const data = evaluationState,
      groups = data.groups,
      datasets = data.datasets;
    const partitions = Object.values(datasets[0]?.partitions || {});
    const initialPartition = partitions.includes("test")
      ? "test"
      : partitions.includes("validation")
        ? "validation"
        : "train";
    $("#evaluation-content").innerHTML =
      `<form id="freeze-form"><h3>1. Freeze an evaluation dataset</h3>${
        groups.length
          ? `<div class="partition-groups">${groups
              .map(
                (g, i) =>
                  `<label>${escapeHTML(g.id)} <span class="fine-print">${g.complete_reviewed_views} complete/reviewed · ${escapeHTML(g.provenances.join(", "))}</span><select name="group-${i}">${options(
                    [
                      ["exclude", "Exclude"],
                      ["train", "Train"],
                      ["validation", "Validation"],
                      ["test", "Test"],
                    ],
                    "exclude",
                  )}</select></label>`,
              )
              .join("")}</div>`
          : '<p class="fine-print">Label a complete view first. An empty visible house can be a valid negative example.</p>'
      }<label>Dataset name<input name="name" required placeholder="Wausau pilot · held-out frames"></label><label>Label mode<select name="mode">${options(
        [
          ["confirmed", "Confirmed · human labels only"],
          ["exploratory", "Exploratory · includes assistant/synthetic labels"],
        ],
        "confirmed",
      )}</select></label><div class="form-grid"><label>Reviewer<input name="author" value="${escapeHTML(localStorage.getItem("hogline-reviewer") || "")}" required></label><label>Reason<input name="reason" required placeholder="Frozen regression cohort"></label></div><button class="button primary" type="submit" ${groups.length ? "" : "disabled"}>Freeze exact revisions →</button></form><form id="evaluate-form"><h3>2. Evaluate one frozen partition</h3><label>Dataset<select name="dataset_id" required>${datasets.map((d) => `<option value="${d.id}">${escapeHTML(d.name)} · ${d.record_count} views · ${d.mode}</option>`).join("")}</select></label><label>Detector version<select name="model_version">${options(
        data.model_versions.map((v) => [v, v]),
        data.default_model_version,
      )}</select></label><div class="form-grid"><label>Partition<select name="partition">${options(
        [
          ["test", "Test"],
          ["validation", "Validation"],
          ["train", "Train diagnostics"],
        ],
        initialPartition,
      )}</select></label><label>Max center error / crop width<input name="tolerance" type="number" min="0.001" max="0.2" step="0.001" value="0.035" required></label></div><p class="fine-print">One-to-one matching counts missed and extra detections. Pixel-space handle accuracy does not establish score, physical-distance, or identity accuracy.</p><button class="button primary" type="submit" ${datasets.length ? "" : "disabled"}>Run current detector →</button></form><div id="evaluation-report"></div><h3>Saved evaluation runs</h3>${
        data.runs
          .slice()
          .reverse()
          .map(
            (r) =>
              `<p class="fine-print"><a href="/api/evaluation/runs/${r.id}.json" download>${escapeHTML(r.model_version)} · ${r.partition} · ${r.mode} ↗</a><br>${r.summary.views} evaluated views · F1 ${percent(r.summary.f1)}</p>`,
          )
          .join("") || '<p class="fine-print">No detector evaluation yet.</p>'
      }`;
  }
  function percent(value) {
    return value === null ? "—" : (100 * value).toFixed(1) + "%";
  }
  function renderReport(report) {
    const s = report.summary;
    $("#evaluation-report").innerHTML =
      `<div class="evaluation-result"><span class="pill amber">${escapeHTML(report.mode.toUpperCase())} · ${report.partition.toUpperCase()}</span><h3>${s.true_positive} matched · ${s.false_negative} missed · ${s.false_positive} extra</h3><p class="fine-print">${escapeHTML(report.model_version)} · ${escapeHTML(report.dataset_name)} · label provenance: ${escapeHTML(Object.keys(report.breakdowns.provenance).join(", "))}</p><p>Precision ${percent(s.precision)} · recall ${percent(s.recall)} · F1 ${percent(s.f1)}</p><p class="fine-print">${report.coverage.evaluated_views} / ${report.coverage.eligible_frozen_views} frozen views evaluated. ${s.ignored_predictions} predictions ignored in occluded regions. ${s.known_negative_predictions || 0} predictions on known non-stone areas counted as errors. This report cannot authorize automatic score acceptance.</p><table><thead><tr><th>Sheet</th><th>Views</th><th>Matched</th><th>Missed</th><th>Extra</th></tr></thead><tbody>${Object.entries(
        report.breakdowns.sheet,
      )
        .map(
          ([sheet, m]) =>
            `<tr><td>${escapeHTML(sheet)}</td><td>${m.views}</td><td>${m.true_positive}</td><td>${m.false_negative}</td><td>${m.false_positive}</td></tr>`,
        )
        .join(
          "",
        )}</tbody></table><div class="evaluation-rows">${report.rows.map((r) => `<button class="button quiet" type="button" data-inspect-label="${r.evidence_id}:${r.view}">Sheet ${r.sheet} · ${r.view} · ${r.provenance} · ${r.false_negative} misses / ${r.false_positive} extras →</button>`).join("")}</div><a class="button secondary" href="/api/evaluation/runs/${report.id}.json" download>Download reproducible report ↗</a></div>`;
  }
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    try {
      if (button.id === "label-frame") await openStudio();
      if (button.id === "label-close") closeStudio();
      if (button.dataset.labelMode) {
        rememberForm();
        workspace.mode = button.dataset.labelMode;
        renderStudio();
      }
      if (button.dataset.removeLabel !== undefined)
        change((l) => l.points.splice(Number(button.dataset.removeLabel), 1));
      if (button.dataset.labelVisibility !== undefined)
        change((l) => {
          const point = l.points[Number(button.dataset.labelVisibility)];
          point.visibility =
            point.visibility === "visible" ? "partial" : "visible";
        });
      if (button.id === "label-clear")
        change((l) => {
          l.points = [];
          l.ignore_regions = [];
          l.negative_regions = [];
        });
      if (button.id === "label-undo" && workspace.undo.length) {
        rememberForm();
        workspace.labels = workspace.undo.pop();
        workspace.labels.review_status = "draft";
        workspace.dirty = true;
        revisionKey++;
        renderStudio();
        renderFrame();
      }
      if (button.id === "label-seed") {
        const observation = currentObservation();
        if (!observation)
          return toast("Run the detector on this retained frame first.", true);
        change((l) => {
          l.points = observation.views[l.view].detections.map((d) => ({
            id: crypto.randomUUID(),
            color: d.color,
            center_px: d.center_px,
            visibility: d.boundary_clipped ? "partial" : "visible",
            note: "Model-seeded draft",
          }));
          l.provenance = "model_seeded";
          l.copied_from_model_version = observation.model_version;
        });
      }
      if (button.id === "evaluation-button") {
        await loadEvaluation();
        $("#evaluation-dialog").showModal();
      }
      if (button.dataset.inspectLabel) {
        if (!window.hoglineLabels.beforeNavigate()) return;
        const [id, view] = button.dataset.inspectLabel.split(":"),
          frame = state.data.evidence.find((e) => e.id === id);
        if (!frame)
          return toast(
            "This frozen frame is not present in the current catalog.",
            true,
          );
        state.source = frame.source_id;
        state.seconds = frame.source_seconds;
        state.view = view;
        $("#evaluation-dialog").close();
        render();
        await openStudio();
      }
    } catch (error) {
      toast(error.message, true);
    }
  });
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!["label-form", "freeze-form", "evaluate-form"].includes(form.id))
      return;
    event.preventDefault();
    const submit = $('button[type="submit"]', form);
    submit.disabled = true;
    try {
      const data = new FormData(form);
      if (form.id === "label-form") {
        rememberForm();
        const labels = workspace.labels;
        const saved = await api(
          `/labels/${labels.evidence_id}/${labels.view}`,
          "PUT",
          {
            expected_revision: workspace.revision,
            author: workspace.author,
            reason: workspace.reason,
            labels,
          },
        );
        workspace.revision = saved.revision;
        workspace.labels = clean(saved);
        workspace.dirty = false;
        workspace.reason = "";
        scratch.delete(labelKey(labels.evidence_id, labels.view));
        localStorage.setItem("hogline-reviewer", workspace.author);
        revisionKey++;
        renderStudio();
        renderFrame();
        toast(
          "Label revision saved with provenance and original crop coordinates.",
        );
      }
      if (form.id === "freeze-form") {
        const partitions = {};
        evaluationState.groups.forEach((g, i) => {
          const partition = data.get(`group-${i}`);
          if (partition !== "exclude") partitions[g.id] = partition;
        });
        const result = await api("/evaluation/datasets", "POST", {
          name: data.get("name"),
          author: data.get("author"),
          reason: data.get("reason"),
          mode: data.get("mode"),
          partitions,
        });
        await loadEvaluation();
        $("#evaluate-form").elements.dataset_id.value = result.id;
        const values = Object.values(partitions);
        $("#evaluate-form").elements.partition.value = values.includes("test")
          ? "test"
          : values.includes("validation")
            ? "validation"
            : "train";
        toast(`Dataset frozen with ${result.records} eligible label views.`);
      }
      if (form.id === "evaluate-form") {
        const report = await api("/evaluation/runs", "POST", {
          dataset_id: data.get("dataset_id"),
          partition: data.get("partition"),
          max_center_error_fraction: Number(data.get("tolerance")),
          model_version: data.get("model_version") || null,
        });
        await loadEvaluation();
        renderReport(report);
        toast("Evaluation saved against the frozen label revisions.");
      }
    } catch (error) {
      toast(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });
  window.hoglineLabels = {
    overlay,
    key: () => revisionKey,
    evidenceId: () => workspace?.labels.evidence_id,
    beforeNavigate: () => {
      if (workspace?.dirty) {
        toast("Save or close the label draft before changing the frame.", true);
        return false;
      }
      closeStudio();
      return true;
    },
  };
})();
