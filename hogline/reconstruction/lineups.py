"""Within-game grouping of face observations into anonymous game players.

Pass one groups individual faces (average linkage on face embeddings, optionally penalised
by clothing difference). Pass two merges the resulting groups when their averaged faces
match and they are (almost) never seen in the same frame: one player's close-up and distant
faces, or head-down frames, otherwise form separate groups. Two faces seen in the same view
at the same instant are never the same person. Groups remain proposals.
"""

import cv2
import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import pdist, squareform

VERSION = "within-game-face-groups-v2"
DEFAULTS = {
    "distance_threshold": 0.55,
    "clothing_weight": 0.0,
    "max_grouped_faces": 6000,
    "attach_similarity": 0.5,
    "merge_similarity": 0.7,
    "merge_min_faces": 10,
    "merge_cooccurrence_tolerance": 0.01,
    "min_player_faces": 5,
    "min_player_span_seconds": 60.0,
}


def frame_key(face):
    return (face["segment_id"], face["sample_index"], face["view"])


def _hellinger_condensed(histograms):
    roots = np.sqrt(np.clip(np.asarray(histograms, dtype=np.float64), 0, None))
    roots /= np.maximum(np.linalg.norm(roots, axis=1, keepdims=True), 1e-12)
    return np.sqrt(np.clip(1 - squareform(roots @ roots.T, checks=False), 0, 1))


def _partition(indices, embeddings, meta):
    """Greedy cannot-link partition: each frame contributes at most one face per group."""
    parts, keys, sums = [], [], []
    for i in sorted(indices, key=lambda i: (-meta[i]["eye_px"], i)):
        key, best, score = frame_key(meta[i]), None, -2.0
        for n, total in enumerate(sums):
            if key in keys[n]:
                continue
            similarity = float(embeddings[i] @ (total / max(np.linalg.norm(total), 1e-12)))
            if similarity > score:
                best, score = n, similarity
        if best is None:
            parts.append([i])
            keys.append({key})
            sums.append(embeddings[i].astype(np.float64))
        else:
            parts[best].append(i)
            keys[best].add(key)
            sums[best] = sums[best] + embeddings[i]
    return [sorted(part) for part in parts]


def _split_conflicts(indices, embeddings, meta, threshold, depth=0):
    """Tighten a group holding two simultaneous faces from one view; partition if that fails."""
    keys = [frame_key(meta[i]) for i in indices]
    if len(set(keys)) == len(keys) or len(indices) < 2:
        return [indices], 0
    if depth < 3:
        labels = fcluster(
            linkage(pdist(embeddings[indices], "cosine"), "average"), threshold * 0.8, "distance"
        )
        if len(set(labels)) > 1:
            groups, splits = [], 1
            for label in sorted(set(labels)):
                part = [i for i, lab in zip(indices, labels, strict=True) if lab == label]
                more, count = _split_conflicts(part, embeddings, meta, threshold * 0.8, depth + 1)
                groups += more
                splits += count
            return groups, splits
    return _partition(indices, embeddings, meta), 1


def merge_groups(labels, embeddings, meta, params):
    """Constrained average linkage over group templates; returns new labels and merge count."""
    count = int(labels.max()) + 1 if len(labels) and labels.max() >= 0 else 0
    members = [np.flatnonzero(labels == g) for g in range(count)]
    eligible = [g for g in range(count) if len(members[g]) >= params["merge_min_faces"]]
    if len(eligible) < 2:
        return labels, 0
    # Face-weighted sums: a merged cluster is compared through its own pooled template.
    sums = [embeddings[members[g]].astype(np.float64).sum(0) for g in eligible]
    frames = [{frame_key(meta[i]) for i in members[g]} for g in eligible]
    size = [len(members[g]) for g in eligible]
    k = len(eligible)
    blocked = np.zeros((k, k), dtype=bool)
    for a in range(k):
        for b in range(a + 1, k):
            shared = len(frames[a] & frames[b])
            if shared > max(2, params["merge_cooccurrence_tolerance"] * min(size[a], size[b])):
                blocked[a, b] = blocked[b, a] = True
    clusters = [[a] for a in range(k)]
    pooled = [vector.copy() for vector in sums]
    merged = 0
    while True:
        unit = np.stack([v / max(np.linalg.norm(v), 1e-12) for v in pooled])
        similarity = unit @ unit.T
        best, pair = params["merge_similarity"], None
        for x in range(len(clusters)):
            for y in range(x + 1, len(clusters)):
                if similarity[x, y] >= best and not blocked[np.ix_(clusters[x], clusters[y])].any():
                    best, pair = similarity[x, y], (x, y)
        if pair is None:
            break
        x, y = pair
        clusters[x] += clusters.pop(y)
        pooled[x] = pooled[x] + pooled.pop(y)
        merged += 1
    if not merged:
        return labels, 0
    target = {}
    for cluster in clusters:
        keep = min(eligible[a] for a in cluster)
        for a in cluster:
            target[eligible[a]] = keep
    remapped = np.array([target.get(int(lab), int(lab)) if lab >= 0 else -1 for lab in labels])
    order = sorted({int(lab) for lab in remapped if lab >= 0}, key=lambda g: (-(remapped == g).sum(), g))
    renumber = {g: n for n, g in enumerate(order)}
    return np.array([renumber[int(lab)] if lab >= 0 else -1 for lab in remapped], dtype=np.int64), merged


