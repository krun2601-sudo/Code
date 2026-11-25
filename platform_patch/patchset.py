"""Deterministic source transformations for the exact ZR10 4.0.0 snapshot.

The installer verifies byte-level source hashes before calling these functions.
Each transformation additionally requires unique textual anchors so a source
that merely shares a version string can never be patched accidentally.
"""
from __future__ import annotations

from collections.abc import Callable


PATCH_VERSION = "ral-hil-context-1.0.0"


class PatchError(RuntimeError):
    """The verified source did not contain the exact expected patch anchor."""


def _replace_once(text: str, old: str, new: str, relative_path: str) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(
            f"{relative_path}: expected exactly one patch anchor, found {count}"
        )
    return text.replace(old, new, 1)


def _models(text: str) -> str:
    path = "zr10lab/models.py"
    old = '''@dataclass(frozen=True)
class PolicyContext:
    t: float
    dt: float
    step: int
    devices: dict[str, Telemetry]
    detections: tuple[Detection, ...] = ()
    rays: tuple[Ray, ...] = ()
    tracks: tuple[Track, ...] = ()
    metrics: dict[str, float] = field(default_factory=dict)
'''
    new = '''@dataclass(frozen=True)
class FrameStatus:
    """Latest newly decoded frame metadata exposed without raw image pixels.

    capture_t is a host-side estimate (receive time minus configured video
    latency), not a camera hardware exposure timestamp.  A usable frame has
    passed the configured resolution and inexpensive image-quality checks.
    """
    device_id: str
    frame_id: int
    capture_t: float
    received_t: float
    width: int
    height: int
    usable: bool = True
    resolution_ok: bool = True
    mean_luma: float = 0.0
    contrast: float = 0.0
    sharpness: float = 0.0
    skipped_frames: int = 0
    reason: str = ""


@dataclass(frozen=True)
class PolicyContext:
    t: float
    dt: float
    step: int
    devices: dict[str, Telemetry]
    detections: tuple[Detection, ...] = ()
    rays: tuple[Ray, ...] = ()
    tracks: tuple[Track, ...] = ()
    metrics: dict[str, float] = field(default_factory=dict)
    # Appended fields preserve every existing positional constructor call.
    frames: dict[str, FrameStatus] = field(default_factory=dict)
    command_results: dict[str, "CommandResult"] = field(default_factory=dict)
'''
    return _replace_once(text, old, new, path)


