"""Unit tests for tfworker.util.remote_vars module."""

import pytest

from tfworker.util.remote_vars import (
    extract_remote_states,
    generate_tf_reference,
    parse_remote_var_reference,
    parse_tf_reference,
    validate_remote_vars,
)


class TestParseRemoteVarReference:
    """Tests for parse_remote_var_reference() function."""

    @pytest.mark.parametrize(
        "input_ref,expected_state,expected_key",
        [
            ("network1.outputs", "network1", None),
            ("network1.outputs.vpc_id", "network1", "vpc_id"),
            ("env_info.outputs.environment", "env_info", "environment"),
            ("network123.outputs.vpc_id", "network123", "vpc_id"),
            (
                "remote_state.outputs.data.nested_value",
                "remote_state",
                "data.nested_value",
            ),
            (
                'remote_state.outputs.items["test_key"]',
                "remote_state",
                'items["test_key"]',
            ),
            (
                'remote_state.outputs.data["test_item"].id',
                "remote_state",
                'data["test_item"].id',
            ),
            (
                "state.outputs.level1.level2.level3.level4",
                "state",
                "level1.level2.level3.level4",
            ),
        ],
        ids=[
            "entire_outputs",
            "specific_key",
            "with_underscores",
            "with_numbers",
            "nested_dot",
            "nested_bracket",
            "nested_mixed",
            "deeply_nested",
        ],
    )
    def test_valid_references(self, input_ref, expected_state, expected_key):
        """Test parsing valid remote var references."""
        state, key = parse_remote_var_reference(input_ref)
        assert state == expected_state
        assert key == expected_key

    @pytest.mark.parametrize(
        "invalid_ref,expected_error",
        [
            ("network1.vpc_id", "Invalid remote var reference"),
            ("", "Invalid remote var reference"),
            ("network-1.outputs.vpc_id", "State name must contain only"),
            ("network1.outputs.vpc-id", "Use bracket notation"),
            (".outputs.vpc_id", "State name must contain only"),
        ],
        ids=[
            "missing_outputs",
            "empty_string",
            "special_chars_state",
            "special_chars_key",
            "missing_state",
        ],
    )
    def test_invalid_references(self, invalid_ref, expected_error):
        """Test that invalid references raise ValueError."""
        with pytest.raises(ValueError) as exc_info:
            parse_remote_var_reference(invalid_ref)
        assert expected_error in str(exc_info.value)


