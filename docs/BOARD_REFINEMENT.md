# Narrowing a board-change gap

Open **Posted board changes → Compare board evidence → Narrow this gap automatically**. The preview shows a few concrete short source intervals, their YouTube links, and one shared storage/worker allowance. Starting it queues the selected windows and then automatically reads only their retained board/clock frames.

The largest remaining gap is sampled first, at its midpoint. Each chosen window is subtracted before selecting another, so samples spread across the uncertainty interval. Compatible completed and running windows are excluded from another decode. Completed windows with unread anchors can be reused for OCR. No other batch's running work is taken over. Failed identical work is reported in the exclusions; candidate exploration is bounded to 128 attempts per preview.

This selection differs from an activity campaign: it does not require another coarse scan or read every cached image in the recording. It uses at most eight windows, each 4–30 seconds, and at most 120 OCR anchors under the same cumulative budget. Defaults are three ten-second windows, 96 MiB retained growth, and 300 worker seconds. Pausing, resuming, retrying, and cancelling use the ordinary Processing controls. The request, original board-change evidence, source/method pins, selected intervals, phase history, and costs remain in the batch record.

The timeline is recomputed from the resulting proposed readings. New evidence may narrow a bracket, expose several separate card changes, or leave it unresolved because the board remains unreadable. It never establishes the exact time an end was played. No automatic follow-up runs are started: choose a current gap for another bounded refinement. Earlier screenshots, observations, and released datasets remain historical evidence.

```sh
uv run hogline refine-board CHANGE_ID --windows 3
uv run hogline refine-board CHANGE_ID --windows 3 --max-growth-mb 96 --max-worker-seconds 300 --enqueue
uv run hogline work
```

The UI uses `POST /api/board-refinement/preview` and `POST /api/board-refinement/batches`. Publication rechecks the reviewed plan hash. A changed timeline, source manifest, or matching work invalidates the preview. Current event IDs are available in `broadcast.board_timeline.changes` from the catalog or JSON export.

Tests cover deterministic gap selection, no side effects from preview, stale previews, a too-small gap, preserving other batches' work, reuse of unread anchors, and excluding unrelated cached frames from OCR. Real-media checks measure orchestration and evidence quality; they do not establish held-out detection precision.

A browser-started real Sheet 4 refinement sampled three ten-second windows inside the 330–7395-second bracket. Its three decodes and ten retained-frame OCR units completed in 37.34 worker seconds with 24.28 MiB reserved. The resulting timeline separated the original broad interval into three proposed card-change brackets of approximately 29 minutes each. The red end-5 card became readable between source times 3867 and 5626.267 seconds; before/after crops and exact source links were checked in the browser. Those timestamps bound a posted-card change, not when end 5 finished.
