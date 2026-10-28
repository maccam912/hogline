(() => {
  let data = null,
    selected = null,
    dirty = false;
  const form = () => $("#score-disagreement-form");
  const close = () => {
    if (dirty) {
      toast("Save or discard the discrepancy review before closing.", true);
      return;
    }
    $("#score-disagreements-dialog").close();
  };
  function renderCount() {
    $("#score-disagreement-count").textContent =
      state.data.score_disagreements?.open_count || 0;
  }
  const score = (s) => `${s.red_points} red · ${s.yellow_points} yellow`;
  const link = (url, label) =>
    url
      ? `<a href="${escapeHTML(url)}" target="_blank" rel="noopener">${escapeHTML(label)} ↗</a>`
      : escapeHTML(label);
  function renderQueue() {
    $("#score-disagreements-list").innerHTML = data.conflicts.length
      ? data.conflicts
          .map(
            (c) =>
              `<article class="batch-card"><div class="batch-heading"><strong>${escapeHTML(c.game_label)} · end ${c.official_number}</strong><span class="pill ${c.review_state === "open" ? "amber" : ""}">${c.review_state}</span></div><p>Saved: <strong>${score(c.saved_score)}</strong> · ${escapeHTML(c.saved_score.acceptance.replaceAll("_", " "))}</p><p class="fine-print">${escapeHTML(c.notice)}</p><details><summary>Saved score evidence</summary>${c.saved_score.evidence_ids
                .map((id) => {
                  const e = state.data.evidence.find((f) => f.id === id);
                  return `<p>${e ? link(e.youtube_url, time(e.source_seconds)) : "Frame reference unavailable"}</p>`;
                })
                .join("")}</details>${c.proposals
                .map(
                  (p) =>
                    `<div class="window-candidate"><strong>Board proposal: ${score(p)}${p.agrees_with_saved ? " · agrees with saved score" : " · differs"}</strong><p class="fine-print">${escapeHTML(state.data.sources.find((s) => s.id === p.source_id)?.title || p.source_id)} · ${time(p.source_start_seconds)}–${time(p.source_end_seconds)} · ${p.evidence_ids.length} frames</p><p>${p.youtube_links
                      .filter(Boolean)
                      .map((url, i) => link(url, `Evidence ${i + 1}`))
                      .join(" · ")}</p></div>`,
                )
                .join(
                  "",
                )}${c.latest_review ? `<p class="fine-print">Latest decision: ${escapeHTML(c.latest_review.decision.replaceAll("_", " "))} · ${escapeHTML(c.latest_review.author)}<br>${escapeHTML(c.latest_review.reason)}</p>` : ""}<div class="batch-actions"><button class="button secondary" data-disagreement-review="${c.id}">Record review decision →</button><button class="button quiet" data-disagreement-game="${escapeHTML(c.game_id)}">Open game review →</button></div></article>`,
          )
          .join("")
      : "<p>No current board proposal disagrees with a saved, numbered end inside its linked source span. Missing or unreadable boards cannot establish agreement.</p>";
    $("#score-disagreements-history").innerHTML =
      `<summary>Decision history · ${data.reviews.length}</summary>${[
        ...data.reviews,
      ]
        .reverse()
        .map(
          (r) =>
            `<p class="fine-print"><strong>${escapeHTML(r.conflict.game_label)} · end ${r.conflict.official_number}</strong><br>${escapeHTML(r.decision.replaceAll("_", " "))} · ${escapeHTML(r.author)} · ${escapeHTML(r.created_at)}<br>${escapeHTML(r.reason)}<br>Game revision ${r.conflict.game_revision} · discrepancy review ${r.review_revision}</p>`,
        )
        .join("")}`;
  }
  async function load() {
    data = await api("/score-disagreements");
    renderQueue();
  }
  function reset() {
    selected = null;
    dirty = false;
    form().reset();
    form().hidden = true;
  }
  form().addEventListener("input", () => {
    dirty = true;
  });
  form().addEventListener("change", () => {
    dirty = true;
  });
  form().addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!selected) return;
    const button = event.submitter;
    button.disabled = true;
    try {
      const values = new FormData(form());
      await api(`/score-disagreements/${selected.id}/reviews`, "POST", {
        expected_review_revision: selected.review_revision,
        decision: values.get("decision"),
        author: values.get("author"),
        reason: values.get("reason"),
      });
      reset();
      await load();
      await refresh();
      toast(
        "Review recorded. The saved score and its acceptance are unchanged.",
      );
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  $("#score-disagreements-dialog").addEventListener("cancel", (event) => {
    if (dirty) {
      event.preventDefault();
      toast("Save or discard the discrepancy review before closing.", true);
    }
  });
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    try {
      if (button.id === "score-disagreements-button") {
        await refresh();
        await load();
        $("#score-disagreements-dialog").showModal();
      }
      if (button.id === "close-score-disagreements") close();
      if (button.id === "discard-score-disagreement") reset();
      if (button.id === "reload-score-disagreements") {
        if (dirty)
          return toast(
            "Save or discard the current review before refreshing.",
            true,
          );
        reset();
        await refresh();
        await load();
      }
      if (button.dataset.disagreementReview) {
        if (dirty)
          return toast(
            "Save or discard the current review before choosing another.",
            true,
          );
        selected = data.conflicts.find(
          (c) => c.id === button.dataset.disagreementReview,
        );
        if (!selected) return;
        form().reset();
        form().hidden = false;
        $("#score-disagreement-selection").textContent =
          `${selected.game_label} · end ${selected.official_number} · saved ${score(selected.saved_score)}`;
        form().scrollIntoView({ block: "center", behavior: "smooth" });
      }
      if (button.dataset.disagreementGame) {
        if (dirty)
          return toast(
            "Save or discard the discrepancy review before opening the game.",
            true,
          );
        if (state.draft)
          return toast("Save or close the current game draft first.", true);
        if (window.hoglineLabels && !window.hoglineLabels.beforeNavigate())
          return;
        const game = state.data.games.find(
          (g) => g.id === button.dataset.disagreementGame,
        );
        if (!game) return toast("This game changed. Refresh the list.", true);
        state.source = game.spans[0].source_id;
        close();
        editGame(game.id);
        $("#game-editor").scrollIntoView({
          block: "start",
          behavior: "smooth",
        });
      }
    } catch (error) {
      toast(error.message, true);
    }
  });
  window.hoglineScoreConflicts = { render: renderCount };
})();
