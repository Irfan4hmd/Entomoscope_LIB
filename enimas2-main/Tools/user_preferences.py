"""Validated persistence for user-selected output preferences."""

import constants


def normalize_stack_output_extension(value) -> str:
    """Return the supported final-output extension, defaulting safely to TIFF."""
    normalized = str(value).strip().lower() if value is not None else ""
    return normalized if normalized in {"tiff", "jpg"} else "tiff"


def parse_config_bool(value, default=False) -> bool:
    """Parse a config boolean without allowing invalid values to affect startup."""
    normalized = str(value).strip().lower() if value is not None else ""
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return default


def load_output_preferences(config) -> None:
    """Load validated output preferences into their runtime state."""
    constants.STACK_OUTPUT_EXTENSION = normalize_stack_output_extension(
        config.get("stack-output-extension")
    )
    constants.SHOW_SCALEBAR = parse_config_bool(config.get("show-scalebar"))


def store_output_preferences(config) -> None:
    """Store only canonical output preferences while preserving other config keys."""
    config["stack-output-extension"] = normalize_stack_output_extension(
        constants.STACK_OUTPUT_EXTENSION
    )
    config["show-scalebar"] = "true" if constants.SHOW_SCALEBAR else "false"
