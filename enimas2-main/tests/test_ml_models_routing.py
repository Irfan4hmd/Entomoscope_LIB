import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import onnx
from PIL import Image
from ultralytics import YOLO
from onnx import TensorProto, helper, numpy_helper

from Tools import ml_models


REPO_ROOT = Path(__file__).resolve().parents[1]
YOLO_MODEL = REPO_ROOT / "models" / "classification" / "Agrilus_M" / "yolov8m_flipud640v2.onnx"
YOLO_MODEL_2 = REPO_ROOT / "models" / "classification" / "test" / "yolov8m_flipud640v2.onnx"
KERAS_MODEL = REPO_ROOT / "models" / "classification" / "MPTD" / "beitv2_best_model.onnx"


def write_constant_classifier(path, input_shape, output_values, metadata):
    input_info = helper.make_tensor_value_info("images", TensorProto.FLOAT, input_shape)
    output_array = np.asarray(output_values, dtype=np.float32)
    output_info = helper.make_tensor_value_info("output", TensorProto.FLOAT, list(output_array.shape))
    output_tensor = numpy_helper.from_array(output_array, name="constant_output")
    output_node = helper.make_node("Constant", [], ["output"], value=output_tensor)
    graph = helper.make_graph([output_node], "synthetic_classifier", [input_info], [output_info])
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    model.ir_version = 9
    for key, value in metadata.items():
        prop = model.metadata_props.add()
        prop.key = key
        prop.value = value
    onnx.save(model, path)


class ClassificationRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sample_image = Image.new("RGB", (320, 240), color=(96, 128, 160))

    @unittest.skipUnless(YOLO_MODEL.exists(), "YOLO fixture model is not available")
    def test_ultralytics_model_routes_by_metadata_not_filename(self):
        metadata = ml_models.load_onnx_model_metadata(YOLO_MODEL)

        self.assertEqual(metadata["backend"], ml_models.BACKEND_YOLO_CLASSIFY)
        self.assertEqual(metadata["input_layout"], "NCHW")
        self.assertEqual(metadata["metadata"]["author"], "Ultralytics")
        self.assertEqual(metadata["metadata"]["task"], "classify")

    def test_synthetic_ultralytics_metadata_routes_yolo_without_filename(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            model_path = Path(tmpdir) / "renamed_model.onnx"
            write_constant_classifier(
                model_path,
                input_shape=[1, 3, 320, 320],
                output_values=[[0.2, 0.8]],
                metadata={
                    "author": "Ultralytics",
                    "task": "classify",
                    "imgsz": json.dumps([320, 320]),
                    "names": json.dumps({0: "first", 1: "second"}),
                },
            )

            metadata = ml_models.load_onnx_model_metadata(model_path)

        self.assertEqual(metadata["backend"], ml_models.BACKEND_YOLO_CLASSIFY)
        self.assertEqual(metadata["input_layout"], "NCHW")
        self.assertEqual(metadata["imgsz"], (320, 320))
        self.assertEqual(metadata["names"], {0: "first", 1: "second"})

    def test_synthetic_non_square_yolo_predicts_with_direct_onnx_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            model_path = Path(tmpdir) / "renamed_non_square.onnx"
            write_constant_classifier(
                model_path,
                input_shape=[1, 3, 128, 64],
                output_values=[[0.1, 0.9]],
                metadata={
                    "author": "Ultralytics",
                    "task": "classify",
                    "imgsz": json.dumps([128, 64]),
                    "names": json.dumps({0: "first", 1: "second"}),
                },
            )

            metadata = ml_models.load_onnx_model_metadata(model_path)
            probabilities = ml_models.predict_model(metadata, self.sample_image)

        self.assertEqual(metadata["backend"], ml_models.BACKEND_YOLO_CLASSIFY)
        self.assertEqual(metadata["imgsz"], (64, 128))
        np.testing.assert_allclose(probabilities, np.array([0.1, 0.9]), rtol=1e-6)

    @unittest.skipUnless(KERAS_MODEL.exists(), "Keras fixture model is not available")
    def test_keras_model_routes_to_nhwc_raw_path(self):
        metadata = ml_models.load_onnx_model_metadata(KERAS_MODEL)

        self.assertEqual(metadata["backend"], ml_models.BACKEND_ONNX_NHWC_RAW)
        self.assertEqual(metadata["input_layout"], "NHWC")
        self.assertEqual(metadata["imgsz"], (224, 224))
        self.assertEqual(len(metadata["names"]), 63)

    def test_synthetic_nhwc_model_routes_and_predicts_raw_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            model_path = Path(tmpdir) / "keras_like.onnx"
            write_constant_classifier(
                model_path,
                input_shape=[1, 224, 224, 3],
                output_values=[[0.25, 0.75]],
                metadata={"names": json.dumps({0: "low", 1: "high"})},
            )

            metadata = ml_models.load_onnx_model_metadata(model_path)
            probabilities = ml_models.predict_model(metadata, self.sample_image)

        self.assertEqual(metadata["backend"], ml_models.BACKEND_ONNX_NHWC_RAW)
        self.assertEqual(metadata["input_layout"], "NHWC")
        self.assertEqual(metadata["imgsz"], (224, 224))
        np.testing.assert_allclose(probabilities, np.array([0.25, 0.75]), rtol=1e-6)

    def test_synthetic_nchw_model_routes_to_legacy_preprocessing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            model_path = Path(tmpdir) / "generic_nchw.onnx"
            write_constant_classifier(
                model_path,
                input_shape=[1, 3, 128, 64],
                output_values=[[1.0, 2.0]],
                metadata={"names": json.dumps({0: "left", 1: "right"})},
            )

            metadata = ml_models.load_onnx_model_metadata(model_path)
            probabilities = ml_models.predict_model(metadata, self.sample_image)

        self.assertEqual(metadata["backend"], ml_models.BACKEND_ONNX_NCHW)
        self.assertEqual(metadata["input_layout"], "NCHW")
        self.assertEqual(metadata["imgsz"], (64, 128))
        self.assertEqual(metadata["preprocessing"], ml_models.PREPROCESS_MINUS_ONE_ONE)
        self.assertEqual(int(np.argmax(probabilities)), 1)

    @unittest.skipUnless(YOLO_MODEL.exists(), "YOLO fixture model is not available")
    def test_yolo_prediction_matches_ultralytics_reference(self):
        metadata = ml_models.load_onnx_model_metadata(YOLO_MODEL)
        routed_probs = ml_models.predict_model(metadata, self.sample_image)

        reference_model = YOLO(str(YOLO_MODEL), task="classify")
        reference_result = reference_model.predict(self.sample_image, imgsz=640, verbose=False)[0]
        reference_probs = reference_result.probs.data.cpu().numpy()

        np.testing.assert_allclose(routed_probs, reference_probs, rtol=1e-6, atol=1e-8)
        self.assertAlmostEqual(float(routed_probs.sum()), 1.0, places=6)

    @unittest.skipUnless(KERAS_MODEL.exists(), "Keras fixture model is not available")
    def test_keras_prediction_returns_probabilities(self):
        metadata = ml_models.load_onnx_model_metadata(KERAS_MODEL)
        probabilities = ml_models.predict_model(metadata, self.sample_image)

        self.assertEqual(probabilities.shape, (63,))
        self.assertAlmostEqual(float(probabilities.sum()), 1.0, places=5)
        self.assertGreaterEqual(float(probabilities.min()), 0.0)

    @unittest.skipUnless(
        YOLO_MODEL.exists() and YOLO_MODEL_2.exists(),
        "YOLO ensemble fixture models are not available",
    )
    def test_yolo_ensemble_with_same_class_order_is_allowed(self):
        metadata_a = ml_models.load_onnx_model_metadata(YOLO_MODEL)
        metadata_b = ml_models.load_onnx_model_metadata(YOLO_MODEL_2)

        class_names = ml_models.validate_ensemble_models([metadata_a, metadata_b])

        self.assertEqual(len(class_names), 63)

    @unittest.skipUnless(YOLO_MODEL.exists(), "YOLO fixture model is not available")
    def test_ensemble_class_order_mismatch_is_rejected(self):
        metadata_a = ml_models.load_onnx_model_metadata(YOLO_MODEL)
        metadata_b = dict(metadata_a)
        metadata_b["names"] = dict(metadata_b["names"])
        metadata_b["names"][0] = "different_class"

        with self.assertRaises(ValueError):
            ml_models.validate_ensemble_models([metadata_a, metadata_b])

    @unittest.skipUnless(YOLO_MODEL.exists(), "YOLO fixture model is not available")
    def test_ensemble_non_contiguous_class_indexes_are_rejected(self):
        metadata = ml_models.load_onnx_model_metadata(YOLO_MODEL)
        broken = dict(metadata)
        broken["names"] = {1: "class_one"}

        with self.assertRaises(ValueError):
            ml_models.validate_ensemble_models([broken])

    def test_sidecar_can_route_metadata_stripped_yolo(self):
        backend, preprocessing, output_kind = ml_models._resolve_backend(
            metadata={"task": "classify"},
            sidecar_config={"backend": "ultralytics_yolo_cls"},
            input_layout="NCHW",
            onnx_path=Path("renamed_model.onnx"),
        )

        self.assertEqual(backend, ml_models.BACKEND_YOLO_CLASSIFY)
        self.assertEqual(preprocessing, ml_models.PREPROCESS_RAW)
        self.assertEqual(output_kind, ml_models.OUTPUT_PROBABILITIES)

    def test_fixed_input_shape_controls_non_square_resize_size(self):
        nchw_size = ml_models._parse_image_size(
            metadata={"imgsz": "[640, 320]"},
            sidecar_config={},
            input_shape=[1, 3, 480, 240],
            input_layout="NCHW",
        )
        nhwc_size = ml_models._parse_image_size(
            metadata={"imgsz": "[640, 320]"},
            sidecar_config={},
            input_shape=[1, 480, 240, 3],
            input_layout="NHWC",
        )

        self.assertEqual(nchw_size, (240, 480))
        self.assertEqual(nhwc_size, (240, 480))

    def test_yolo_imgsz_arg_uses_ultralytics_height_width_order(self):
        self.assertEqual(ml_models._yolo_imgsz_arg((640, 640)), 640)
        self.assertEqual(ml_models._yolo_imgsz_arg((240, 480)), (480, 240))

    def test_shape_layout_uses_layout_constants(self):
        self.assertEqual(ml_models._shape_layout([1, 3, 224, 224]), ml_models.LAYOUT_NCHW)
        self.assertEqual(ml_models._shape_layout([1, 224, 224, 3]), ml_models.LAYOUT_NHWC)
        self.assertEqual(ml_models._shape_layout([1, 224, 224]), ml_models.LAYOUT_UNKNOWN)

    def test_dynamic_shape_uses_metadata_height_width_order(self):
        size = ml_models._parse_image_size(
            metadata={"imgsz": "[480, 240]"},
            sidecar_config={},
            input_shape=[1, 3, "height", "width"],
            input_layout="NCHW",
        )

        self.assertEqual(size, (240, 480))

    def test_prediction_text_keeps_error_status_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "sample.jpg"
            Image.new("RGB", (10, 10), color=(0, 0, 0)).save(image_path)
            predictor = ml_models.Predictor(str(image_path), [], None, None)
            predictor.save_predictions_to_txt(
                "ModelA:\n"
                " sample.jpg\n"
                "Prediction: Error\n"
                "Probability: 0.0000\n"
                "Status: No ONNX model found for ModelA."
            )

            saved_text = image_path.with_name("sample_classification.txt").read_text()

        self.assertIn("Prediction: Error", saved_text)
        self.assertIn("Status: No ONNX model found for ModelA.", saved_text)

    def test_logits_are_softmaxed_for_probability_reporting(self):
        logits = np.array([1.0, 2.0, 3.0], dtype=np.float32)

        probabilities = ml_models._normalize_output(logits, ml_models.OUTPUT_AUTO)

        self.assertAlmostEqual(float(probabilities.sum()), 1.0, places=6)
        self.assertEqual(int(np.argmax(probabilities)), 2)


if __name__ == "__main__":
    unittest.main()