class TestGenerateTfReference:
    """Tests for generate_tf_reference() function."""

    @pytest.mark.parametrize(
        "input_ref,expected_output",
        [
            ("network1.outputs", "data.terraform_remote_state.network1.outputs"),
            (
                "network1.outputs.vpc_id",
                "data.terraform_remote_state.network1.outputs.vpc_id",
            ),
            (
                "env_info.outputs.environment",
                "data.terraform_remote_state.env_info.outputs.environment",
            ),
            (
                "remote_state.outputs.data.nested_value",
                "data.terraform_remote_state.remote_state.outputs.data.nested_value",
            ),
            (
                'remote_state.outputs.items["test_key"]',
                'data.terraform_remote_state.remote_state.outputs.items["test_key"]',
            ),
            (
                'remote_state.outputs.data["test_item"].id',
                'data.terraform_remote_state.remote_state.outputs.data["test_item"].id',
            ),
        ],
        ids=[
            "entire_outputs",
            "specific_key",
            "with_underscores",
            "nested_dot",
            "nested_bracket",
            "nested_mixed",
        ],
    )
    def test_valid_generation(self, input_ref, expected_output):
        """Test generating valid Terraform references."""
        assert generate_tf_reference(input_ref) == expected_output

    def test_invalid_format(self):
        """Test that invalid format raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            generate_tf_reference("invalid.reference")
        assert "Invalid remote var reference" in str(exc_info.value)


class TestExtractRemoteStates:
    """Tests for extract_remote_states() function."""

    @pytest.mark.parametrize(
        "value,expected_states",
        [
            ("network1.outputs.vpc_id", {"network1"}),
            ("network1.outputs", {"network1"}),
            ({"k1": "net1.outputs", "k2": "net2.outputs.vpc"}, {"net1", "net2"}),
            (
                ["net1.outputs", "net2.outputs.id", "net3.outputs.name"],
                {"net1", "net2", "net3"},
            ),
            ({}, set()),
            ([], set()),
        ],
        ids=[
            "simple_string",
            "string_entire_outputs",
            "dict_single_level",
            "list",
            "empty_dict",
            "empty_list",
        ],
    )
    def test_extract_basic_cases(self, value, expected_states):
        """Test extracting state names from various structures."""
        assert extract_remote_states(value) == expected_states

    def test_extract_from_nested_dict(self):
        """Test extracting state names from nested dict structure."""
        states = extract_remote_states(
            {
                "vpcs": {"platform": "net1.outputs", "payments": "net2.outputs.vpc"},
                "env": "env_info.outputs.environment",
            }
        )
        assert states == {"net1", "net2", "env_info"}

    def test_extract_from_nested_list_in_dict(self):
        """Test extracting state names from list nested in dict."""
        states = extract_remote_states(
            {
                "vpc_ids": ["net1.outputs.vpc_id", "net2.outputs.vpc_id"],
                "env": "env_info.outputs.environment",
            }
        )
        assert states == {"net1", "net2", "env_info"}

    def test_extract_from_dict_in_list(self):
        """Test extracting state names from dict nested in list."""
        states = extract_remote_states(
            [{"name": "net1.outputs.name"}, {"name": "net2.outputs.name"}]
        )
        assert states == {"net1", "net2"}

    def test_extract_from_deeply_nested_structure(self):
        """Test extracting state names from deeply nested structure."""
        states = extract_remote_states(
            {
                "networks": {
                    "production": {
                        "primary": "net1.outputs",
                        "secondary": "net2.outputs",
                    },
                    "staging": ["net3.outputs.vpc_id", "net4.outputs.vpc_id"],
                },
                "databases": [
                    {"primary": "db1.outputs.endpoint"},
                    {"replica": "db2.outputs.endpoint"},
                ],
            }
        )
        assert states == {"net1", "net2", "net3", "net4", "db1", "db2"}

    def test_extract_duplicate_states(self):
        """Test that duplicate state names are deduplicated."""
        states = extract_remote_states(
            {
                "vpc_id": "net1.outputs.vpc_id",
                "subnet_ids": "net1.outputs.subnet_ids",
                "cidr": "net1.outputs.cidr",
            }
        )
        assert states == {"net1"}


class TestParseTfReference:
    """Tests for parse_tf_reference() function."""

    @pytest.mark.parametrize(
        "tf_ref,expected_state,expected_key",
        [
            ("data.terraform_remote_state.network1.outputs", "network1", None),
            (
                "data.terraform_remote_state.network1.outputs.vpc_id",
                "network1",
                "vpc_id",
            ),
            (
                "data.terraform_remote_state.env_info.outputs.environment",
                "env_info",
                "environment",
            ),
            (
                "data.terraform_remote_state.remote_state.outputs.data.nested_value",
                "remote_state",
                "data.nested_value",
            ),
            (
                'data.terraform_remote_state.remote_state.outputs.items["test_key"]',
                "remote_state",
                'items["test_key"]',
            ),
            (
                'data.terraform_remote_state.remote_state.outputs.data["test_item"].id',
                "remote_state",
                'data["test_item"].id',
            ),
        ],
        ids=[
            "entire_outputs",
            "specific_key",
            "with_underscores",
            "nested_dot",
            "nested_bracket",
            "nested_mixed",
        ],
    )
    def test_valid_parsing(self, tf_ref, expected_state, expected_key):
        """Test parsing valid Terraform references."""
        state, key = parse_tf_reference(tf_ref)
        assert state == expected_state
        assert key == expected_key

    @pytest.mark.parametrize(
        "invalid_ref,expected_error",
        [
            (
                "terraform_remote_state.network1.outputs",
                "Invalid Terraform remote state reference",
            ),
            (
                "data.remote_state.network1.outputs",
                "Invalid Terraform remote state reference",
            ),
            (
                "data.terraform_remote_state.network1.vpc_id",
                "Invalid Terraform remote state reference",
            ),
            ("", "Invalid Terraform remote state reference"),
        ],
        ids=[
            "missing_data_prefix",
            "wrong_middle",
            "missing_outputs",
            "empty_string",
        ],
    )
    def test_invalid_parsing(self, invalid_ref, expected_error):
        """Test that invalid references raise ValueError."""
        with pytest.raises(ValueError) as exc_info:
            parse_tf_reference(invalid_ref)
        assert expected_error in str(exc_info.value)


class TestValidateRemoteVars:
    """Tests for validate_remote_vars() function."""

    @pytest.mark.parametrize(
        "config",
        [
            {"env": "env_info.outputs.environment"},
            {"vpcs": {"platform": "net1.outputs", "payments": "net2.outputs.vpc"}},
            {"vpc_ids": ["net1.outputs.vpc_id", "net2.outputs.vpc_id"]},
            {
                "networks": {
                    "production": {
                        "primary": "net1.outputs",
                        "secondary": "net2.outputs",
                    }
                },
                "databases": ["db1.outputs.endpoint"],
            },
            {},
        ],
        ids=[
            "simple_string",
            "dict_structure",
            "list_structure",
            "nested_structure",
            "empty_dict",
        ],
    )
    def test_valid_configs(self, config):
        """Test validating valid remote_vars configurations."""
        validate_remote_vars(config)  # Should not raise

    @pytest.mark.parametrize(
        "config,expected_error",
        [
            (
                {"bad": "invalid.reference"},
                "Invalid remote_vars configuration for key 'bad'",
            ),
            (
                {"vpcs": {"platform": "invalid.reference"}},
                "Invalid remote_vars configuration for key 'vpcs'",
            ),
            ({"items": [123, 456]}, "Unsupported remote_vars value type"),
            (
                {"valid": "net1.outputs.vpc", "invalid": "bad.reference"},
                "Invalid remote_vars configuration for key 'invalid'",
            ),
        ],
        ids=[
            "invalid_string",
            "invalid_nested_string",
            "invalid_type_in_list",
            "multiple_keys_one_invalid",
        ],
    )
    def test_invalid_configs(self, config, expected_error):
        """Test that invalid configurations raise ValueError."""
        with pytest.raises(ValueError) as exc_info:
            validate_remote_vars(config)
        assert expected_error in str(exc_info.value)


class TestIntegrationScenarios:
    """Integration tests for common usage scenarios."""

    def test_standard_string_references(self):
        """Test that standard string references work correctly."""
        config = {
            "global_ipv4_blacklist": "env_info.outputs.global_ipv4_blacklist",
            "vpcs": "env_info.outputs.vpcs",
            "waf_ip_lists": "env_info.outputs.waf_ip_lists",
            "environment": "env_info.outputs.environment",
        }

        # Validate
        validate_remote_vars(config)

        # Extract states
        all_states = set()
        for value in config.values():
            all_states.update(extract_remote_states(value))
        assert all_states == {"env_info"}

        # Generate Terraform references
        for key, value in config.items():
            tf_ref = generate_tf_reference(value)
            assert tf_ref.startswith("data.terraform_remote_state.env_info.outputs.")

    def test_new_dict_structure_entire_outputs(self):
        """Test new dict structure with entire outputs."""
        config = {
            "vpcs": {
                "platform-tools": "network1.outputs",
                "payments-network": "network2.outputs",
            }
        }

        # Validate
        validate_remote_vars(config)

        # Extract states
        states = extract_remote_states(config["vpcs"])
        assert states == {"network1", "network2"}

        # Generate Terraform references
        for key, value in config["vpcs"].items():
            tf_ref = generate_tf_reference(value)
            state, output_key = parse_tf_reference(tf_ref)
            assert output_key is None  # Entire outputs requested

    def test_new_dict_structure_specific_keys(self):
        """Test new dict structure with specific output keys."""
        config = {
            "vpc_configs": {
                "platform": "network1.outputs.vpc_config",
                "payments": "network2.outputs.vpc_config",
            }
        }

        # Validate
        validate_remote_vars(config)

        # Extract states
        states = extract_remote_states(config["vpc_configs"])
        assert states == {"network1", "network2"}

        # Generate Terraform references
        for key, value in config["vpc_configs"].items():
            tf_ref = generate_tf_reference(value)
            state, output_key = parse_tf_reference(tf_ref)
            assert output_key == "vpc_config"

    def test_new_list_structure(self):
        """Test new list structure."""
        config = {
            "all_vpc_ids": [
                "network1.outputs.vpc_id",
                "network2.outputs.vpc_id",
                "network3.outputs.vpc_id",
            ]
        }

        # Validate
        validate_remote_vars(config)

        # Extract states
        states = extract_remote_states(config["all_vpc_ids"])
        assert states == {"network1", "network2", "network3"}

        # Generate Terraform references
        for value in config["all_vpc_ids"]:
            tf_ref = generate_tf_reference(value)
            state, output_key = parse_tf_reference(tf_ref)
            assert output_key == "vpc_id"

    def test_mixed_simple_and_complex(self):
        """Test config with mix of simple and complex structures."""
        config = {
            # Simple (existing)
            "environment": "env_info.outputs.environment",
            # Dict with entire outputs
            "vpcs": {"platform": "network1.outputs", "payments": "network2.outputs"},
            # List
            "vpc_ids": ["network1.outputs.vpc_id", "network2.outputs.vpc_id"],
            # Dict with specific keys
            "endpoints": {
                "db_primary": "db1.outputs.endpoint",
                "db_replica": "db2.outputs.endpoint",
            },
        }

        # Validate entire config
        validate_remote_vars(config)

        # Extract all states
        all_states = set()
        for value in config.values():
            all_states.update(extract_remote_states(value))
        assert all_states == {"env_info", "network1", "network2", "db1", "db2"}