def group_faces(embeddings, meta, params=None):
    """Return a group label per face (-1 = unattached) and diagnostics.

    `meta` items need segment_id, sample_index, view, eye_px and, for clothing, a
    torso_hsv_histogram. Input order must be stable; the result is deterministic.
    """
    params = {**DEFAULTS, **(params or {})}
    embeddings = np.asarray(embeddings, dtype=np.float32)
    count = len(meta)
    if embeddings.shape[0] != count:
        raise ValueError("Face embeddings and metadata disagree")
    labels = np.full(count, -1, dtype=np.int64)
    if count == 0:
        return labels, {
            "faces": 0,
            "grouped": 0,
            "attached": 0,
            "unattached": 0,
            "simultaneous_face_splits": 0,
        }
    order = sorted(range(count), key=lambda i: (-meta[i]["eye_px"], i))
    core = sorted(order[: params["max_grouped_faces"]])
    if len(core) == 1:
        groups, conflicts = [core], 0
    else:
        distances = pdist(embeddings[core], "cosine")
        histograms = [meta[i].get("torso_hsv_histogram") for i in core]
        if params["clothing_weight"] > 0 and all(h is not None for h in histograms):
            distances = distances + params["clothing_weight"] * _hellinger_condensed(histograms)
        first = fcluster(linkage(distances, "average"), params["distance_threshold"], "distance")
        groups, conflicts = [], 0
        for label in sorted(set(first)):
            members = [i for i, lab in zip(core, first, strict=True) if lab == label]
            parts, splits = _split_conflicts(members, embeddings, meta, params["distance_threshold"])
            groups += parts
            conflicts += splits
    groups.sort(key=lambda g: (-len(g), min(g)))
    for number, members in enumerate(groups):
        labels[members] = number
    attached = 0
    rest = sorted(set(range(count)) - set(core))
    if rest:
        centroids = np.stack([embeddings[g].mean(0) for g in groups])
        centroids /= np.maximum(np.linalg.norm(centroids, axis=1, keepdims=True), 1e-12)
        occupied = [{frame_key(meta[i]) for i in g} for g in groups]
        similarity = embeddings[rest] @ centroids.T
        for row, face in enumerate(rest):
            for group in np.argsort(-similarity[row]):
                if similarity[row, group] < params["attach_similarity"]:
                    break
                if frame_key(meta[face]) not in occupied[group]:
                    labels[face] = group
                    occupied[group].add(frame_key(meta[face]))
                    attached += 1
                    break
    labels, merged = merge_groups(labels, embeddings, meta, params)
    return labels, {
        "faces": count,
        "fragments_merged": merged,
        "grouped": len(core),
        "attached": attached,
        "unattached": int((labels < 0).sum()),
        "simultaneous_face_splits": conflicts,
        "groups": int(labels.max()) + 1 if (labels >= 0).any() else 0,
    }


def template(embeddings, weights=None):
    vectors = np.asarray(embeddings, dtype=np.float64)
    weights = np.ones(len(vectors)) if weights is None else np.asarray(weights, dtype=np.float64)
    mean = (vectors * weights[:, None]).sum(0) / max(weights.sum(), 1e-12)
    norm = np.linalg.norm(mean)
    if not norm or not np.isfinite(norm):
        raise ValueError("Cannot form a template from empty or invalid embeddings")
    return (mean / norm).astype(np.float32)


PORTRAIT_VERSION = "clear-plausible-face-v1"
PORTRAIT_MIN_SIMILARITY = 0.4
THUMBNAIL_POLICY = "diverse-clear-faces-v1"


def face_quality(face):
    """Larger, frontal, sharper and more confident faces make better thumbnails.

    `yaw_proxy` is the nose offset from the eye midpoint in eye-distance units: about 0 for a
    frontal face and beyond ±0.5 in profile.
    """
    frontal = max(0.25, 1.0 - abs(face.get("yaw_proxy") or 0.0) / 0.6)
    sharp = min(1.0, 0.5 + (face.get("sharpness") or 0.0) / 400.0)
    return float(face["eye_px"]) * frontal * sharp * float(face.get("raw_detector_score") or 1.0)


