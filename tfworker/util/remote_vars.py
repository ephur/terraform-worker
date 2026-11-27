"""Utilities for parsing and processing remote_vars configurations."""

import re
from typing import Any, Dict, List, Set, Tuple, Union

# Type alias for remote_vars values
RemoteVarValue = Union[str, Dict[str, Any], List[Any]]


def parse_remote_var_reference(value: str) -> Tuple[str, str | None]:
    """
    Parse a remote var reference into state name and optional output key path.

    Supports multiple formats:
    - "state.outputs" -> entire outputs dict from state
    - "state.outputs.key" -> specific output key from state
    - "state.outputs.key.nested" -> nested key with dot notation
    - "state.outputs.key["index"]" -> key with bracket notation
    - "state.outputs.key.nested["index"]" -> mixed notation

    Args:
        value: Remote var reference string (e.g., "network1.outputs.vpc_id")

    Returns:
        Tuple of (state_name, output_key_path_or_None)
        - If output_key_path is None, the entire outputs dict is requested
        - If output_key_path is present, it's the path to the output (may include nested keys)

    Raises:
        ValueError: If the reference format is invalid

    Examples:
        >>> parse_remote_var_reference("network1.outputs")
        ('network1', None)

        >>> parse_remote_var_reference("network1.outputs.vpc_id")
        ('network1', 'vpc_id')

        >>> parse_remote_var_reference("network1.outputs.data.nested_key")
        ('network1', 'data.nested_key')

        >>> parse_remote_var_reference("network1.outputs.items[\"test_key\"]")
        ('network1', 'items["test_key"]')
    """
    # Split on '.outputs' to separate state from key path
    if ".outputs" not in value:
        raise ValueError(
            f"Invalid remote var reference: '{value}'. "
            f"Expected format: 'state.outputs[.key.path]' or 'state.outputs[\"key\"]'"
        )

    parts = value.split(".outputs", 1)
    state = parts[0]
    key_path = parts[1] if len(parts) > 1 else ""

    # Validate state name (must be word characters only)
    if not re.match(r"^\w+$", state):
        raise ValueError(
            f"Invalid remote var reference: '{value}'. "
            f"State name must contain only alphanumeric characters and underscores."
        )

    # If there's a key path, validate it starts with . or [
    if key_path:
        if not key_path.startswith(".") and not key_path.startswith("["):
            raise ValueError(
                f"Invalid remote var reference: '{value}'. "
                f"Key path after 'outputs' must start with '.' or '[', got: '{key_path}'"
            )

        # If using pure dot notation (no brackets), validate each segment
        if "[" not in key_path:
            # Remove leading '.'
            clean_path = key_path[1:] if key_path.startswith(".") else key_path
            # Split on dots and validate each segment
            segments = clean_path.split(".")
            for segment in segments:
                if segment and not re.match(r"^\w+$", segment):
                    raise ValueError(
                        f"Invalid remote var reference: '{value}'. "
                        f"When using dot notation, each key segment must contain only "
                        f"alphanumeric characters and underscores. Invalid segment: '{segment}'. "
                        f"Use bracket notation for keys with special characters: 'state.outputs[\"{segment}\"]'"
                    )

        # Remove leading '.' for consistency
        if key_path.startswith("."):
            key_path = key_path[1:]
        return state, key_path
    else:
        return state, None


def generate_tf_reference(value: str) -> str:
    """
    Convert a remote var reference to Terraform HCL data source syntax.

    Args:
        value: Remote var reference string (e.g., "network1.outputs.vpc_id")

    Returns:
        Terraform HCL reference string

    Raises:
        ValueError: If the reference format is invalid

    Examples:
        >>> generate_tf_reference("network1.outputs")
        'data.terraform_remote_state.network1.outputs'

        >>> generate_tf_reference("network1.outputs.vpc_id")
        'data.terraform_remote_state.network1.outputs.vpc_id'

        >>> generate_tf_reference("network1.outputs.data.nested_key")
        'data.terraform_remote_state.network1.outputs.data.nested_key'

        >>> generate_tf_reference("network1.outputs.items[\"test_key\"]")
        'data.terraform_remote_state.network1.outputs.items["test_key"]'
    """
    state, key_path = parse_remote_var_reference(value)
    base_ref = f"data.terraform_remote_state.{state}.outputs"
    if key_path:
        # If key_path starts with '[', don't add a dot
        if key_path.startswith("["):
            return f"{base_ref}{key_path}"
        else:
            return f"{base_ref}.{key_path}"
    return base_ref