def _vision(text: str) -> str:
    path = "zr10lab/vision.py"
    text = _replace_once(
        text,
        "from .models import Detection\n",
        "from .models import Detection, FrameStatus\n",
        path,
    )
    old = '''        self.errors: dict[str, str] = {}
        self.skipped_frames: dict[str, int] = {key: 0 for key in self.sources}
        self._reported_errors: dict[str, str] = {}
'''
    new = '''        self.errors: dict[str, str] = {}
        self.skipped_frames: dict[str, int] = {key: 0 for key in self.sources}
        self._reported_errors: dict[str, str] = {}
        # Frame health has a mailbox independent of detection and preview.  A
        # pure coverage policy therefore never needs a fake Detection object.
        self._frame_updates: dict[str, FrameStatus] = {}
        quality = cfg.policy.get("frame_quality", {})
        if not isinstance(quality, Mapping):
            raise ValueError("policy.frame_quality must be a mapping")
        self._quality = {
            "min_mean_luma": float(quality.get("min_mean_luma", 0.0)),
            "max_mean_luma": float(quality.get("max_mean_luma", 255.0)),
            "min_contrast": float(quality.get("min_contrast", 0.0)),
            "min_sharpness": float(quality.get("min_sharpness", 0.0)),
        }
        values = tuple(self._quality.values())
        if (not all(math.isfinite(value) for value in values) or
                not 0 <= self._quality["min_mean_luma"] <= self._quality["max_mean_luma"] <= 255 or
                self._quality["min_contrast"] < 0 or self._quality["min_sharpness"] < 0):
            raise ValueError("invalid policy.frame_quality thresholds")
'''
    text = _replace_once(text, old, new, path)

    old = '''    def preview_batch(self, device_id: str) -> DetectionBatch | None:
        """Only read the latest image; does not consume collect(), returns time for stale marking."""
'''
    # The upstream docstring is Chinese; insert relative to the method line only.
    if old not in text:
        old = '''    def preview_batch(self, device_id: str) -> DetectionBatch | None:
        """\u53ea\u8bfb\u83b7\u53d6\u6700\u65b0\u753b\u9762\uff1b\u4e0d\u4f1a\u6d88\u8d39 collect()\uff0c\u8fd4\u56de\u65f6\u95f4\u7528\u4e8e\u754c\u9762\u6807\u793a\u9648\u65e7\u56fe\u50cf\u3002"""
'''
    new = '''    def _assess_frame(self, frame: VideoFrame) -> FrameStatus:
        """Compute cheap, deterministic health features on a small sample."""
        image = np.asarray(frame.image_bgr)
        reasons: list[str] = []
        if (image.ndim not in (2, 3) or image.shape[0] <= 0 or image.shape[1] <= 0 or
                (image.ndim == 3 and image.shape[2] <= 0)):
            width = height = 0
            mean_luma = contrast = sharpness = 0.0
            resolution_ok = False
            reasons.append("invalid_image_shape")
        else:
            height, width = int(image.shape[0]), int(image.shape[1])
            expected = tuple(int(value) for value in self._device_configs[frame.device_id].camera.get(
                "image_size", (width, height)))
            resolution_ok = expected == (width, height)
            if not resolution_ok:
                reasons.append("resolution_mismatch")
            row_step = max(1, height // 90)
            col_step = max(1, width // 160)
            sample = image[::row_step, ::col_step]
            if sample.ndim == 3:
                if sample.shape[2] < 3:
                    gray = sample[..., 0].astype(np.float32)
                else:
                    values = sample[..., :3].astype(np.float32)
                    gray = 0.114 * values[..., 0] + 0.587 * values[..., 1] + 0.299 * values[..., 2]
            else:
                gray = sample.astype(np.float32)
            if not np.isfinite(gray).all():
                reasons.append("nonfinite_pixels")
                gray = np.nan_to_num(gray, nan=0.0, posinf=255.0, neginf=0.0)
            mean_luma = float(np.mean(gray))
            contrast = float(np.std(gray))
            gradients = []
            if gray.shape[0] > 1:
                gradients.append(np.abs(np.diff(gray, axis=0)).ravel())
            if gray.shape[1] > 1:
                gradients.append(np.abs(np.diff(gray, axis=1)).ravel())
            sharpness = float(np.mean(np.concatenate(gradients))) if gradients else 0.0
            if not self._quality["min_mean_luma"] <= mean_luma <= self._quality["max_mean_luma"]:
                reasons.append("luma_out_of_range")
            if contrast < self._quality["min_contrast"]:
                reasons.append("low_contrast")
            if sharpness < self._quality["min_sharpness"]:
                reasons.append("low_sharpness")
        age_s = time.monotonic() - float(frame.capture_monotonic_s)
        if age_s < 0:
            reasons.append("future_timestamp")
        elif age_s > self.max_frame_age_s:
            reasons.append("stale_frame")
        return FrameStatus(
            device_id=frame.device_id, frame_id=int(frame.frame_id),
            capture_t=float(frame.capture_monotonic_s), received_t=float(frame.receive_monotonic_s),
            width=width, height=height, usable=not reasons, resolution_ok=resolution_ok,
            mean_luma=mean_luma, contrast=contrast, sharpness=sharpness,
            skipped_frames=int(self.skipped_frames.get(frame.device_id, 0)), reason=";".join(reasons),
        )

    def collect_frame_statuses(self) -> dict[str, FrameStatus]:
        """Consume each device's newest unreported frame status exactly once."""
        with self._lock:
            results = dict(self._frame_updates)
            self._frame_updates.clear()
        return results

    def preview_batch(self, device_id: str) -> DetectionBatch | None:
        """\u53ea\u8bfb\u83b7\u53d6\u6700\u65b0\u753b\u9762\uff1b\u4e0d\u4f1a\u6d88\u8d39 collect()\uff0c\u8fd4\u56de\u65f6\u95f4\u7528\u4e8e\u754c\u9762\u6807\u793a\u9648\u65e7\u56fe\u50cf\u3002"""
'''
    text = _replace_once(text, old, new, path)

    old = '''                    if time.monotonic() - frame.capture_monotonic_s > self.max_frame_age_s:
                        continue
                    start = time.monotonic()
                    enabled = self.detection_enabled
                    detections = validate_detections(self.detector.detect(frame.image_bgr),
                                                     frame.image_bgr.shape[1], frame.image_bgr.shape[0]) if enabled else []
                    batch = DetectionBatch(frame, tuple(detections), start, time.monotonic())
                    with self._lock:
                        self._preview[key] = batch
                        if enabled and self.detection_enabled:
                            self._batches[key] = batch
                        self.errors.pop(key, None)
'''
    new = '''                    # Publish health before optional inference.  Detector failure must
                    # not erase evidence that the RTSP decoder delivered a frame.
                    status = self._assess_frame(frame)
                    with self._lock:
                        self._frame_updates[key] = status
                    if time.monotonic() - frame.capture_monotonic_s > self.max_frame_age_s:
                        worked = True
                        continue
                    start = time.monotonic()
                    enabled = self.detection_enabled
                    detections = validate_detections(self.detector.detect(frame.image_bgr),
                                                     frame.image_bgr.shape[1], frame.image_bgr.shape[0]) if enabled else []
                    batch = DetectionBatch(frame, tuple(detections), start, time.monotonic())
                    with self._lock:
                        self._preview[key] = batch
                        if enabled and self.detection_enabled:
                            self._batches[key] = batch
                        self.errors.pop(key, None)
'''
    return _replace_once(text, old, new, path)


