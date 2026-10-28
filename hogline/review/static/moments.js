(() => {
  let result = null,
    pending = false,
    lastQuery = null;
  const form = () => $("#moments-form");
  const stamp = (n) =>
    time(n) +
    (Number.isInteger(n) ? "" : (n % 1).toFixed(3).slice(1).replace(/0+$/, ""));
  const names = {
    board_change: "Posted board change",
    score_proposal: "Proposed end score",
    handle_motion: "Handle position change",
    house_configuration: "Persistent configuration",
    reviewed_end: "Saved end review",
  };
  async function search(offset = 0) {
    if (pending) return;
    pending = true;
    $("#moment-submit").disabled = true;
    $("#moment-next").disabled = true;
    $("#moment-download").disabled = true;
    $("#moments-status").textContent = "Searching retained evidence…";
    try {
      const values = new FormData(form());
      const query = offset
        ? { ...lastQuery, offset, expected_index_sha256: result.index_sha256 }
        : {
            text: values.get("text"),
            sheet: values.get("sheet") ? Number(values.get("sheet")) : null,
            source_id: values.get("source") || null,
            kinds: values.get("kind") ? [values.get("kind")] : [],
            acceptance: values.get("acceptance") || null,
            title_date_from: values.get("from") || null,
            title_date_to: values.get("to") || null,
            include_earlier_inputs: values.has("earlier"),
            limit: 25,
            offset: 0,
          };
      for (const field of form().elements) field.disabled = true;
      result = await api("/moments/search", "POST", query);
      lastQuery = query;
      $("#moments-status").textContent =
        `${result.total} matching moments · ${result.coverage.indexed_recordings} recordings with indexed moments / ${result.coverage.inventoried_recordings} inventoried. ${result.notice}`;
      $("#moments-results").innerHTML = result.moments.length
        ? result.moments
            .map(
              (m) =>
                `<article class="batch-card"><div class="batch-heading"><strong>${escapeHTML(names[m.kind])}</strong><span class="pill ${m.acceptance === "human_confirmed" ? "" : "amber"}">${escapeHTML(m.acceptance.replaceAll("_", " "))}</span></div><h3>${escapeHTML(m.title)}</h3><p>${escapeHTML(m.source_title)} · sheet ${m.sheet || "unknown"}<br>${stamp(m.start_seconds)}–${stamp(m.end_seconds)}${m.earlier_inputs ? " · earlier source or analysis inputs" : ""}</p><p class="fine-print">${escapeHTML(m.notice)}</p><div class="batch-actions"><button class="button secondary" data-open-moment="${escapeHTML(m.id)}">Inspect in workbench →</button>${m.start_youtube_url ? `<a class="evidence-link" href="${escapeHTML(m.start_youtube_url)}" target="_blank" rel="noopener">Check ${stamp(m.start_seconds)} on YouTube ↗</a>` : ""}${m.end_youtube_url && m.end_seconds !== m.start_seconds ? `<a class="evidence-link" href="${escapeHTML(m.end_youtube_url)}" target="_blank" rel="noopener">Check ${stamp(m.end_seconds)} on YouTube ↗</a>` : ""}</div><details><summary>${m.evidence.length} retained supporting/context frames</summary>${m.evidence.map((e) => `<p class="fine-print">${stamp(e.source_seconds)}${e.youtube_url ? ` · <a href="${escapeHTML(e.youtube_url)}" target="_blank" rel="noopener">YouTube ↗</a>` : ""}</p>`).join("")}${m.model_version ? `<p class="fine-print">Method: ${escapeHTML(m.model_version)}</p>` : ""}</details></article>`,
            )
            .join("")
        : "<p>No retained moment matches these filters. Try another kind or a wider date range; unprocessed footage remains unknown.</p>";
      $("#moment-page").textContent = result.total
        ? `Results ${offset + 1}–${offset + result.moments.length} of ${result.total}`
        : "";
      $("#moment-next").disabled = result.next_offset === null;
      $("#moment-download").disabled = false;
    } catch (error) {
      result = null;
      $("#moments-results").textContent = "";
      $("#moment-page").textContent = "";
      $("#moments-status").textContent = error.message;
    } finally {
      pending = false;
      for (const field of form().elements) field.disabled = false;
    }
  }
  form().addEventListener("submit", (event) => {
    event.preventDefault();
    search();
  });
  // Editing filters invalidates the old page continuation; start a new search.
  form().addEventListener("input", () => {
    $("#moment-next").disabled = true;
  });
  form().addEventListener("change", () => {
    $("#moment-next").disabled = true;
  });
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.id === "moments-button") {
      try {
        await refresh();
      } catch (error) {
        toast(error.message, true);
        return;
      }
      const previous = form().elements.source.value;
      form().elements.source.innerHTML =
        '<option value="">All recordings</option>' +
        state.data.sources
          .map(
            (s) =>
              `<option value="${escapeHTML(s.id)}">${escapeHTML(s.title)} · ${escapeHTML(s.platform_id || s.id)}</option>`,
          )
          .join("");
      form().elements.source.value = previous;
      $("#moments-dialog").showModal();
      await search();
    }
    if (button.id === "moment-next" && result && result.next_offset !== null)
      await search(result.next_offset);
    if (button.id === "moment-download" && result) {
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(result, null, 2)], {
          type: "application/json",
        }),
      );
      const a = document.createElement("a");
      a.href = url;
      a.download = `hogline-moments-${result.index_sha256.slice(0, 12)}-page-${result.query.offset}.json`;
      a.click();
      URL.revokeObjectURL(url);
    }
    if (button.dataset.openMoment) {
      const moment = result?.moments.find(
        (m) => m.id === button.dataset.openMoment,
      );
      if (!moment) return;
      if (state.draft)
        return toast(
          "Save or close the current game review before opening another moment.",
          true,
        );
      if (window.hoglineLabels && !window.hoglineLabels.beforeNavigate())
        return;
      state.source = moment.source_id;
      state.seconds = moment.start_seconds;
      state.view = moment.view;
      $("#moments-dialog").close();
      render();
      // Open a precise fragment/board comparison when one exists; otherwise use the evidence viewer.
      if (moment.reference.board_change_id)
        document
          .querySelector(
            `[data-board-change="${CSS.escape(moment.reference.board_change_id)}"]`,
          )
          ?.click();
      else if (moment.reference.track_result_id)
        await window.hoglineTracks.open(
          moment.reference.window_id,
          moment.reference.track_id,
          moment.reference.track_result_id,
        );
      else
        $("#frame-stage").scrollIntoView({
          block: "center",
          behavior: "smooth",
        });
    }
  });
})();