def extract_remote_states(value: RemoteVarValue) -> Set[str]:
    """
    Recursively extract all remote state names from a remote_vars value.

    Supports strings, dicts, and lists to handle complex nested structures.

    Args:
        value: A remote_vars value (string, dict, or list)

    Returns:
        Set of unique state names referenced in the value

    Examples:
        >>> extract_remote_states("network1.outputs.vpc_id")
        {'network1'}

        >>> extract_remote_states({"k1": "net1.outputs", "k2": "net2.outputs.vpc"})
        {'net1', 'net2'}

        >>> extract_remote_states(["net1.outputs", "net2.outputs.id"])
        {'net1', 'net2'}

        >>> extract_remote_states({
        ...     "vpcs": {
        ...         "platform": "net1.outputs",
        ...         "payments": "net2.outputs.vpc"
        ...     },
        ...     "env": "env_info.outputs.environment"
        ... })
        {'net1', 'net2', 'env_info'}
    """
    states = set()

    if isinstance(value, str):
        # Simple string reference
        state, _ = parse_remote_var_reference(value)
        states.add(state)
    elif isinstance(value, dict):
        # Dict: recursively process all values
        for v in value.values():
            states.update(extract_remote_states(v))
    elif isinstance(value, list):
        # List: recursively process all items
        for item in value:
            states.update(extract_remote_states(item))

    return states


def parse_tf_reference(tf_ref: str) -> Tuple[str, str | None]:
    """
    Parse a Terraform remote state reference into state name and optional output key path.

    This is the inverse of generate_tf_reference() - it parses Terraform HCL
    references back into their components.

    Args:
        tf_ref: Terraform HCL reference (e.g., "data.terraform_remote_state.net1.outputs.vpc_id")

    Returns:
        Tuple of (state_name, output_key_path_or_None)

    Raises:
        ValueError: If the reference format is invalid

    Examples:
        >>> parse_tf_reference("data.terraform_remote_state.network1.outputs")
        ('network1', None)

        >>> parse_tf_reference("data.terraform_remote_state.network1.outputs.vpc_id")
        ('network1', 'vpc_id')

        >>> parse_tf_reference("data.terraform_remote_state.network1.outputs.data.nested_key")
        ('network1', 'data.nested_key')

        >>> parse_tf_reference("data.terraform_remote_state.network1.outputs.items[\"test_key\"]")
        ('network1', 'items["test_key"]')
    """
    # Split on '.outputs' to separate the prefix from the key path
    prefix = "data.terraform_remote_state."
    if not tf_ref.startswith(prefix):
        raise ValueError(
            f"Invalid Terraform remote state reference: '{tf_ref}'. "
            f"Expected format: 'data.terraform_remote_state.state.outputs[.key.path]'"
        )

    remainder = tf_ref[len(prefix) :]
    if ".outputs" not in remainder:
        raise ValueError(
            f"Invalid Terraform remote state reference: '{tf_ref}'. "
            f"Expected format: 'data.terraform_remote_state.state.outputs[.key.path]'"
        )

    parts = remainder.split(".outputs", 1)
    state = parts[0]
    key_path = parts[1] if len(parts) > 1 else ""

    # Validate state name
    if not re.match(r"^\w+$", state):
        raise ValueError(
            f"Invalid Terraform remote state reference: '{tf_ref}'. "
            f"State name must contain only alphanumeric characters and underscores."
        )

    # Process key path
    if key_path:
        # Remove leading '.' if present
        if key_path.startswith("."):
            key_path = key_path[1:]
        return state, key_path if key_path else None
    else:
        return state, None


def validate_remote_vars(remote_vars: Dict[str, RemoteVarValue]) -> None:
    """
    Validate a remote_vars configuration dictionary.

    Checks that all string references are properly formatted and recursively
    validates nested structures.

    Args:
        remote_vars: Dictionary of remote variable configurations

    Raises:
        ValueError: If any reference is invalid

    Examples:
        >>> validate_remote_vars({"env": "env_info.outputs.environment"})
        # No exception - valid

        >>> validate_remote_vars({"bad": "invalid reference"})
        Traceback (most recent call last):
        ...
        ValueError: Invalid remote var reference...
    """
    for key, value in remote_vars.items():
        try:
            _validate_value(value)
        except ValueError as e:
            raise ValueError(f"Invalid remote_vars configuration for key '{key}': {e}")


def _validate_value(value: RemoteVarValue) -> None:
    """Recursively validate a remote_vars value."""
    if isinstance(value, str):
        # Validate string reference format
        parse_remote_var_reference(value)
    elif isinstance(value, dict):
        # Recursively validate dict values
        for v in value.values():
            _validate_value(v)
    elif isinstance(value, list):
        # Recursively validate list items
        for item in value:
            _validate_value(item)
    else:
        raise ValueError(
            f"Unsupported remote_vars value type: {type(value).__name__}. "
            f"Expected str, dict, or list."
        )
