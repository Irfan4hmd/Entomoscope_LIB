import unittest
import warnings

warnings.filterwarnings("ignore", message="You are using a Python version", category=FutureWarning)

from plugins.classification.plugin import Plugin


class _DummyCombo:
    def currentText(self):
        return "original"


class _DummyWindow:
    VariantCombo = _DummyCombo()


class ClassificationPluginResultTests(unittest.TestCase):
    def make_plugin(self):
        plugin = Plugin()
        plugin.window = _DummyWindow()
        plugin.results = []
        return plugin

    def test_success_block_without_status_is_backward_compatible(self):
        plugin = self.make_plugin()

        plugin.on_prediction_done(
            [["ModelA:", " image.jpg", "Prediction: class_a", "Probability: 0.7500"]],
            ["class a"],
        )

        self.assertEqual(
            plugin.results,
            [
                {
                    "image": "image.jpg",
                    "variant": "original",
                    "model": "ModelA",
                    "prediction": "class_a",
                    "probability": 0.75,
                    "status": "prediction done",
                }
            ],
        )

    def test_error_block_status_line_is_parsed_cleanly(self):
        plugin = self.make_plugin()

        plugin.on_prediction_done(
            [
                [
                    "ModelB:",
                    " image.jpg",
                    "Prediction: Error",
                    "Probability: 0.0000",
                    "Status: No ONNX model found for ModelB.",
                ]
            ],
            [None],
        )

        self.assertEqual(plugin.results[0]["image"], "image.jpg")
        self.assertEqual(plugin.results[0]["prediction"], "Error")
        self.assertEqual(plugin.results[0]["probability"], 0.0)
        self.assertEqual(plugin.results[0]["status"], "No ONNX model found for ModelB.")


if __name__ == "__main__":
    unittest.main()