def _recording(text: str) -> str:
    path = "zr10lab/recording.py"
    text = _replace_once(
        text,
        '    "truth": ["truth_id","x_m","y_m","z_m"],\n',
        '    "truth": ["truth_id","x_m","y_m","z_m"],\n'
        '    "frames": ["device_id","frame_id","capture_t","received_t","width","height","age_s","usable","resolution_ok","mean_luma","contrast","sharpness","skipped_frames","reason"],\n',
        path,
    )
    text = _replace_once(
        text,
        '            "time_basis":"simulation_seconds" if mode=="sim" else "host_monotonic_seconds",\n'
        '            "config":config,"completed":False}\n',
        '            "time_basis":"simulation_seconds" if mode=="sim" else "host_monotonic_seconds",\n'
        '            "platform_patch_version":"ral-hil-context-1.0.0",\n'
        '            "config":config,"completed":False}\n',
        path,
    )
    return text


def _runtime(text: str) -> str:
    path = "zr10lab/runtime.py"
    text = _replace_once(
        text,
        '''    def process(self,t,dt,states,detections,history=None,truth=None,action_transform=None,
                fusion_device_ids=None,coverage_enabled=True):
''',
        '''    def process(self,t,dt,states,detections,history=None,truth=None,action_transform=None,
                fusion_device_ids=None,coverage_enabled=True,frames=None,command_results=None):
''',
        path,
    )
    text = _replace_once(
        text,
        '''        context = PolicyContext(t,dt,self.step,states,tuple(valid_detections),tuple(rays),tuple(tracks),public_metrics)
''',
        '''        # Copy extension dictionaries so a plugin cannot mutate live runtime mailboxes.
        frame_statuses = dict(frames or {})
        latest_results = dict(command_results or {})
        context = PolicyContext(t,dt,self.step,states,tuple(valid_detections),tuple(rays),tuple(tracks),
                                public_metrics,frame_statuses,latest_results)
''',
        path,
    )
    text = _replace_once(
        text,
        '''        self._record(t,states,detections,rays,tracks,localizations,actions,truth)
''',
        '''        self._record(t,states,detections,rays,tracks,localizations,actions,truth,frame_statuses)
''',
        path,
    )
    text = _replace_once(
        text,
        '''    def _record(self,t,states,detections,rays,tracks,locations,actions,truth):
''',
        '''    def _record(self,t,states,detections,rays,tracks,locations,actions,truth,frames):
''',
        path,
    )
    text = _replace_once(
        text,
        '''        for a in actions.values():
            r.write("actions",t,device_id=a.device_id,issued_t=a.issued_t,ttl_s=a.ttl_s,yaw_deg=a.yaw_deg,
                pitch_deg=a.pitch_deg,zoom=a.zoom,target_ids_json=dumps(a.target_ids),reason=a.reason,parameters_json=dumps(a.parameters))
        if truth:
''',
        '''        for a in actions.values():
            r.write("actions",t,device_id=a.device_id,issued_t=a.issued_t,ttl_s=a.ttl_s,yaw_deg=a.yaw_deg,
                pitch_deg=a.pitch_deg,zoom=a.zoom,target_ids_json=dumps(a.target_ids),reason=a.reason,parameters_json=dumps(a.parameters))
        for frame in frames.values():
            r.write("frames",t,device_id=frame.device_id,frame_id=frame.frame_id,
                capture_t=frame.capture_t,received_t=frame.received_t,width=frame.width,height=frame.height,
                age_s=t-frame.capture_t,usable=frame.usable,resolution_ok=frame.resolution_ok,
                mean_luma=frame.mean_luma,contrast=frame.contrast,sharpness=frame.sharpness,
                skipped_frames=frame.skipped_frames,reason=frame.reason)
        if truth:
''',
        path,
    )
    text = _replace_once(
        text,
        '''    def result_sink(result):
        recorder.write("commands",result.t,device_id=result.device_id,command_t=result.t,status=result.status,
            sent=result.sent,ack=result.ack,latency_ms=result.latency_ms,applied_json=dumps(result.applied),
            error=result.error,action_json=dumps(result.action))
''',
        '''    latest_results = {}
    def result_sink(result):
        latest_results[result.device_id] = result
        recorder.write("commands",result.t,device_id=result.device_id,command_t=result.t,status=result.status,
            sent=result.sent,ack=result.ack,latency_ms=result.latency_ms,applied_json=dumps(result.applied),
            error=result.error,action_json=dumps(result.action))
''',
        path,
    )
    text = _replace_once(
        text,
        '''    vision = VisionPipeline(cfg,event_sink=recorder.event)
''',
        '''    vision = VisionPipeline(cfg,event_sink=recorder.event,
                            detection_enabled=bool(cfg.policy.get("requires_detection", True)))
''',
        path,
    )
    text = _replace_once(
        text,
        '''            actions = await asyncio.to_thread(experiment.process,now,dt,fleet.states(),vision.collect(),histories.get)
''',
        '''            detections = vision.collect()
            frames = vision.collect_frame_statuses()
            states = fleet.states()
            now = time.monotonic()  # Snapshot time is not earlier than any accepted input.
            actions = await asyncio.to_thread(experiment.process,now,dt,states,detections,histories.get,
                                              frames=frames,command_results=dict(latest_results))
''',
        path,
    )
    return text


