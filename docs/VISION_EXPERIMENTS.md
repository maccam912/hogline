# Vision experiments

## Generic person detection, October 2026

A local OpenCV DNN trial used the [OpenCV Zoo YOLOX-s model](https://github.com/opencv/opencv_zoo/tree/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/object_detection_yolox), trained on COCO's generic object classes. The model directory is Apache-2.0 licensed. The ONNX artifact was pinned to commit `48b10fa505aeb51fbd127fc60a5c660040e823b7` and SHA-256 `c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063` (35,858,002 bytes). Weights and outputs remain in ignored local data storage.

The trial used 640×640 RGB input with aspect-preserving top-left letterboxing (padding 114), raw 0–255 float input, stride-8/16/32 YOLOX decoding, person objectness×class threshold 0.35 and NMS threshold 0.5. Six selected overhead crops included clearly visible players on Sheets 3, 4, 5, and 6, and an empty Sheet 2 crop. It found one of the Sheet 4 players but missed other conspicuous overhead people, including the January Sheet 5 person responsible for the persistent false handle candidate. Selected examples are not a representative benchmark.

This model is **not enabled in the processing pipeline**. Its absence of detections cannot establish clear ice. The current dark-region heuristic remains an abstention cue, also with known limitations. A useful replacement needs labels and evaluation for overhead people, brooms, hands, occluded ice, and true stone/handle instances. Generic COCO recognition alone did not meet that need in this trial.

## Foreground label correction

Inspecting the retained January candidate's exact center showed it on the person's foot/clothing at approximately (110,361) in the 480×480 bottom-house crop. Earlier development notes called it a broom head; that identification was incorrect. The visible red broom head is elsewhere in the image. The v2 window gate still rejects the frame due to the large dark foreground region. The explicit negative-region annotation now ensures that the handle detector's error is also counted in evaluation, rather than being hidden by the surrounding occlusion mask.
