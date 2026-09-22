import unittest

import constants
from Tools.user_preferences import (
    load_output_preferences,
    normalize_stack_output_extension,
    parse_config_bool,
    store_output_preferences,
)


class OutputPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.previous_extension = constants.STACK_OUTPUT_EXTENSION
        self.previous_scalebar = constants.SHOW_SCALEBAR

    def tearDown(self):
        constants.STACK_OUTPUT_EXTENSION = self.previous_extension
        constants.SHOW_SCALEBAR = self.previous_scalebar

    def test_normalize_stack_output_extension_defaults_missing_or_invalid_to_tiff(self):
        for value in (None, "", "png", " tiffx "):
            with self.subTest(value=value):
                self.assertEqual(normalize_stack_output_extension(value), "tiff")

    def test_normalize_stack_output_extension_accepts_case_normalized_values(self):
        self.assertEqual(normalize_stack_output_extension(" TIFF "), "tiff")
        self.assertEqual(normalize_stack_output_extension("JpG"), "jpg")

    def test_parse_config_bool_defaults_invalid_values(self):
        for value in (None, "", "yes", "1"):
            with self.subTest(value=value):
                self.assertFalse(parse_config_bool(value))
        self.assertTrue(parse_config_bool("invalid", default=True))

    def test_parse_config_bool_accepts_case_normalized_values(self):
        self.assertTrue(parse_config_bool(" TRUE "))
        self.assertFalse(parse_config_bool("False", default=True))

    def test_load_output_preferences_defaults_missing_or_invalid_values(self):
        for config in ({}, {"stack-output-extension": "png", "show-scalebar": "yes"}):
            with self.subTest(config=config):
                load_output_preferences(config)

                self.assertEqual(constants.STACK_OUTPUT_EXTENSION, "tiff")
                self.assertFalse(constants.SHOW_SCALEBAR)

    def test_load_output_preferences_accepts_case_normalized_values(self):
        load_output_preferences({"stack-output-extension": " JPG ", "show-scalebar": "TRUE"})

        self.assertEqual(constants.STACK_OUTPUT_EXTENSION, "jpg")
        self.assertTrue(constants.SHOW_SCALEBAR)

    def test_store_output_preferences_serializes_canonical_values_and_preserves_other_keys(self):
        constants.STACK_OUTPUT_EXTENSION = " TIFF "
        constants.SHOW_SCALEBAR = True
        config = {"unrelated": "preserved"}

        store_output_preferences(config)

        self.assertEqual(
            config,
            {
                "unrelated": "preserved",
                "stack-output-extension": "tiff",
                "show-scalebar": "true",
            },
        )

    def test_store_and_load_output_preferences_round_trip_restart_values(self):
        for extension, scalebar in (("tiff", False), ("jpg", True)):
            with self.subTest(extension=extension, scalebar=scalebar):
                constants.STACK_OUTPUT_EXTENSION = extension
                constants.SHOW_SCALEBAR = scalebar
                config = {}
                store_output_preferences(config)

                constants.STACK_OUTPUT_EXTENSION = "tiff"
                constants.SHOW_SCALEBAR = False
                load_output_preferences(config)

                self.assertEqual(constants.STACK_OUTPUT_EXTENSION, extension)
                self.assertEqual(constants.SHOW_SCALEBAR, scalebar)