def _control_modes(text: str) -> str:
    path = "zr10lab/control_modes.py"
    text = _replace_once(
        text,
        '    allowed = {"id", "label", "description", "policy", "settings", "requires_detection", "requires_calibration"}\n',
        '    allowed = {"id", "label", "description", "policy", "settings", "requires_detection", "requires_calibration", "requires_video"}\n',
        path,
    )
    text = _replace_once(
        text,
        '            raise ValueError("\u6bcf\u4e2a\u6a21\u5f0f\u5fc5\u987b\u662f\u6620\u5c04\uff0c\u4e14\u53ea\u80fd\u5305\u542b id/label/description/policy/settings/requires_detection/requires_calibration")\n',
        '            raise ValueError("\u6bcf\u4e2a\u6a21\u5f0f\u5fc5\u987b\u662f\u6620\u5c04\uff0c\u4e14\u53ea\u80fd\u5305\u542b id/label/description/policy/settings/requires_detection/requires_calibration/requires_video")\n',
        path,
    )
    text = _replace_once(
        text,
        '        for field in ("requires_detection", "requires_calibration"):\n',
        '        for field in ("requires_detection", "requires_calibration", "requires_video"):\n',
        path,
    )
    text = _replace_once(
        text,
        '''        base = registry.get(key, dict(id=key, label=key, description="\u672c\u5730\u81ea\u5b9a\u4e49\u7b97\u6cd5", settings={},
                                      requires_detection=True, requires_calibration=True))
''',
        '''        base = registry.get(key, dict(id=key, label=key, description="\u672c\u5730\u81ea\u5b9a\u4e49\u7b97\u6cd5", settings={},
                                      requires_detection=True, requires_calibration=True, requires_video=False))
''',
        path,
    )
    return text


