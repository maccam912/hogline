(() => {
  let base = null,
    candidates = [],
    preview = null,
    conflictDefinitions = [];
  const form = () => $("#reconcile-form");
  const canonical = (value) => {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object")
      return Object.fromEntries(
        Object.keys(value)
          .sort()
          .map((key) => [key, canonical(value[key])]),
      );
    return value;
  };
  function canLeaveDraft() {
    if (window.hoglineLabels && !window.hoglineLabels.beforeNavigate())
      return false;
    if (!state.draft) return true;
    readEditor();
    const saved = state.data.games.find((g) => g.id === state.draft.game.id);
    if (
      !saved ||
      state.audit.reason?.trim() ||
      JSON.stringify(canonical(cleanGame(saved))) !==
        JSON.stringify(canonical(state.draft.game))
    ) {
      toast(
        "Save or close your unsaved game edits before splitting, merging, or undoing a correction.",
        true,
      );
      return false;
    }
    return true;
  }
  function invalidate() {
    preview = null;
    $("#publish-reconciliation").disabled = true;
    $("#reconcile-preview").textContent =
      "Preview the current correction before saving.";
  }
  function open(id) {
    if (!canLeaveDraft()) return;
    base = structuredClone(state.data.games.find((g) => g.id === id));
    if (!base)
      return toast("This game is no longer active. Reload its history.", true);
    candidates = structuredClone(
      state.data.games.filter(
        (g) => g.sheet === base.sheet && g.id !== base.id,
      ),
    );
    form().reset();
    form().elements.author.value =
      localStorage.getItem("hogline-reviewer") || "";
    $("#reconcile-parent").textContent =
      `${base.label} · revision ${base.revision}. Original interpretations stay in history. New boundaries, carried scores, and final results require review.`;
    renderInputs();
    $("#reconcile-dialog").showModal();
  }
  function renderInputs() {
    conflictDefinitions = [];
    $("#reconcile-conflicts").innerHTML = "";
    invalidate();
    const f = form();
    if (f.elements.action.value === "merge") {
      $("#reconcile-inputs").innerHTML = candidates.length
        ? `<h3>Other game intervals on this sheet</h3>${candidates.map((g) => `<label class="reconcile-choice"><input type="checkbox" name="merge-parent" value="${g.id}"><span>${escapeHTML(g.label)} · ${g.ends.length} recorded ends</span></label>`).join("")}`
        : '<p class="fine-print">There are no other active games on this sheet.</p>';
      return;
    }
    const ids = [...new Set(base.spans.map((s) => s.source_id))];
    $("#reconcile-inputs").innerHTML =
      `<h3>Split position in each recording</h3><p class="fine-print">Enter each recording’s source time. Use 00:00:00 to put its full span in the later game, or its duration to put the full span in the earlier game. Recordings are not assumed synchronized.</p>${ids
        .map((id) => {
          const src = state.data.sources.find((s) => s.id === id),
            spans = base.spans.filter((s) => s.source_id === id);
          const start = Math.min(...spans.map((s) => s.start_seconds)),
            end = Math.max(...spans.map((s) => s.end_seconds));
          const value =
            id === state.source && state.seconds > start && state.seconds < end
              ? state.seconds
              : (start + end) / 2;
          return `<label>${escapeHTML(src?.title || id)}<input name="cut-${id}" value="${value % 1 ? value : time(value)}"></label><p class="fine-print">Linked span ${time(start)}–${time(end)}${youtube(src, start) ? ` · <a class="evidence-link" href="${escapeHTML(youtube(src, start))}" target="_blank" rel="noopener">Check recording ↗</a>` : ""}</p>`;
        })
        .join("")}`;
  }
  function read() {
    const data = new FormData(form()),
      action = data.get("action");
    const selected = [
      base,
      ...candidates.filter(
        (g) => action === "merge" && data.getAll("merge-parent").includes(g.id),
      ),
    ];
    return {
      action,
      parents: selected.map((g) => ({
        game_id: g.id,
        expected_revision: g.revision,
      })),
      source_cuts:
        action === "split"
          ? Object.fromEntries(
              [...data.entries()]
                .filter(([key]) => key.startsWith("cut-"))
                .map(([key, value]) => [key.slice(4), parseTime(value)]),
            )
          : {},
      end_choices: Object.fromEntries(
        [...data.entries()]
          .filter(([key, value]) => key.startsWith("choice-") && value)
          .map(([key, value]) => [key.slice(7), value]),
      ),
      label: data.get("label").trim() || null,
    };
  }
  function gameSummary(g) {
    return `<article class="reconcile-game"><h4>${escapeHTML(g.label)}</h4><p>${g.ends.length} recorded ends · ${escapeHTML(g.acceptance.replaceAll("_", " "))} · result ${escapeHTML(g.result.outcome.replaceAll("_", " "))}</p>${g.spans
      .map((s) => {
        const src = state.data.sources.find((x) => x.id === s.source_id),
          url = youtube(src, s.start_seconds);
        return `<p class="fine-print">${escapeHTML(src?.title || s.source_id)} · ${time(s.start_seconds)}–${time(s.end_seconds)} · ${escapeHTML(s.relationship)} ${url ? `<a class="evidence-link" href="${escapeHTML(url)}" target="_blank" rel="noopener">Check span ↗</a>` : ""}</p>`;
      })
      .join(
        "",
      )}${g.ends.length ? `<p class="fine-print">${g.ends.map((e) => `End ${e.official_number ?? "?"}: ${e.red_points ?? "?"} red / ${e.yellow_points ?? "?"} yellow (${escapeHTML(e.acceptance.replaceAll("_", " "))})`).join(" · ")}</p>` : ""}</article>`;
  }
  function renderConflicts(conflicts) {
    const values = new FormData(form());
    const unresolved = new Set(conflicts.map((c) => c.key));
    conflictDefinitions = [
      ...conflicts,
      ...conflictDefinitions.filter(
        (c) => !unresolved.has(c.key) && values.get(`choice-${c.key}`),
      ),
    ];
    $("#reconcile-conflicts").innerHTML = conflictDefinitions
      .map((c) => {
        const options =
          c.kind === "split_end_assignment"
            ? c.options.map((side) => [
                side,
                side === "earlier" ? "Earlier game" : "Later game",
              ])
            : c.options.map((o) => [
                o.value,
                `${o.parent_label}: ${o.end.red_points ?? "?"} red / ${o.end.yellow_points ?? "?"} yellow · ${o.end.acceptance.replaceAll("_", " ")}`,
              ]);
        return `<section class="reconcile-conflict"><p>End ${c.official_number ?? `? (observed ${c.observed_index})`}: ${escapeHTML(c.reason)}</p>${options.length ? `<label>Choose interpretation<select name="choice-${escapeHTML(c.key)}">${windowOptions([["", "Choose…"], ...options], values.get(`choice-${c.key}`) || "")}</select></label>` : ""}</section>`;
      })
      .join("");
  }
  // Use the workbench's shared escaping and option renderer.
  const windowOptions = (items, selected) => options(items, selected);
  form().addEventListener("change", (event) => {
    if (event.target.name === "action") return renderInputs();
    if (event.target.name === "merge-parent") {
      conflictDefinitions = [];
      $("#reconcile-conflicts").innerHTML = "";
    }
    if (!["author", "reason"].includes(event.target.name)) invalidate();
  });
  form().addEventListener("input", (event) => {
    if (!["author", "reason"].includes(event.target.name)) invalidate();
  });
  form().addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    try {
      const request = read(),
        signature = JSON.stringify(request);
      const result = await api("/reconciliations/preview", "POST", request);
      if (JSON.stringify(read()) !== signature) return;
      // Preserve already resolved choices; only remaining conflicts need selectors.
      if (result.conflicts.length) {
        renderConflicts(result.conflicts);
        invalidate();
        $("#reconcile-preview").textContent =
          "Resolve the end assignments above, then preview again.";
        return;
      }
      renderConflicts([]);
      preview = { request, preview_sha256: result.preview_sha256 };
      $("#reconcile-preview").innerHTML =
        `<h3>Proposed correction</h3>${result.successors.map(gameSummary).join("")}<p class="fine-print">${result.end_mappings.length} prior end interpretations mapped; ${result.end_mappings.filter((m) => m.action === "superseded").length} remain only in parent history. ${result.invalidated_results.length} previous final results will require new review.</p><p>${escapeHTML(result.notice)}</p>`;
      $("#publish-reconciliation").disabled = false;
    } catch (error) {
      invalidate();
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  function render() {
    const related = (state.data.reconciliations || []).filter((r) =>
      [...r.parents.map((p) => p.game), ...r.successors].some((g) =>
        g.spans.some((s) => s.source_id === state.source),
      ),
    );
    $("#reconciliation-history").hidden = !related.length;
    $("#reconciliation-history").innerHTML =
      `<h3>Game correction history</h3>${related
        .slice(-10)
        .reverse()
        .map(
          (r) =>
            `<article class="reconcile-game"><p>${r.action === "split" ? "Split" : "Merged"} · ${escapeHTML(r.state)} · ${escapeHTML(r.author)}</p><p class="fine-print">${escapeHTML(r.reason)}</p><button class="button quiet" data-reconciliation-history="${r.id}">Inspect correction →</button></article>`,
        )
        .join("")}`;
  }
  function showHistory(id) {
    const r = state.data.reconciliations.find((x) => x.id === id);
    if (!r) return;
    $("#reconcile-history-detail").innerHTML =
      `<p>${escapeHTML(r.reason)} · ${escapeHTML(r.author)} · ${escapeHTML(r.state)}</p><h3>Previous grouping</h3>${r.parents.map((p) => gameSummary(p.game)).join("")}<h3>Replacement grouping</h3>${r.successors.map(gameSummary).join("")}${r.state === "active" ? `<p class="fine-print">Restore is available only while every related game remains unchanged. Later edits must be resolved first.</p><label>Reviewer<input id="undo-reconcile-author" value="${escapeHTML(localStorage.getItem("hogline-reviewer") || "")}" maxlength="120"></label><label>Reason for restoring<input id="undo-reconcile-reason" maxlength="2000"></label><button class="button secondary" data-undo-reconciliation="${r.id}">Restore previous grouping →</button>` : `<p class="fine-print">Restored by ${escapeHTML(r.undo.author)}: ${escapeHTML(r.undo.reason)}</p>`}`;
    $("#reconcile-history-dialog").showModal();
  }
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.reconcile) return open(button.dataset.reconcile);
    if (button.dataset.reconciliationHistory)
      return showHistory(button.dataset.reconciliationHistory);
    if (button.id === "publish-reconciliation" && preview) {
      const author = form().elements.author.value.trim(),
        reason = form().elements.reason.value.trim();
      if (!author || !reason)
        return toast("Enter a reviewer and reason for this correction.", true);
      button.disabled = true;
      try {
        await api("/reconciliations", "POST", { ...preview, author, reason });
        localStorage.setItem("hogline-reviewer", author);
        $("#reconcile-dialog").close();
        state.draft = null;
        renderEditor();
        await refresh();
        toast(
          "Correction saved. The new grouping needs review; previous interpretations remain in history.",
        );
      } catch (error) {
        invalidate();
        toast(error.message, true);
      }
    }
    if (button.dataset.undoReconciliation) {
      if (!canLeaveDraft()) return;
      const receipt = state.data.reconciliations.find(
        (r) => r.id === button.dataset.undoReconciliation,
      );
      const author = $("#undo-reconcile-author").value.trim(),
        reason = $("#undo-reconcile-reason").value.trim();
      if (!author || !reason)
        return toast(
          "Enter a reviewer and reason for restoring this grouping.",
          true,
        );
      button.disabled = true;
      try {
        await api(`/reconciliations/${receipt.id}/undo`, "POST", {
          expected_revision: receipt.revision,
          author,
          reason,
        });
        $("#reconcile-history-dialog").close();
        state.draft = null;
        renderEditor();
        await refresh();
        toast(
          "Previous grouping restored. All correction history is retained.",
        );
      } catch (error) {
        button.disabled = false;
        toast(error.message, true);
      }
    }
  });
  window.hoglineReconciliation = { open, render };
})();
