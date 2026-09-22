import ast
import json
import numpy as np
import onnxruntime as ort
import PIL.Image
from pathlib import Path
from threading import Lock, Thread
import constants
from typing import Any, List, Dict
from logging import getLogger, basicConfig, DEBUG
from PyQt5.QtCore import pyqtBoundSignal
from ultralytics import YOLO
from ultralytics.data.augment import classify_transforms

# Set up logging configuration
basicConfig(level=DEBUG)
logger = getLogger("ml_models")
logger.setLevel(DEBUG)

ONNX_MODELS = {}
YOLO_MODELS = {}
YOLO_CLASSIFICATION_MODELS = {}
YOLO_CLASSIFICATION_MODELS_LOCK = Lock()

DEFAULT_IMAGE_SIZE = (224, 224)  # Default image size if not provided in metadata
DEFAULT_CLASS_NAMES = {i: f"class_{i}" for i in range(70)}  # Default class names if not provided
RESAMPLE_BILINEAR = getattr(PIL.Image, "Resampling", PIL.Image).BILINEAR

LAYOUT_NCHW = "NCHW"
LAYOUT_NHWC = "NHWC"
LAYOUT_UNKNOWN = "unknown"

BACKEND_YOLO_CLASSIFY = "ultralytics_yolo_cls"
BACKEND_ONNX_NHWC_RAW = "onnx_nhwc_raw"
BACKEND_ONNX_NCHW = "onnx_nchw"
BACKEND_UNKNOWN = "unknown"

PREPROCESS_RAW = "raw"
PREPROCESS_ZERO_ONE = "zero_one"
PREPROCESS_MINUS_ONE_ONE = "minus_one_one"
PREPROCESS_TORCH_IMAGENET = "torch_imagenet"

OUTPUT_AUTO = "auto"
OUTPUT_PROBABILITIES = "probabilities"
OUTPUT_LOGITS = "logits"

def load_models(model_names: List[str]):
    """
    Loads the ONNX models asynchronously for the given model names.
    
    Args:
        model_names (List[str]): List of model names to load.
    """
    logger.debug(f"Initiating model loading for: {model_names}")
    Thread(target=_load_models, args=(model_names,)).start()

def _load_models(model_names: List[str]):
    """
    Helper function to load models from disk and initialize them.

    Args:
        model_names (List[str]): List of model names to load.
    """
    models_base_path = Path('models').resolve()
    for name in model_names:
        # Find ONNX models in the specified directory
        onnx_models = find_onnx_models(Path(models_base_path, "classification", name))
        if onnx_models is not None:
            # Load all found ONNX models and store their metadata
            ONNX_MODELS[name] = [load_onnx_model_metadata(path) for path in onnx_models]
            logger.debug(f"Loaded ONNX models for {name}.")
        else:
            ONNX_MODELS[name] = None
            #logger.debug(f"No ONNX model file found in models/classification/{name}.")

    if constants.CROP_IMAGES:
        # Load YOLO model for image cropping
        onnx_models = find_onnx_models(Path(models_base_path, "cropping"))
        YOLO_MODELS['crop'] = YOLO(onnx_models[0], task="detect") if onnx_models is not None else None
        if onnx_models is None:
            logger.debug(f"No ONNX model file found in models/cropping. Images will not be cropped.")

def find_onnx_models(directory: Path) -> List[str] | None:
    """
    Searches the directory for ONNX model files (.onnx).

    Args:
        directory (Path): Path to the directory to search.

    Returns:
        List[str] | None: List of ONNX model file paths, or None if no models are found.
    """
    logger.debug(f"Searching for ONNX models in directory: {directory}")
    all_files = directory.glob('*')
    onnx_files = sorted(file for file in all_files if file.suffix.lower() == '.onnx')
    if not onnx_files:
        logger.debug(f'No ONNX files found in directory "{directory}".')
        return None
    return onnx_files

def _parse_structured_value(value: Any, default: Any = None) -> Any:
    """Parse JSON or Python-literal metadata without using eval()."""
    if value is None:
        return default
    if isinstance(value, (dict, list, tuple, int, float)):
        return value

    value_str = str(value)
    for parser in (json.loads, ast.literal_eval):
        try:
            return parser(value_str)
        except (ValueError, SyntaxError, TypeError, json.JSONDecodeError):
            continue
    return default

