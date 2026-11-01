"""Printable player estimates with linked, inspectable source evidence."""

from html import escape


def clock(seconds):
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def delivery_ray_html(game_id, ray):
    if not ray:
        return ""
    root = (
        f"/api/games/{escape(game_id, quote=True)}/skill-estimates/delivery-rays/"
        f"{escape(ray['id'], quote=True)}"
    )
    split = ray["house_split"]
    timing = "Partial house pass; a full house split is unavailable"
    if split["status"] == "partial_house_only":
        elapsed = ray["last_seconds"] - ray["first_seconds"]
        span = ray["observed_forward_span_m"][1] - ray["observed_forward_span_m"][0]
        timing = (
            f"Partial pass: {round(elapsed * ray['source_fps'])} source frames / {elapsed:.3f} s "
            f"over {span:.2f} m; full-house split unavailable"
        )
    if split["status"] in ("observed_full_house", "estimated_full_house"):
        timing = (
            f"{split['source_frame_difference']} source frames · "
            f"{split['elapsed_seconds']:.3f} s across the 12 ft house"
        )
        if split["status"] == "estimated_full_house":
            timing = "Approximately " + timing + " (crop-edge crossing estimated)"
    observed = ray["observed_weight"]["status"]
    weight = (
        "Observed light of tee (front-of-rings placement; possibly an intended guard)."
        if observed == "light_of_tee"
        else "Short/past-tee finish: unresolved from the reviewed evidence."
    )
    frames = " · ".join(
        f"<a target='_blank' rel='noopener' href='/api/evidence/{escape(f['id'], quote=True)}/composite'>"
        f"{f['pts'] * ray['points'][0]['time_base_num'] / ray['points'][0]['time_base_den']:.3f} s</a>"
        for f in ray["native_frame_ids"]
    )
    lo, hi = ray["target_tee_sensitivity_m"]
    panels = []
    for view, caption, alt in (
        (
            "origin",
            "Delivery house · tracked handle",
            "Native handle observations through the delivery house",
        ),
        ("receiving", "Receiving house · synchronous broom view", "Straight ray on the opposite tee line"),
    ):
        url = f"{root}/{view}.svg"
        panels.append(
            f"<figure><a href='{url}' target='_blank' rel='noopener'>"
            f"<img loading='lazy' src='{url}' alt='{alt}'></a>"
            f"<figcaption>{caption}</figcaption></figure>"
        )
    distance = ""
    if ray.get("perspectives"):
        for view, label in (
            ("perspective_left", "Left perspective"),
            ("perspective_right", "Right perspective"),
        ):
            url = f"{root}/{view}.svg"
            panels.append(
                f"<figure><a href='{url}' target='_blank' rel='noopener'>"
                f"<img loading='lazy' src='{url}' alt='Dotted ice ray in the {label.lower()}'>"
                f"</a><figcaption>{label} · same source instant · dotted straight ray</figcaption></figure>"
            )
        cal = ray["perspective_calibration"]
        audit = cal["distance_check"]
        a, b = audit["sensitivity_range_m"]
        paired_frames = " · ".join(
            f"<a target='_blank' rel='noopener' href='/api/evidence/{escape(p['frame_id'], quote=True)}"
            f"/composite'>{p['source_seconds']:.3f} s</a>"
            for p in cal["pairs"]
        )
        distance = (
            "<details><summary>Button-to-button check · paired stone footage</summary>"
            f"<p>{len(cal['pairs'])} paired frames in both delivery directions give an approximate "
            f"{audit['estimate_m']:.2f} m span. Pixel/landmark perturbations give "
            f"{a:.2f}–{b:.2f} m; this is a sensitivity range, not a confidence interval. "
            "The local maps use painted ring diameters and tee-to-hog distance, without fitting "
            "to the 114 ft button distance.</p>"
            "<p><strong>114 ft retained.</strong> These shallow views do not independently confirm "
            "a surveyed distance or justify changing it. Lens distortion, extrapolation, stone "
            "ground-point placement and camera exposure timing remain uncertain.</p>"
            f"<a href='{root}/distance.svg' target='_blank' rel='noopener'>"
            f"<img loading='lazy' src='{root}/distance.svg' alt='Same yellow stone marked in both "
            "perspective views'></a>"
            f"<p>Original paired frames: {paired_frames}.</p></details>"
        )
    proxy = "A full-house weight proxy is unavailable for this partial pass."
    if split.get("elapsed_seconds"):
        proxy = (
            f"Delivery split {split['elapsed_seconds']:.3f} s; "
            f"nominal average speed {split['nominal_mean_speed_mps']:.2f} m/s. "
            "A shorter split means faster passage."
        )
    return (
        "<div class='native-ray'><h4>Delivery-house ray and timing</h4>"
        f"<p><a href='/games/{escape(game_id, quote=True)}/hog-rays#"
        f"{escape(ray['candidate_id'], quote=True)}'>Compare direction at the delivery hog line</a></p>"
        f"<p><strong>{timing}</strong>. Handle path observed at "
        f"{ray['first_seconds']:.3f}–{ray['last_seconds']:.3f} s.</p>"
        f"<div class='ray-pair'>{''.join(panels)}</div>"
        "<p>Pink line: straight continuation of the observed path. Pink dot: opposite tee crossing. "
        f"Sensitivity range: {lo:+.2f} to {hi:+.2f} m, image-right positive; "
        "this is not a confidence interval.</p>"
        f"<p><strong>Weight proxy:</strong> {proxy} The curler can still be pushing during this split; "
        "it does not determine the eventual stop.</p>"
        f"<p><strong>Observed finish:</strong> {weight}</p>"
        "<p class='muted'>Nominal button-to-button distance: 114 ft (34.7472 m), from "
        f"<a href='{escape(ray['dimension_reference'], quote=True)}'>World Curling rules</a>; "
        "no Wausau-specific survey found. This ray does not include curl or subsequent steering. "
        "Painted-ice geometry, fixed cameras and handle height limit accuracy. "
        f"Exact retained source frames: {frames}.</p>{distance}</div>"
    )


