(() => {
  let editing = null,
    dirty = false;
  const dialog = () => $("#board-label-dialog");
  const pct = (n) => (n == null ? "—" : `${(100 * n).toFixed(1)}%`);
  const options = (values, selected) =>
    values
      .map(
        ([value, label]) =>
          `<option value="${escapeHTML(value)}"${value === selected ? " selected" : ""}>${escapeHTML(label)}</option>`,
      )
      .join("");
  function closeLabels() {
    if (dirty)
      return toast(
        "Save these labels or choose Discard changes before closing.",
        true,
      );
    dialog().close();
  }
  dialog().addEventListener("cancel", (event) => {
    event.preventDefault();
    closeLabels();
  });
  $("#close-board-labels").addEventListener("click", closeLabels);
  $("#board-label-content").addEventListener("input", () => {
    dirty = true;
  });
  $("#board-label-content").addEventListener("change", () => {
    dirty = true;
    const f = $("#board-label-form");
    for (const color of ["red", "yellow"])
      for (let n = 1; n <= 15; n++)
        f.elements[`${color}-${n}`].disabled =
          f.elements[`${color}-visibility`].value === "unreadable";
  });
  async function openLabels(evidenceId) {
    const frame = state.data.evidence.find((f) => f.id === evidenceId);
    if (!frame || frame.acquisition_tier === "coarse") return;
    const saved = await api(`/board-labels/${encodeURIComponent(evidenceId)}`),
      label = saved.labels;
    const src = state.data.sources.find((s) => s.id === frame.source_id);
    const obs = state.data.broadcast.observations.find(
      (o) => o.evidence_id === evidenceId,
    );
    editing = { evidenceId, revision: label?.revision || 0 };
    dirty = false;
    $("#board-label-content").innerHTML =
      `<p><strong>Sheet ${src.sheet} · ${time(frame.source_seconds)}</strong>${frame.youtube_url ? ` · <a href="${escapeHTML(frame.youtube_url)}" target="_blank" rel="noopener">Check on YouTube ↗</a>` : ""}</p>
      ${obs?.crops?.scoreboard ? `<img class="board-label-image" src="/api/broadcast/${obs.id}/scoreboard" alt="Retained scoreboard crop">` : ""}
      <details${obs?.crops?.scoreboard ? "" : " open"}><summary>Full original frame</summary><img class="board-label-image" src="/api/evidence/${evidenceId}/composite" alt="Original frame for board labeling"></details>
      <form id="board-label-form">${["red", "yellow"]
        .map((color) => {
          const row = label?.rows[color],
            visibility = row?.visibility || "unreadable";
          return `<fieldset><legend>${color === "red" ? "Red" : "Yellow"} row</legend><label>Row visibility<select name="${color}-visibility">${options(
            [
              ["unreadable", "Unreadable / obscured / uncertain"],
              ["readable", "Readable — label all visible cards"],
            ],
            visibility,
          )}</select></label><p class="fine-print">Enter each movable end number under its cumulative point column. Leave empty columns blank.</p><div class="board-label-columns">${Array.from(
            { length: 15 },
            (_, i) => {
              const n = i + 1,
                card = row?.cards.find((c) => c.cumulative_points === n);
              return `<label>Column ${n}<input name="${color}-${n}" aria-label="${color} column ${n} end number" type="number" min="1" max="16" step="1" value="${card?.end_number ?? ""}"${visibility === "unreadable" ? " disabled" : ""}></label>`;
            },
          ).join("")}</div></fieldset>`;
        })
        .join("")}<div class="form-grid">
      <label>Label provenance<select name="provenance">${options(
        [
          ["human", "Human inspected"],
          ["assistant_visual", "Assistant visual inspection"],
          ["synthetic", "Synthetic fixture"],
          ["model_seeded", "Unverified model seed"],
        ],
        label?.provenance || "human",
      )}</select></label>
      <label>Review status<select name="review_status">${options(
        [
          ["draft", "Draft"],
          ["reviewed", "Reviewed"],
        ],
        label?.review_status || "draft",
      )}</select></label>
      <label>Whole recording/day group<input name="evaluation_group" required value="${escapeHTML(label?.evaluation_group || `wausau:${src.title_date || src.id}`)}"></label>
      <label>Reviewer<input name="author" required value="${escapeHTML(localStorage.getItem("hogline-reviewer") || "")}"></label></div>
      <label>Reason for this revision<input name="reason" required></label><label>Label notes<textarea name="note">${escapeHTML(label?.note || "")}</textarea></label>
      <p class="fine-print">These labels measure reader performance. Saving does not change a game, end score, or accepted result.</p>
      <div class="editor-actions"><button type="button" class="button quiet" id="discard-board-labels">Discard changes</button><button class="button primary" type="submit">Save board labels →</button></div></form>
      <details><summary>Label revision history · ${saved.history.length}</summary>${saved.history.map((r) => `<p class="fine-print">Revision ${r.revision} · ${escapeHTML(r.author)} · ${escapeHTML(r.reason)} · ${escapeHTML(r.created_at)}</p>`).join("")}</details>`;
    dialog().showModal();
  }
  $("#board-label-content").addEventListener("submit", async (event) => {
    event.preventDefault();
    const f = event.target,
      d = new FormData(f),
      rows = {};
    for (const color of ["red", "yellow"]) {
      const visibility = d.get(`${color}-visibility`),
        cards = [];
      if (visibility === "readable")
        for (let n = 1; n <= 15; n++) {
          const value = d.get(`${color}-${n}`);
          if (value)
            cards.push({ end_number: Number(value), cumulative_points: n });
        }
      rows[color] = { visibility, cards };
    }
    const body = { expected_revision: editing.revision, rows };
    for (const name of [
      "author",
      "reason",
      "provenance",
      "review_status",
      "evaluation_group",
      "note",
    ])
      body[name] = d.get(name);
    event.submitter.disabled = true;
    try {
      await api(`/board-labels/${editing.evidenceId}`, "PUT", body);
      localStorage.setItem("hogline-reviewer", body.author);
      dirty = false;
      dialog().close();
      toast("Board label revision saved.");
    } catch (error) {
      toast(error.message, true);
      event.submitter.disabled = false;
    }
  });
  function resultCard(run) {
    const s = run.summary;
    return `<article class="batch-card"><strong>${escapeHTML(run.model_version)} · ${escapeHTML(run.partition)}</strong>
      <p>${run.frame_count} frames · ${escapeHTML(run.mode)} · ${run.provenances.map(escapeHTML).join(", ")}</p>
      <p>Cards: ${s.true_positive || 0} matched · ${s.false_positive || 0} extra · ${s.false_negative || 0} missed. Precision ${pct(s.card_precision)} · recall ${pct(s.card_recall)}.</p>
      <p>Exact readable rows: ${s.exact_rows || 0}/${s.readable_rows || 0} (${pct(s.exact_row_accuracy)}). ${s.abstained_readable_rows || 0} readable rows abstained. ${s.readable_predictions_on_unreadable_rows || 0}/${s.unreadable_rows || 0} unreadable labeled rows received a complete reading.</p>
      <p>${run.missing_prediction_evidence_ids.length} frames have no prediction; they remain in the denominator.</p>
      <table><thead><tr><th>Sheet</th><th>Rows</th><th>Exact</th><th>Extra cards</th><th>Missed cards</th></tr></thead><tbody>${Object.entries(
        run.by_sheet,
      )
        .map(
          ([sheet, m]) =>
            `<tr><td>${escapeHTML(sheet)}</td><td>${m.readable_rows || 0}</td><td>${pct(m.exact_row_accuracy)}</td><td>${m.false_positive || 0}</td><td>${m.false_negative || 0}</td></tr>`,
        )
        .join("")}</tbody></table>
      <a class="evidence-link" href="/api/board-evaluation/runs/${run.id}.json" download>Download frozen predictions and per-frame results ↗</a></article>`;
  }
  async function renderBenchmark() {
    const data = await api("/board-evaluation"),
      groups = [...new Set(data.labels.map((l) => l.evaluation_group))].sort();
    $("#board-evaluation-content").innerHTML =
      `<h3>Freeze reviewed labels</h3>${
        !groups.length
          ? "<p>No board labels saved yet. Select a retained detailed frame, then choose “Label visible board cards” under the board readings.</p>"
          : `<form id="freeze-board-dataset"><div class="form-grid"><label>Dataset name<input name="name" required></label><label>Label eligibility<select name="mode"><option value="confirmed">Human reviewed only</option><option value="exploratory">Exploratory — include inspected assistant/synthetic labels</option></select></label><label>Reviewer<input name="author" required value="${escapeHTML(localStorage.getItem("hogline-reviewer") || "")}"></label><label>Reason<input name="reason" required></label></div>${groups
              .map(
                (group, i) =>
                  `<label>${escapeHTML(group)} · ${data.labels.filter((l) => l.evaluation_group === group).length} labeled frames<select name="group-${i}">${options(
                    [
                      ["", "Exclude"],
                      ["train", "Training / development"],
                      ["validation", "Validation"],
                      ["test", "Test"],
                    ],
                    "",
                  )}</select></label>`,
              )
              .join(
                "",
              )}<button class="button primary" type="submit">Freeze board dataset →</button></form>`
      }
      <h3>Evaluate a reader</h3>${
        !data.datasets.length
          ? "<p>Freeze a dataset to compare reader versions.</p>"
          : `<form id="run-board-evaluation"><label>Frozen dataset<select name="dataset_id">${data.datasets.map((d) => `<option value="${d.id}">${escapeHTML(d.name)} · ${d.record_count} frames · ${d.mode}</option>`).join("")}</select></label><div class="form-grid"><label>Reader version<select name="model_version">${data.model_versions.map((v) => `<option>${escapeHTML(v)}</option>`).join("")}</select></label><label>Partition<select name="partition">${options(
              [
                ["test", "Test"],
                ["validation", "Validation"],
                ["train", "Training / development"],
              ],
              "test",
            )}</select></label></div><button class="button primary" type="submit"${data.model_versions.length ? "" : " disabled"}>Evaluate cached readings →</button></form>`
      }
      <details><summary>Frozen datasets · ${data.datasets.length}</summary>${data.datasets.map((d) => `<p><a href="/api/board-evaluation/datasets/${d.id}.json" download>${escapeHTML(d.name)} ↗</a> · ${escapeHTML(JSON.stringify(d.partitions))}</p>`).join("")}</details>
      <h3>Saved evaluations</h3>${data.runs.slice().reverse().slice(0, 10).map(resultCard).join("") || "<p>No board evaluation runs saved.</p>"}`;
    const freeze = $("#freeze-board-dataset");
    if (freeze)
      freeze.addEventListener("submit", async (event) => {
        event.preventDefault();
        const d = new FormData(freeze),
          partitions = {};
        groups.forEach((group, i) => {
          if (d.get(`group-${i}`)) partitions[group] = d.get(`group-${i}`);
        });
        event.submitter.disabled = true;
        try {
          await api("/board-evaluation/datasets", "POST", {
            partitions,
            ...Object.fromEntries(
              ["name", "mode", "author", "reason"].map((k) => [k, d.get(k)]),
            ),
          });
          await renderBenchmark();
          toast("Board labels frozen with their exact revisions.");
        } catch (error) {
          toast(error.message, true);
          event.submitter.disabled = false;
        }
      });
    const run = $("#run-board-evaluation");
    if (run)
      run.addEventListener("submit", async (event) => {
        event.preventDefault();
        event.submitter.disabled = true;
        try {
          await api(
            "/board-evaluation/runs",
            "POST",
            Object.fromEntries(new FormData(run)),
          );
          await renderBenchmark();
          toast("Board evaluation saved.");
        } catch (error) {
          toast(error.message, true);
          event.submitter.disabled = false;
        }
      });
  }
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    try {
      if (button?.dataset.labelBoard)
        await openLabels(button.dataset.labelBoard);
      if (button?.id === "discard-board-labels") {
        dirty = false;
        dialog().close();
      }
      if (button?.id === "board-evaluation-button") {
        await renderBenchmark();
        $("#board-evaluation-dialog").showModal();
      }
    } catch (error) {
      toast(error.message, true);
    }
  });
})();
