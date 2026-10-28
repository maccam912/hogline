(() => {
  const data = () =>
    state.data.broadcast?.board_timeline || { epochs: [], changes: [] };
  const stamp = (n) =>
    time(n) +
    (Number.isInteger(n) ? "" : (n % 1).toFixed(3).slice(1).replace(/0+$/, ""));
  const cards = (epoch) =>
    epoch.cards.length
      ? epoch.cards
          .map((c) => `end ${c.end_number} at column ${c.cumulative_points}`)
          .join(" · ")
      : "No readable cards; score unknown";
  const titles = {
    cards_added: "Cards added",
    cards_removed: "Cards no longer read",
    cards_repositioned: "Card positions changed",
    cards_appeared: "Cards became readable",
    no_readable_cards: "Cards are no longer readable",
    mixed_card_change: "Card readings changed",
  };
  function renderTimeline() {
    const timeline = data(),
      epochs = timeline.epochs.filter((e) => e.source_id === state.source),
      changes = timeline.changes
        .filter((e) => e.source_id === state.source)
        .sort((a, b) => b.after.source_seconds - a.after.source_seconds);
    $("#board-change-count").textContent = changes.length;
    $("#board-change-list").innerHTML = changes.length
      ? changes
          .slice(0, 50)
          .map(
            (change) =>
              `<article class="activity-card"><div><strong>${escapeHTML(change.color)} · ${titles[change.change_type]}</strong>
      <p class="fine-print">Between ${stamp(change.before.source_seconds)} and ${stamp(change.after.source_seconds)} · ${time(change.bracket_seconds)} uncertainty interval${change.unusable_intervening_readings ? ` · ${change.unusable_intervening_readings} unreadable intervening samples` : ""}</p></div>
      <button class="button secondary" data-board-change="${change.id}">Compare board evidence →</button></article>`,
          )
          .join("")
      : '<p class="fine-print">No change has two repeated, readable board states yet. Three distinct frames spanning at least two seconds are required for each row state. Unreadable or unpinned evidence cannot fill a gap.</p>';
    if (changes.length > 50)
      $("#board-change-list").innerHTML +=
        `<p class="fine-print">Showing the latest 50 of ${changes.length} changes. The dataset export retains all changes.</p>`;
    $("#board-epochs").innerHTML =
      `<summary>Repeated board readings · ${epochs.length} row states</summary>${epochs
        .map(
          (epoch) =>
            `<p class="fine-print"><strong>${escapeHTML(epoch.color)} · ${stamp(epoch.first.source_seconds)}–${stamp(epoch.last.source_seconds)}</strong><br>${escapeHTML(cards(epoch))} · ${epoch.evidence_ids.length} frames${epoch.first.youtube_url ? ` · <a href="${escapeHTML(epoch.first.youtube_url)}" target="_blank" rel="noopener">Check first observation ↗</a>` : ""}</p>`,
        )
        .join("")}`;
  }
  function compare(id) {
    const timeline = data(),
      change = timeline.changes.find((c) => c.id === id);
    if (!change)
      return toast(
        "This timeline changed. Reopen the evidence comparison.",
        true,
      );
    const before = timeline.epochs.find((e) => e.id === change.before_epoch_id),
      after = timeline.epochs.find((e) => e.id === change.after_epoch_id);
    $("#board-change-comparison").innerHTML =
      `<h3>${escapeHTML(change.color)} · ${titles[change.change_type]}</h3>
      <p>The last repeated earlier reading was at <strong>${stamp(change.before.source_seconds)}</strong>; the first repeated later reading was at <strong>${stamp(change.after.source_seconds)}</strong>. The change could be anywhere inside that ${time(change.bracket_seconds)} interval.</p>
      <p class="fine-print">Posted cards can lag play, be obscured, be corrected, or be reset. This does not identify an end completion, a final result, or a new game.${change.continuity !== "sampled_only" ? ` Evidence continuity: ${escapeHTML(change.continuity.replaceAll("_", " "))}.` : " Frames are discrete samples, not continuous coverage."}</p>
      ${[
        ["Last earlier reading", change.before, before],
        ["First later reading", change.after, after],
      ]
        .map(([caption, anchor, epoch]) => {
          const obs = state.data.broadcast.observations.find(
            (o) => o.id === anchor.observation_id,
          );
          return `<figure class="boundary-frame"><figcaption><strong>${caption} · ${stamp(anchor.source_seconds)}</strong><span class="fine-print">${escapeHTML(cards(epoch))}<br>Repeated in ${epoch.evidence_ids.length} frames from ${stamp(epoch.first.source_seconds)} to ${stamp(epoch.last.source_seconds)}.</span></figcaption>
          ${obs?.crops?.scoreboard ? `<img src="/api/broadcast/${encodeURIComponent(obs.id)}/scoreboard" alt="${caption} scoreboard crop">` : ""}
          <details><summary>Full original frame</summary><img src="/api/evidence/${encodeURIComponent(anchor.evidence_id)}/composite" alt="${caption} original source frame"></details>
          ${anchor.youtube_url ? `<a class="evidence-link" href="${escapeHTML(anchor.youtube_url)}" target="_blank" rel="noopener">Double-check ${stamp(anchor.source_seconds)} on YouTube ↗</a>` : ""}</figure>`;
        })
        .join("")}`;
    $("#board-change-comparison").innerHTML +=
      `<button class="button primary" data-refine-board="${change.id}">Narrow this gap automatically →</button>`;
    $("#board-change-dialog").showModal();
  }
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (button?.dataset.boardChange) compare(button.dataset.boardChange);
  });
  window.hoglineBoardTimeline = { render: renderTimeline };
  renderTimeline();
})();