def report_html(report):
    def e(value):
        return escape(str(value or ""), quote=True)

    sections = []
    for player in report["players"]:
        findings = "".join(f"<li>{e(f['text'])}</li>" for f in player["findings"])
        rows = []
        for shot in player["deliveries"]:
            feedback = shot["feedback_comparison"]
            comparison = ""
            if feedback:
                comparison = (
                    "<p><strong>Possible broom feedback:</strong> "
                    f"{e(feedback['image_direction'])} in this view, "
                    f"approximately {abs(feedback['nominal_lateral_shift_m']):.1f} m. "
                    f"{e(feedback['basis'])}</p><p class='muted'>{e(feedback['notice'])}</p>"
                )
            frames = "".join(
                f"<a href='{e(frame['image_url'])}' target='_blank' rel='noopener'>"
                f"<img loading='lazy' src='{e(frame['image_url'])}' "
                f"alt='Source frame at {clock(frame['seconds'])}'>"
                f"<span>{clock(frame['seconds'])}</span></a>"
                for frame in shot["evidence"]
            )
            rows.append(
                f"<details><summary><a href='{e(shot['video_url'])}' target='_blank' rel='noopener'>"
                f"{clock(shot['seconds'])}</a> · "
                f"{('End ' + str(shot['end']) + ' (estimated)') if shot['end'] else 'End unknown'} · "
                f"{e(shot['finish'].replace('_', ' '))}</summary>"
                f"<p>{e(shot['summary']) or 'Delivery identity reviewed; outcome remains unknown.'}</p>"
                f"<p class='muted'>Attribution: {e(shot['basis'])}</p>{comparison}"
                + (
                    f"<p><a href='/games/{e(report['game_id'])}/reconstructions?throw="
                    f"{e(shot['candidate_id'])}'>Reconstruct this throw</a></p>"
                    if shot.get("delivery_ray")
                    else ""
                )
                + delivery_ray_html(report["game_id"], shot.get("delivery_ray"))
                + f"<div class='frames'>{frames}</div></details>"
            )
        positions = player["position"] or "Position unassigned"
        sections.append(
            f"<section id='player-{e(player['person_id'])}'><h2>{e(player['name'])} "
            f"<small>{e(positions)}</small></h2><p>{player['observed_deliveries']} attributed deliveries · "
            f"{e(player['sample_status'].replace('_', ' '))}</p>"
            f"{('<ul>' + findings + '</ul>') if findings else '<p>No individual findings reviewed yet.</p>'}"
            "<p class='muted'>Weight accuracy, release aim accuracy and sweeping effectiveness: "
            "unavailable where intended targets or supported measurements are missing.</p>"
            f"<h3>Shot evidence</h3>{''.join(rows)}</section>"
        )
    context = next(iter(report["contexts"].values()), {})
    stale = (
        f"<p class='warning'>Evidence changed: {e(', '.join(report['stale_reasons']))}. "
        "Earlier findings are withheld pending a new review.</p>"
        if report["stale"]
        else ""
    )
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{e(report['label'])} · Player reports</title><style>"
        "body{font:17px/1.6 system-ui,sans-serif;color:#172f39;background:#f3f6f5;margin:0}"
        "main{max-width:1050px;margin:auto;padding:30px}h1{line-height:1.2}"
        "section{background:white;border:1px solid #ccd8d5;border-radius:12px;padding:24px;margin:24px 0}"
        "small,.muted{color:#526b72}small{font-size:17px;font-weight:400;margin-left:12px}"
        "a{color:#006b71}details{border-top:1px solid #d9e2df;padding:14px 0}summary{cursor:pointer}"
        ".frames{display:flex;gap:10px;overflow:auto}.frames a{min-width:220px;max-width:300px}"
        "img{display:block;width:100%;height:auto}.frames span{display:block}"
        ".ray-pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}"
        ".ray-pair figure{margin:0}"
        ".native-ray{background:#edf5f4;padding:16px;margin:16px 0;border-radius:8px}"
        ".native-ray h4{margin-top:0}@media(max-width:600px){.ray-pair{grid-template-columns:1fr}}"
        ".warning{background:#fff0c7;padding:16px}nav{display:flex;gap:20px;flex-wrap:wrap}"
        "@media print{body{background:white}main{padding:0}section{break-before:page;border:0}"
        "nav,.frames{display:none}details{display:block}}"
        f"</style><main><p>{e(context.get('recording_date', ''))} · Estimated player reports</p>"
        f"<h1>{e(report['label'])}</h1><p>{e(context.get('team', 'Team'))} · "
        f"{e(context.get('stone_color', 'Unassigned'))} stones</p>"
        f"<p>{e(report['notice'])}</p>{stale}"
        f"<p>{report['coverage']['attributed_deliveries']} attributed deliveries from "
        f"{report['coverage']['sequence_clips']} analyzed clips. {e(report['coverage']['notice'])}</p>"
        "<nav>"
        + "".join(f"<a href='#player-{e(p['person_id'])}'>{e(p['name'])}</a>" for p in report["players"])
        + f"<a href='/static/skills.html?game={e(report['game_id'])}'>Review evidence</a>"
        + f"<a href='/games/{e(report['game_id'])}/reconstructions'>Replay all throws together</a>"
        + f"<a href='/api/games/{e(report['game_id'])}/skill-estimates'>Download JSON</a></nav>"
        + "".join(sections)
        + f"<p class='muted'>Review revision {report['revision']} · {e(report['author'])}. "
        "No assistant observation is automatically accepted as a confirmed measurement.</p></main></html>"
    )