def select_thumbnails(meta, embeddings, limit, per_person=8, same_similarity=0.5):
    """Indices of faces whose thumbnails a segment keeps, best first, spread across people.

    Picking only the largest faces lets whoever stands nearest a camera take every slot.
    Here a face is skipped once `per_person` kept faces already resemble it, so each person
    in the segment contributes a few clear faces. Leftover slots go to the next best faces.
    """
    order = sorted(range(len(meta)), key=lambda i: (-face_quality(meta[i]), i))
    if len(order) <= limit:
        return order
    vectors = np.asarray(embeddings, dtype=np.float32).reshape(len(meta), -1)
    kept, skipped = [], []
    for i in order:
        if len(kept) >= limit:
            break
        if kept and int((vectors[kept] @ vectors[i] >= same_similarity).sum()) >= per_person:
            skipped.append(i)
            continue
        kept.append(i)
    return kept + skipped[: limit - len(kept)]


def thumbnail_defects(tile):
    """(border, sharpness) of one aligned RGB thumbnail.

    `border` is the share of black fill that alignment adds when a head touches the frame
    edge; `sharpness` is the variance of OpenCV's Laplacian on luminance.
    """
    tile = np.ascontiguousarray(tile, dtype=np.uint8)
    border = float((tile.max(axis=2) < 10).mean())
    gray = cv2.cvtColor(tile, cv2.COLOR_RGB2GRAY)
    return border, float(cv2.Laplacian(gray, cv2.CV_64F).var())


def portrait_face(indices, meta, embeddings, reference, thumbnail=None):
    """The face that best shows one group to a person: large, frontal, sharp and really them.

    Close-up faces embed a little differently from the many distant ones (October 30 2026:
    the largest face of each real player scored 0.5–0.7 against its group template), so
    similarity only rejects strays below 0.4. Among the rest, larger, frontal, sharper faces
    win; black frame-edge fill is penalised. `thumbnail(i)` returns face i's stored image.
    Returns (index, template similarity), or None without thumbnailed faces.
    """
    candidates = [i for i in indices if meta[i].get("thumb", -1) >= 0]
    if not candidates:
        return None
    vectors = np.asarray(embeddings, dtype=np.float32)
    similarity = {i: float(vectors[i] @ np.asarray(reference, dtype=np.float32)) for i in candidates}
    plausible = [i for i in candidates if similarity[i] >= PORTRAIT_MIN_SIMILARITY]
    if not plausible:
        best = max(candidates, key=lambda i: (similarity[i], -i))
        return best, similarity[best]

    def score(i):
        face = meta[i]
        frontal = max(0.25, 1.0 - abs(face.get("yaw_proxy") or 0.0) / 0.6)
        value = min(float(face["eye_px"]), 30.0) * frontal * (0.5 + similarity[i])
        if thumbnail is not None:
            border, sharpness = thumbnail_defects(thumbnail(i))
            value *= max(0.0, 1.0 - 4.0 * border) * min(1.0, 0.4 + sharpness / 300.0)
        return value

    best = max(plausible, key=lambda i: (score(i), -i))
    return best, similarity[best]


def representative_faces(indices, meta, limit=12):
    """Prefer large faces, spread across the game, with stored thumbnails."""
    candidates = sorted(
        (i for i in indices if meta[i].get("thumb", -1) >= 0),
        key=lambda i: (-meta[i]["eye_px"], meta[i]["source_seconds"]),
    )
    if len(candidates) <= limit:
        return candidates
    times = np.array([meta[i]["source_seconds"] for i in candidates])
    edges = np.linspace(times.min(), times.max() + 1e-6, limit + 1)
    chosen = []
    for left, right in zip(edges, edges[1:], strict=False):
        best = next(
            (i for i in candidates if left <= meta[i]["source_seconds"] < right and i not in chosen), None
        )
        if best is not None:
            chosen.append(best)
    for i in candidates:
        if len(chosen) >= limit:
            break
        if i not in chosen:
            chosen.append(i)
    return sorted(chosen, key=lambda i: -meta[i]["eye_px"])


def classify_players(groups, span_seconds, params):
    """Rank groups against this game's own large groups; flag one-camera edge groups.

    A player on this sheet walks between both ends, so both end cameras see them. A group
    seen almost only by one camera near its frame edge is probably on the next sheet.
    """
    sizes = sorted((g["faces"] for g in groups), reverse=True)[:8]
    reference = float(np.median(sizes)) if sizes else 0.0
    floor = max(params["min_player_faces"], 0.1 * reference)
    for g in groups:
        total = sum(g["views"].values()) or 1
        balance = min(g["views"].values()) / total
        g["likely_other_sheet"] = bool(balance < 0.1 and abs(g["median_foot_x"] - 0.5) > 0.38)
        long_enough = g["span_seconds"] >= max(params["min_player_span_seconds"], 0.25 * span_seconds)
        g["role"] = (
            "other_sheet"
            if g["likely_other_sheet"] and g["faces"] >= params["min_player_faces"]
            else "player"
            if g["faces"] >= floor and long_enough
            else "minor"
        )
    return {"player_face_floor": floor, "reference_group_faces": reference}