def _control_center(text: str) -> str:
    path = "zr10lab/control_center.py"
    text = _replace_once(
        text,
        '''    def _detection_required(self):
        entry = next(item for item in self._catalog() if item["id"] == self.mode)
        return bool(entry.get("requires_detection", self.mode in ("track", "center", "multi")))

    async def _ensure_vision(self):
''',
        '''    def _detection_required(self):
        entry = next(item for item in self._catalog() if item["id"] == self.mode)
        return bool(entry.get("requires_detection", self.mode in ("track", "center", "multi")))

    def _video_required(self):
        entry = next(item for item in self._catalog() if item["id"] == self.mode)
        return bool(entry.get("requires_video", False))

    async def _ensure_vision(self):
''',
        path,
    )
    text = _replace_once(
        text,
        '''        required = self._detection_required()
        if not (required or self.video_enabled):
''',
        '''        required = self._detection_required()
        if not (required or self._video_required() or self.video_enabled):
''',
        path,
    )
    text = _replace_once(
        text,
        '''                t = self._clock()
                detections = self._world.observe() if self._world is not None else self._vision.collect() if self._vision is not None else []
                detections = [d for d in detections if d.device_id not in self._disconnected]
''',
        '''                detections = self._world.observe() if self._world is not None else self._vision.collect() if self._vision is not None else []
                frames = self._vision.collect_frame_statuses() if self._vision is not None else {}
                t = self._clock()  # Snapshot time is not earlier than any accepted frame.
                detections = [d for d in detections if d.device_id not in self._disconnected]
                frames = {key: value for key, value in frames.items() if key not in self._disconnected}
''',
        path,
    )
    text = _replace_once(
        text,
        '''                actions = await worker.call(lambda: experiment.process(t, self._dt, states, detections,
                                            histories.get if histories else None, truth, arbitrate,
                                            fusion_device_ids=fusion_ids,
                                            coverage_enabled=fusion_ids is None or len(fusion_ids) == len(self.cfg.active_devices)))
''',
        '''                command_results = copy.deepcopy(self._results)
                actions = await worker.call(lambda: experiment.process(t, self._dt, states, detections,
                                            histories.get if histories else None, truth, arbitrate,
                                            fusion_device_ids=fusion_ids,
                                            coverage_enabled=fusion_ids is None or len(fusion_ids) == len(self.cfg.active_devices),
                                            frames=frames,command_results=command_results))
''',
        path,
    )
    return text


TRANSFORMERS: dict[str, Callable[[str], str]] = {
    "zr10lab/models.py": _models,
    "zr10lab/vision.py": _vision,
    "zr10lab/recording.py": _recording,
    "zr10lab/runtime.py": _runtime,
    "zr10lab/control_modes.py": _control_modes,
    "zr10lab/control_center.py": _control_center,
}


def transform_source(relative_path: str, source: str) -> str:
    try:
        transformer = TRANSFORMERS[relative_path]
    except KeyError as exc:
        raise PatchError(f"unsupported patch target: {relative_path}") from exc
    return transformer(source)


def transform_bytes(relative_path: str, source: bytes) -> bytes:
    """Preserve the source file's newline convention and UTF-8 encoding."""
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(f"{relative_path}: source is not UTF-8") from exc
    newline = "\r\n" if b"\r\n" in source else "\n"
    normalized = text.replace("\r\n", "\n")
    transformed = transform_source(relative_path, normalized)
    if transformed == normalized:
        raise PatchError(f"{relative_path}: transformation made no change")
    if newline == "\r\n":
        transformed = transformed.replace("\n", "\r\n")
    return transformed.encode("utf-8")
