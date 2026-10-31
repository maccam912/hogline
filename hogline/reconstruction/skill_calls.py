"""Associate pre-delivery observations by their frozen anchor, never by strategic plausibility."""


def call_evidence(sequences, candidates):
    result = []
    for sequence in sequences:
        request = sequence["request"]
        if request.get("purpose") != "pre_delivery_call":
            continue
        linked = [
            c["id"]
            for c in candidates
            if any(
                o["sequence_id"] == request["anchor_sequence_id"]
                and o["delivery_index"] == request["anchor_delivery_index"]
                for o in c["observations"]
            )
        ]
        result.append(
            {
                "sequence_id": sequence["id"],
                "candidate_ids": linked,
                "association": "precedes_candidate_unconfirmed_receiving_end",
                "stale": sequence["stale"],
                "view": request["view"],
                "start_seconds": request["start_seconds"],
                "end_seconds": request["end_seconds"],
                "call": sequence["proposal"]["call"],
                "call_basis": sequence["proposal"]["call_basis"],
                "targets": [
                    {**target, **asset}
                    for target, asset in zip(
                        sequence["proposal"].get("targets", []),
                        sequence.get("target_assets", []),
                        strict=True,
                    )
                ],
                "strategy_options": sequence["proposal"].get("strategy_options", []),
                "limitations": sequence["proposal"]["limitations"],
                "acceptance": "proposed",
            }
        )
    return result
