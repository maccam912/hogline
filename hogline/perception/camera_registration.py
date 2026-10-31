"""Bounded image alignment proposals; never establish physical camera calibration."""

import cv2
import numpy as np

from hogline.perception.landmark_calibration import map_points

VERSION = "retained-camera-registration-v1"


def register_camera_images(reference, target):
    base = {
        "version": VERSION,
        "status": "unavailable",
        "matrix": None,
        "notice": "Image feature alignment is not independent landmark validation or physical accuracy.",
    }
    if reference.size != target.size or min(reference.size) < 160 or max(reference.size) > 1080:
        return {**base, "reason": "camera_image_geometry_differs_or_is_unsupported"}
    width, height = reference.size
    # Exclude the broadcast logos/footer. Moving people, brooms and stones can
    # still supply false matches, so require broad agreement and held-out matches.
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[round(height * 0.16) : round(height * 0.90), round(width * 0.03) : round(width * 0.97)] = 255
    orb = cv2.ORB_create(nfeatures=1800)
    kp, descriptors = [], []
    for image in (reference, target):
        points, values = orb.detectAndCompute(
            cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY), mask
        )
        kp.append(points)
        descriptors.append(values)
    if any(d is None or len(d) < 40 for d in descriptors):
        return {**base, "reason": "insufficient_camera_texture"}
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    reverse = matcher.match(descriptors[1], descriptors[0])
    reverse_index = {m.queryIdx: m.trainIdx for m in reverse}
    matches = [
        a
        for pair in matcher.knnMatch(descriptors[0], descriptors[1], k=2)
        if len(pair) == 2
        for a, b in [pair]
        if a.distance < 0.7 * b.distance and reverse_index.get(a.trainIdx) == a.queryIdx
    ]
    if len(matches) < 40:
        return {**base, "reason": "insufficient_distinct_camera_matches", "match_count": len(matches)}
    # Spatial ordering spreads deterministic held-out points around the frame.
    matches.sort(key=lambda m: (kp[0][m.queryIdx].pt[1], kp[0][m.queryIdx].pt[0]))
    source = np.array([kp[0][m.queryIdx].pt for m in matches], dtype=np.float64)
    destination = np.array([kp[1][m.trainIdx].pt for m in matches], dtype=np.float64)
    holdout = np.arange(len(matches)) % 3 == 0
    matrix, inliers = cv2.findHomography(
        source[~holdout], destination[~holdout], cv2.RANSAC, 2.0, maxIters=2000, confidence=0.995
    )
    if matrix is None or inliers is None or not np.isfinite(matrix).all():
        return {**base, "reason": "camera_registration_failed"}
    accepted = inliers.ravel().astype(bool)
    consensus = source[~holdout][accepted]
    if len(consensus) < 24 or np.mean(accepted) < 0.65:
        return {**base, "reason": "camera_matches_do_not_agree", "match_count": len(matches)}
    grid = {(min(2, int(x / width * 3)), min(2, int(y / height * 3))) for x, y in consensus}
    if len(grid) < 4 or np.ptp(consensus[:, 0]) < width * 0.35 or np.ptp(consensus[:, 1]) < height * 0.20:
        return {**base, "reason": "camera_matches_too_localized"}
    try:
        errors = np.linalg.norm(map_points(source[holdout], matrix) - destination[holdout], axis=1)
        corners = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]])
        moved = map_points(corners, matrix)
    except ValueError:
        return {**base, "reason": "unstable_camera_registration"}
    if np.median(errors) > 1.5 or np.quantile(errors, 0.90) > 3:
        return {**base, "reason": "held_out_camera_matches_disagree"}
    displacement = float(np.max(np.linalg.norm(moved - corners, axis=1)))
    if displacement > 20 or cv2.contourArea(moved.astype(np.float32), oriented=True) <= 0:
        return {
            **base,
            "reason": "camera_change_exceeds_small_drift_pilot",
            "maximum_corner_shift_px": displacement,
        }
    return {
        **base,
        "status": "alignment_proposal",
        "matrix": (matrix / matrix[2, 2]).tolist(),
        "match_count": len(matches),
        "fit_inlier_count": len(consensus),
        "held_out_count": int(sum(holdout)),
        "held_out_median_error_px": float(np.median(errors)),
        "held_out_p90_error_px": float(np.quantile(errors, 0.90)),
        "maximum_corner_shift_px": displacement,
        "feature_support_polygon_px": cv2.convexHull(consensus.astype(np.float32)).reshape(-1, 2).tolist(),
        "acceptance": "proposed",
    }
