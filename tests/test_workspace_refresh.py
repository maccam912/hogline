"""Exercise the browser's real merge code with out-of-order polling responses."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_progress_survives_independent_overview_and_detail_refreshes():
    node = shutil.which("node")
    if not node:
        pytest.skip("Browser state regression requires Node.js")
    script = Path(__file__).parents[1] / "hogline/review/static/app.js"
    # Bootstrap only the state and merge functions; no DOM or network is needed.
    code = script.read_text().split("async function refreshSelectedWorkspace()", 1)[0]
    scenario = r"""
const assert = require('node:assert/strict');
state.source = 'recording';
const summary = (revision, batchState = 'active') => ({
  id: 'batch', revision, state: batchState, summary_only: true,
  units: [], executions: [], phase_history: [], succeeded_units: null,
  request: {source_ids: ['recording']}, phase: 'survey', limits: {},
});
const detail = (revision, fraction) => ({
  ...summary(revision), summary_only: false, units: [{job_id: 'job'}],
  succeeded_units: 1, progress_summary: {overall_fraction: fraction,
    completed_tasks: 1, total_tasks: 4, live_worker_updates: ['2026-11-07T22:00:00Z']},
});
archiveOverview = {batches: [summary(1)], jobs: []};
selectedWorkspace = {source_id: 'recording', batches: [detail(1, .25)], jobs: []};
applyWorkspace();
assert.equal(state.data.batches[0].progress_summary.overall_fraction, .25);
// Every new overview used to discard the detailed card and its progress bar.
archiveOverview.batches = [summary(2)];
applyWorkspace();
assert.equal(state.data.batches[0].summary_only, false);
assert.equal(state.data.batches[0].units.length, 1);
assert.equal(state.data.batches[0].progress_summary.overall_fraction, .25);
assert.equal(state.data.batches[0].progress_refreshing, true);
// Newer details must render even when the overview cache still has an older revision.
selectedWorkspace.batches = [detail(3, .5)];
applyWorkspace();
assert.equal(state.data.batches[0].progress_summary.overall_fraction, .5);
assert.equal(state.data.batches[0].progress_refreshing, undefined);
// New control states take effect immediately without removing progress.
archiveOverview.batches = [summary(4, 'paused')];
applyWorkspace();
assert.equal(state.data.batches[0].state, 'paused');
assert.equal(state.data.batches[0].progress_summary.overall_fraction, .5);
assert.deepEqual(state.data.batches[0].progress_summary.live_worker_updates, []);
archiveOverview.batches = [summary(5, 'completed')];
applyWorkspace();
assert.equal(state.data.batches[0].state, 'completed');
assert.equal(state.data.batches[0].progress_summary.overall_fraction, 1);
// Cleared analyses and other recordings must not regain cached details.
archiveOverview.batches = [];
applyWorkspace();
assert.equal(state.data.batches.length, 0);
archiveOverview.batches = [summary(6)];
state.source = 'other';
applyWorkspace();
assert.equal(state.data.batches[0].summary_only, true);
"""
    result = subprocess.run(
        [node, "-e", "global.localStorage = {getItem: () => null};\n" + code + scenario],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
