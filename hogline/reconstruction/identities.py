"""Cross-game face identities: which anonymous game players are probably one person.

Every game player carries one face template pooled over many faces. Players from different
games are linked by average-linkage clustering of those templates. Hard constraints come
from the data and from operators: two players of the same game are never one person, two
named people are never one person, and a pair an operator separated stays apart. Identities
are proposals for browsing; names remain operator decisions.
"""

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial.distance import pdist

VERSION = "cross-game-face-identities-v1"
BLOCK = 1024


def _conflicted(members, keys):
    seen = set()
    for m in members:
        if keys[m] & seen:
            return True
        seen |= keys[m]
    return False


def _partition(members, vectors, keys, weights, threshold):
    """Greedy constrained assignment, heaviest first; unmatched nodes stand alone."""
    parts, used, sums = [], [], []
    for m in sorted(members, key=lambda m: (-weights[m], m)):
        best, score = None, threshold
        for n, total in enumerate(sums):
            if keys[m] & used[n]:
                continue
            similarity = float(vectors[m] @ (total / max(np.linalg.norm(total), 1e-12)))
            if similarity >= score:
                best, score = n, similarity
        if best is None:
            parts.append([m])
            used.append(set(keys[m]))
            sums.append(vectors[m].astype(np.float64) * weights[m])
        else:
            parts[best].append(m)
            used[best] |= keys[m]
            sums[best] = sums[best] + vectors[m] * weights[m]
    return parts


def _split(members, vectors, keys, weights, threshold, cut, depth=0):
    """Tighten a cluster holding conflicting nodes; partition greedily if that fails."""
    if len(members) < 2 or not _conflicted(members, keys):
        return [members]
    if depth < 4 and len(members) > 2:
        tighter = cut * 0.8
        labels = fcluster(linkage(pdist(vectors[members], "cosine"), "average"), tighter, "distance")
        if len(set(labels)) > 1:
            out = []
            for label in sorted(set(labels)):
                part = [m for m, lab in zip(members, labels, strict=True) if lab == label]
                out += _split(part, vectors, keys, weights, threshold, tighter, depth + 1)
            return out
    return _partition(members, vectors, keys, weights, threshold)


def link_identities(vectors, keys, weights, threshold):
    """Return one identity label per node, numbered by total weight (largest first).

    `vectors` are unit templates, `keys` sets of exclusive keys (two nodes sharing a key can
    never be one identity) and `weights` positive face counts. Two nodes join only through a
    chain of average similarities at or above `threshold`. Average-linkage clusters at a cut
    lie inside the single-linkage components at that cut, so linkage runs per component.
    """
    vectors = np.asarray(vectors, dtype=np.float32)
    count = len(vectors)
    if count == 0:
        return np.zeros(0, dtype=np.int64)
    rows, cols = [], []
    for start in range(0, count, BLOCK):
        similarity = vectors[start : start + BLOCK] @ vectors.T
        i, j = np.nonzero(similarity >= threshold)
        i = i + start
        keep = j > i
        rows.append(i[keep])
        cols.append(j[keep])
    rows, cols = np.concatenate(rows), np.concatenate(cols)
    graph = coo_matrix((np.ones(len(rows), dtype=np.int8), (rows, cols)), shape=(count, count))
    _, component = connected_components(graph, directed=False)
    cut = 1.0 - threshold
    groups = []
    for label in np.unique(component):
        members = np.flatnonzero(component == label).tolist()
        if len(members) == 1:
            groups.append(members)
            continue
        first = fcluster(linkage(pdist(vectors[members], "cosine"), "average"), cut, "distance")
        for sub in sorted(set(first)):
            part = [m for m, lab in zip(members, first, strict=True) if lab == sub]
            groups += _split(part, vectors, keys, weights, threshold, cut)
    groups.sort(key=lambda g: (-sum(weights[m] for m in g), min(g)))
    labels = np.full(count, -1, dtype=np.int64)
    for number, members in enumerate(groups):
        labels[members] = number
    return labels


def pooled(vectors, weights):
    total = (np.asarray(vectors, dtype=np.float64) * np.asarray(weights, dtype=np.float64)[:, None]).sum(0)
    norm = np.linalg.norm(total)
    if not norm or not np.isfinite(norm):
        raise ValueError("Cannot pool empty or invalid templates")
    return (total / norm).astype(np.float32)