def _as_int_dim(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None

def _shape_layout(shape: list[Any]) -> str:
    """Return NHWC, NCHW, or unknown for a 4D image tensor shape."""
    if len(shape) != 4:
        return LAYOUT_UNKNOWN

    channel_first = _as_int_dim(shape[1])
    channel_last = _as_int_dim(shape[3])
    if channel_first == 3:
        return LAYOUT_NCHW
    if channel_last == 3:
        return LAYOUT_NHWC
    return LAYOUT_UNKNOWN

def _shape_image_size(shape: list[Any], layout: str) -> tuple[int, int] | None:
    if len(shape) != 4:
        return None
    if layout == LAYOUT_NCHW:
        h = _as_int_dim(shape[2])
        w = _as_int_dim(shape[3])
    elif layout == LAYOUT_NHWC:
        h = _as_int_dim(shape[1])
        w = _as_int_dim(shape[2])
    else:
        return None

    if h and w:
        return (w, h)
    return None

def _read_sidecar_config(onnx_path: Path) -> dict[str, Any]:
    """
    Read optional model routing config.

    Supported locations:
    - model_name.json
    - model_name.config.json
    - model_config.json in the model directory
    """
    candidates = [
        onnx_path.with_suffix(".json"),
        onnx_path.with_name(f"{onnx_path.stem}.config.json"),
        onnx_path.parent / "model_config.json",
    ]
    for config_path in candidates:
        if not config_path.exists():
            continue
        try:
            with config_path.open("r", encoding="utf-8") as file:
                config = json.load(file)
            logger.info(f"Loaded model sidecar config from {config_path}")
            return config
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(f"Could not read model sidecar config {config_path}: {error}")
    return {}

def _parse_class_names(metadata: dict[str, str], sidecar_config: dict[str, Any], output_dim: int | None) -> dict[int, str]:
    class_names_raw = (
        sidecar_config.get("class_names")
        or sidecar_config.get("names")
        or metadata.get("class_names")
        or metadata.get("names")
    )
    parsed = _parse_structured_value(class_names_raw)

    if isinstance(parsed, dict):
        try:
            return {int(k): str(v) for k, v in parsed.items()}
        except (TypeError, ValueError):
            logger.warning("Class names metadata could not be parsed as an index-to-name mapping.")
    elif isinstance(parsed, list):
        return {i: str(v) for i, v in enumerate(parsed)}

    if output_dim:
        logger.warning("Class names metadata missing; using output dimension to create fallback names.")
        return {i: f"class_{i}" for i in range(output_dim)}

    logger.warning("Class names metadata missing and output dimension unknown; using legacy fallback names.")
    return DEFAULT_CLASS_NAMES.copy()

def _parse_image_size(
    metadata: dict[str, str],
    sidecar_config: dict[str, Any],
    input_shape: list[Any],
    input_layout: str,
) -> tuple[int, int]:
    shape_size = _shape_image_size(input_shape, input_layout)
    if shape_size:
        return shape_size

    img_size_raw = sidecar_config.get("img_size") or sidecar_config.get("imgsz") or metadata.get("img_size") or metadata.get("imgsz")
    parsed = _parse_structured_value(img_size_raw)

    if isinstance(parsed, int):
        return (parsed, parsed)
    if isinstance(parsed, (list, tuple)) and len(parsed) >= 2:
        try:
            # Metadata and sidecars use [height, width]; PIL resize uses (width, height).
            return (int(parsed[1]), int(parsed[0]))
        except (TypeError, ValueError):
            logger.warning(f"Could not parse image size metadata: {img_size_raw}")

    logger.warning(f"Image size metadata missing; using default {DEFAULT_IMAGE_SIZE}.")
    return DEFAULT_IMAGE_SIZE

def _is_ultralytics_classify(metadata: dict[str, str], sidecar_config: dict[str, Any]) -> bool:
    backend = str(sidecar_config.get("backend", "")).lower()
    if backend in {"yolo", "yolo_cls", BACKEND_YOLO_CLASSIFY}:
        return True

    task = str(metadata.get("task", sidecar_config.get("task", ""))).lower()
    if task != "classify":
        return False

    metadata_text = " ".join(
        str(metadata.get(key, ""))
        for key in ("author", "docs", "description", "license")
    ).lower()
    return "ultralytics" in metadata_text

def _resolve_backend(
    metadata: dict[str, str],
    sidecar_config: dict[str, Any],
    input_layout: str,
    onnx_path: Path,
) -> tuple[str, str, str]:
    backend_override = str(sidecar_config.get("backend", "")).lower()
    preprocessing = str(sidecar_config.get("preprocessing", "")).lower()
    output_kind = str(sidecar_config.get("output", OUTPUT_AUTO)).lower()

    if output_kind not in {OUTPUT_AUTO, OUTPUT_PROBABILITIES, OUTPUT_LOGITS}:
        logger.warning(f"Unknown output kind '{output_kind}' for {onnx_path}; using auto.")
        output_kind = OUTPUT_AUTO

    if backend_override in {"yolo", "yolo_cls", BACKEND_YOLO_CLASSIFY} or _is_ultralytics_classify(metadata, sidecar_config):
        return BACKEND_YOLO_CLASSIFY, PREPROCESS_RAW, OUTPUT_PROBABILITIES

    if backend_override in {"keras", "tensorflow", "tf", "nhwc", BACKEND_ONNX_NHWC_RAW}:
        return BACKEND_ONNX_NHWC_RAW, PREPROCESS_RAW, output_kind

    if backend_override in {"pytorch", "torch", "nchw", BACKEND_ONNX_NCHW}:
        return BACKEND_ONNX_NCHW, preprocessing or PREPROCESS_MINUS_ONE_ONE, output_kind

    if input_layout == LAYOUT_NHWC:
        return BACKEND_ONNX_NHWC_RAW, PREPROCESS_RAW, output_kind

    if input_layout == LAYOUT_NCHW:
        if preprocessing:
            return BACKEND_ONNX_NCHW, preprocessing, output_kind
        logger.warning(
            f"{onnx_path} is NCHW but not identifiable as Ultralytics YOLO. "
            "Using legacy PyTorch preprocessing ((x/255 - 0.5) / 0.5). "
            "Add a sidecar JSON with backend/preprocessing to make this explicit."
        )
        return BACKEND_ONNX_NCHW, PREPROCESS_MINUS_ONE_ONE, output_kind

    return BACKEND_UNKNOWN, preprocessing or PREPROCESS_RAW, output_kind

def _softmax(values: np.ndarray) -> np.ndarray:
    shifted = values - np.max(values)
    exp_values = np.exp(shifted)
    return exp_values / np.sum(exp_values)

def _looks_like_probabilities(values: np.ndarray) -> bool:
    if values.size == 0 or not np.all(np.isfinite(values)):
        return False
    total = float(np.sum(values))
    return float(np.min(values)) >= -1e-5 and float(np.max(values)) <= 1.00001 and abs(total - 1.0) <= 1e-3

def _normalize_output(raw_output: np.ndarray, output_kind: str) -> np.ndarray:
    values = np.asarray(raw_output, dtype=np.float64).squeeze()
    if values.ndim != 1:
        raise ValueError(f"Classification output must be 1D after squeeze, got shape {values.shape}.")
    if not np.all(np.isfinite(values)):
        raise ValueError("Classification output contains non-finite values.")

    if output_kind == OUTPUT_LOGITS:
        return _softmax(values)

    if output_kind == OUTPUT_PROBABILITIES:
        total = float(np.sum(values))
        if total <= 0:
            raise ValueError("Probability output has non-positive sum.")
        return values / total

    if _looks_like_probabilities(values):
        return values / np.sum(values)
    return _softmax(values)

def _class_name_list(class_names: dict[int, str]) -> list[str]:
    keys = sorted(class_names.keys())
    expected = list(range(len(keys)))
    if keys != expected:
        raise ValueError(
            "Class names must use contiguous zero-based indexes. "
            f"Got indexes {keys[:5]}...{keys[-5:] if keys else []}."
        )
    return [class_names[i] for i in expected]

def _ensure_rgb(image: PIL.Image.Image) -> PIL.Image.Image:
    return image if image.mode == "RGB" else image.convert("RGB")

def _resize_rgb(image: PIL.Image.Image, size: tuple[int, int]) -> PIL.Image.Image:
    return _ensure_rgb(image).resize(size, resample=RESAMPLE_BILINEAR)

def _apply_nchw_preprocessing(img_np: np.ndarray, preprocessing: str) -> np.ndarray:
    if preprocessing in {"", PREPROCESS_MINUS_ONE_ONE, "legacy", "legacy_minus_one_one"}:
        return (img_np / 255.0 - 0.5) / 0.5
    if preprocessing in {PREPROCESS_ZERO_ONE, "0_1", "ultralytics", "yolo"}:
        return img_np / 255.0
    if preprocessing in {PREPROCESS_TORCH_IMAGENET, "imagenet"}:
        values = img_np / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(1, 3, 1, 1)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(1, 3, 1, 1)
        return (values - mean) / std
    if preprocessing == PREPROCESS_RAW:
        return img_np
    raise ValueError(f"Unsupported NCHW preprocessing mode: {preprocessing}")

def _get_yolo_classification_model(model_path: Path) -> YOLO:
    model_key = str(model_path.resolve())
    with YOLO_CLASSIFICATION_MODELS_LOCK:
        if model_key not in YOLO_CLASSIFICATION_MODELS:
            logger.info(f"Loading YOLO classification model from {model_path}")
            YOLO_CLASSIFICATION_MODELS[model_key] = YOLO(str(model_path), task="classify")
    return YOLO_CLASSIFICATION_MODELS[model_key]

def _yolo_imgsz_arg(imgsz: tuple[int, int]) -> int | tuple[int, int]:
    # ENIMAS stores sizes in PIL order (width, height), while Ultralytics
    # expects non-square imgsz tuples as (height, width).
    width, height = imgsz
    return height if width == height else (height, width)

def _predict_yolo_classification_onnx_direct(model_metadata: Dict, image: PIL.Image.Image) -> np.ndarray:
    width, height = model_metadata["imgsz"]
    # ENIMAS pins ultralytics==8.2.0; use its classification transform directly
    # for non-square ONNX inputs because YOLO.predict only applies self.imgsz[0].
    transform = classify_transforms(size=(height, width))
    img_tensor = transform(_ensure_rgb(image)).unsqueeze(0)
    img_np = img_tensor.cpu().numpy().astype(np.float32)

    session = model_metadata["session"]
    result = session.run(None, {model_metadata["input_name"]: img_np})
    return _normalize_output(result[0], OUTPUT_PROBABILITIES)

def _predict_yolo_classification(model_metadata: Dict, image: PIL.Image.Image) -> np.ndarray:
    if model_metadata["imgsz"][0] != model_metadata["imgsz"][1]:
        logger.info(
            f"Using direct ONNX Runtime inference for non-square YOLO classification model "
            f"{model_metadata['path'].name}."
        )
        return _predict_yolo_classification_onnx_direct(model_metadata, image)

    yolo_model = _get_yolo_classification_model(model_metadata["path"])
    result = yolo_model.predict(_ensure_rgb(image), imgsz=_yolo_imgsz_arg(model_metadata["imgsz"]), verbose=False)
    probs = getattr(result[0], "probs", None)
    if probs is None:
        raise ValueError(f"YOLO model {model_metadata['path']} did not return classification probabilities.")
    return _normalize_output(probs.data.cpu().numpy(), OUTPUT_PROBABILITIES)

def _predict_onnx_nhwc_raw(model_metadata: Dict, image: PIL.Image.Image) -> np.ndarray:
    img = _resize_rgb(image, model_metadata["imgsz"])
    img_np = np.array(img).astype(np.float32)[np.newaxis, :]
    session = model_metadata["session"]
    result = session.run(None, {model_metadata["input_name"]: img_np})
    return _normalize_output(result[0], model_metadata["output_kind"])

def _predict_onnx_nchw(model_metadata: Dict, image: PIL.Image.Image) -> np.ndarray:
    img = _resize_rgb(image, model_metadata["imgsz"])
    img_np = np.array(img).astype(np.float32)
    img_np = np.transpose(img_np, (2, 0, 1))[np.newaxis, :]
    img_np = _apply_nchw_preprocessing(img_np, model_metadata["preprocessing"])

    session = model_metadata["session"]
    result = session.run(None, {model_metadata["input_name"]: img_np.astype(np.float32)})
    return _normalize_output(result[0], model_metadata["output_kind"])

def predict_model(model_metadata: Dict, image: PIL.Image.Image) -> np.ndarray:
    backend = model_metadata["backend"]
    if backend == BACKEND_YOLO_CLASSIFY:
        return _predict_yolo_classification(model_metadata, image)
    if backend == BACKEND_ONNX_NHWC_RAW:
        return _predict_onnx_nhwc_raw(model_metadata, image)
    if backend == BACKEND_ONNX_NCHW:
        return _predict_onnx_nchw(model_metadata, image)
    raise ValueError(
        f"Could not determine inference backend for {model_metadata['path']}. "
        "Add a sidecar JSON with backend/preprocessing metadata."
    )

def validate_ensemble_models(models: List[Dict]) -> dict[int, str]:
    if not models:
        raise ValueError("No ONNX models loaded for ensemble.")

    reference_names = models[0]["names"]
    reference_items = _class_name_list(reference_names)
    for model_metadata in models[1:]:
        names = model_metadata["names"]
        items = _class_name_list(names)
        if items != reference_items:
            raise ValueError(
                "Ensemble class mapping mismatch. "
                f"{models[0]['path'].name} and {model_metadata['path'].name} do not share identical class order."
            )
    return reference_names

def load_onnx_model_metadata(onnx_path: Path) -> Dict:
    """
    Loads an ONNX model and extracts its metadata.

    Args:
        onnx_path (Path): Path to the ONNX model file.

    Returns:
        Dict: A dictionary containing model metadata such as session, image size, class names, task, and batch size.
    """
    session = ort.InferenceSession(str(onnx_path))
    metadata = session.get_modelmeta().custom_metadata_map
    sidecar_config = _read_sidecar_config(onnx_path)

    inputs = session.get_inputs()
    outputs = session.get_outputs()
    input_shape = list(inputs[0].shape)
    output_shape = list(outputs[0].shape)
    output_dim = _as_int_dim(output_shape[-1]) if output_shape else None
    input_layout = _shape_layout(input_shape)

    class_names = _parse_class_names(metadata, sidecar_config, output_dim)
    imgsz = _parse_image_size(metadata, sidecar_config, input_shape, input_layout)
    backend, preprocessing, output_kind = _resolve_backend(metadata, sidecar_config, input_layout, onnx_path)
    if output_dim and output_dim != len(class_names):
        logger.warning(
            f"{onnx_path} output dimension is {output_dim}, but metadata contains {len(class_names)} classes."
        )

    logger.debug(
        f"Loaded {onnx_path}: backend={backend}, preprocessing={preprocessing}, "
        f"output={output_kind}, input_shape={input_shape}, imgsz={imgsz}."
    )

    return {
        "path": onnx_path,
        "session": session,
        "imgsz": imgsz,
        "names": class_names,
        "task": metadata.get("task", "classify"),
        "batch": int(metadata.get("batch", 1)),
        "metadata": metadata,
        "sidecar_config": sidecar_config,
        "input_name": inputs[0].name,
        "input_shape": input_shape,
        "input_layout": input_layout,
        "output_name": outputs[0].name,
        "output_shape": output_shape,
        "backend": backend,
        "preprocessing": preprocessing,
        "output_kind": output_kind,
    }

class Predictor(Thread):
    def __init__(self, image_path: str, chosen_model_names: list[str], fuse_status_signal: pyqtBoundSignal, prediction_done: pyqtBoundSignal) -> None:
        """
        Initializes the Predictor object for image classification.

        Args:
            image_path (str): Path to the image to classify.
            chosen_model_names (list[str]): List of model names to use for prediction.
            fuse_status_signal (pyqtBoundSignal): Signal to emit status updates.
            prediction_done (pyqtBoundSignal): Signal to emit when prediction is complete.
        """
        super().__init__()
        self.image_path = Path(image_path)

        stem = self.image_path.stem
        parts = stem.split('_', 1)
        if len(parts) == 2:
            self.suffix = '_' + parts[1]
        else:
            self.suffix = 'Raw'

        self.fuse_status_signal = fuse_status_signal
        self.prediction_done = prediction_done
        self.model_names = chosen_model_names

    def run(self):
        """Run the classification process."""
        self.classify_image()

    def get_cropped_image(self, confidence_threshold: float = 0.6) -> PIL.Image.Image:
        """
        Crops the input image using the YOLO detection model.

        Args:
            confidence_threshold (float): Confidence threshold for YOLO model to filter detections.

        Returns:
            PIL.Image.Image: Cropped image or original image if no detection is made.
        """
        logger.debug("Cropping image using YOLO model.")
        self.fuse_status_signal.emit("Cropping the image using the YOLO detection model.")
        with PIL.Image.open(self.image_path) as img:
            stacked_image = img.copy()

        detect_model: YOLO = YOLO_MODELS["crop"]
        detection_result = detect_model.predict(stacked_image, conf=confidence_threshold)
        boxes = detection_result[0].boxes.xyxy
        stacked_image_np = np.array(stacked_image)

        if len(boxes) > 0:
            # Crop the image based on detection box
            x1, y1, x2, y2 = map(int, boxes[0][:4])
            cropped_image_np = stacked_image_np[y1:y2, x1:x2]
            cropped_image_pil = PIL.Image.fromarray(cropped_image_np)
            stacked_image.close()
            logger.debug("Cropping successful.")
            self.fuse_status_signal.emit("Cropping complete.")
            return cropped_image_pil

        logger.debug("Cropping failed. Using original image.")
        return stacked_image

    def classify_image(self) -> None:
        """
        Classifies the image using the selected ONNX models and emits the results.
        """
        logger.debug(f"Starting image classification for {self.image_path}.")
        crop = constants.CROP_IMAGES and YOLO_MODELS["crop"] is not None
        stacked_image = None
        try:
            if not crop:
                with PIL.Image.open(self.image_path) as img:
                    stacked_image = img.copy()
            else:
                stacked_image = self.get_cropped_image()

            predictions = []
            predicted_classes = []

            for model_name in self.model_names:
                logger.debug(f"Classifying with model: {model_name}")
                model_pred = [f"{model_name}:"]
                models = ONNX_MODELS.get(model_name)

                if models is None:
                    logger.debug(f"No model found for {model_name}.")
                    model_pred.append(f" {self.image_path.name}")
                    model_pred.append("Prediction: Error")
                    model_pred.append("Probability: 0.0000")
                    model_pred.append(f"Status: No ONNX model found for {model_name}.")
                    predictions.append(model_pred)
                    predicted_classes.append(None)
                    continue

                self.fuse_status_signal.emit(f"Loading {model_name} ONNX models for prediction.")
                try:
                    class_names = validate_ensemble_models(models)
                    ensemble_outputs = np.zeros(len(class_names), dtype=np.float64)
                    model_count = len(models)
                    logger.debug(f"Using {model_count} models for ensemble in {model_name} folder.")

                    for model_metadata in models:
                        logger.info(
                            f"Classifying with {model_metadata['path'].name}: "
                            f"backend={model_metadata['backend']}, preprocessing={model_metadata['preprocessing']}"
                        )
                        probabilities = predict_model(model_metadata, stacked_image)
                        if probabilities.shape[0] != len(class_names):
                            raise ValueError(
                                f"Model {model_metadata['path'].name} returned {probabilities.shape[0]} classes, "
                                f"expected {len(class_names)}."
                            )
                        ensemble_outputs += probabilities

                    ensemble_outputs /= model_count
                    top1_index = int(np.argmax(ensemble_outputs))
                    top1_prob = float(np.max(ensemble_outputs))
                    top1_class_name = class_names[top1_index]
                except Exception as error:
                    logger.exception(f"Classification failed for {model_name}: {error}")
                    model_pred.append(f" {self.image_path.name}")
                    model_pred.append(f"Prediction: Error")
                    model_pred.append(f"Probability: 0.0000")
                    model_pred.append(f"Status: {error}")
                    predicted_classes.append(None)
                    predictions.append(model_pred)
                    continue

                model_pred.append(f" {self.image_path.name}")
                model_pred.append(f"Prediction: {top1_class_name}")
                model_pred.append(f"Probability: {top1_prob:.4f}")

                predicted_classes.append(top1_class_name.replace("_", " "))
                predictions.append(model_pred)

            self.prediction_done.emit(predictions, predicted_classes)
            predictions_string = "\n\n".join(["\n".join(block) for block in predictions])
            self.save_predictions_to_txt(predictions_string)
            logger.debug(f"Prediction complete. Results saved to {self.image_path.with_suffix('.txt')}.")
            self.fuse_status_signal.emit(f"Prediction complete. Results saved at {self.image_path.with_suffix('.txt')}")
        finally:
            if stacked_image is not None:
                stacked_image.close()

    def save_predictions_to_txt(self, txt: str) -> None:
        """
        Saves the predictions to a text file.

        Args:
            txt (str): Text containing the predictions.
        """
        #save_path = self.image_path.with_suffix(".txt")
        save_path = self.image_path.with_name(f"{self.image_path.stem}_classification.txt")
        logger.debug(f"Saving predictions to {save_path}.")
        with open(save_path, 'w') as file:
            file.write(txt)
