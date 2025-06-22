from unittest import mock

import pytest
from pydantic import BaseModel, ValidationError

import tfworker.commands.base as b


class TestHandlerHelpers:
    def test_parse_handlers(self, mocker):
        mocker.patch.object(b, "_validate_handler_config", return_value="cfg")
        mocker.patch.object(b, "_initialize_handler", return_value="inst")
        result = b._parse_handlers({"h": {"a": 1}})
        assert result == {"h": "inst"}

    def test_add_universal_handlers(self, mocker):
        mocker.patch(
            "tfworker.handlers.registry.HandlerRegistry.list_universal_handlers",
            return_value=["u"],
        )
        mocker.patch(
            "tfworker.handlers.registry.HandlerRegistry.get_handler",
            return_value=lambda: "uinst",
        )
        parsed = {}
        b._add_universal_handlers(parsed)
        assert callable(parsed["u"]) or parsed["u"] == "uinst"

    def test_check_handlers_ready(self):
        handlers = {"h": mock.Mock(is_ready=True)}
        b._check_handlers_ready(handlers)
        assert handlers == {"h": handlers["h"]}

    def test_check_handlers_ready_removes(self):
        class HandlerDict(dict):
            def items(self):
                return list(super().items())

        handlers = HandlerDict({"h": mock.Mock(is_ready=False), "g": mock.Mock(is_ready=True)})
        b._check_handlers_ready(handlers)
        assert "h" not in handlers and "g" in handlers


class TestInitFunctions:
    def test_init_authenticators_success(self, mocker):
        root_opts = mock.Mock()
        fake = mock.Mock()
        fake.keys.return_value = [mock.Mock(tag="a")]
        ac = mocker.patch(
            "tfworker.authenticators.collection.AuthenticatorsCollection",
            return_value=fake,
        )
        result = b._init_authenticators(root_opts)
        assert result is fake
        ac.assert_called_with(root_opts)

    def test_init_authenticators_error(self, mocker):
        mocker.patch(
            "tfworker.authenticators.collection.AuthenticatorsCollection",
            side_effect=b.TFWorkerException("boom"),
        )
        with pytest.raises(SystemExit):
            b._init_authenticators(mock.Mock())

    def test_init_providers_success(self, mocker):
        prov = mock.Mock()
        prov.keys.return_value = ["p"]
        pc = mocker.patch(
            "tfworker.providers.collection.ProvidersCollection", return_value=prov
        )
        res = b._init_providers({}, mock.Mock())
        assert res is prov
        pc.assert_called_with({}, mock.ANY)

    def test_init_providers_validation_error(self, mocker):
        class M(BaseModel):
            a: int

        try:
            M(a="x")
        except ValidationError as err:
            val_err = err
        mocker.patch(
            "tfworker.providers.collection.ProvidersCollection", side_effect=val_err
        )
        mocker.patch(
            "tfworker.commands.base.handle_config_error", side_effect=SystemExit
        )
        with pytest.raises(SystemExit):
            b._init_providers({}, mock.Mock())

    def test_init_definitions_success(self, mocker):
        defs = mock.Mock()
        defs.keys.return_value = ["d"]
        dc = mocker.patch(
            "tfworker.definitions.collection.DefinitionsCollection", return_value=defs
        )
        mocker.patch("tfworker.commands.config.find_limiter", return_value=["d"])
        res = b._init_definitions({"d": {}})
        assert res is defs
        dc.assert_called_with({"d": {}}, limiter=["d"])

    def test_init_definitions_error(self, mocker):
        mocker.patch(
            "tfworker.definitions.collection.DefinitionsCollection",
            side_effect=ValueError("bad"),
        )
        with pytest.raises(SystemExit):
            b._init_definitions({})

    def test_init_backend_calls_helpers(self, mocker):
        state = mock.Mock()
        state.root_options.backend = "b"
        state.deployment = "dep"
        state.authenticators = "auth"
        state.root_options.backend_plans = False
        be = mock.Mock(tag="t")
        sel = mocker.patch("tfworker.commands.base._select_backend", return_value=be)
        chk = mocker.patch("tfworker.commands.base._check_backend_plans")
        res = b._init_backend_(state)
        assert res is be
        sel.assert_called_with("b", "dep", "auth")
        chk.assert_called_with(False, be)

    def test_select_backend_success(self, mocker):
        backend = mock.Mock()
        backend.value.return_value = "ok"
        assert b._select_backend(backend, "dep", "auth") == "ok"
        backend.value.assert_called_with("auth", deployment="dep")

    def test_select_backend_error(self, mocker):
        backend = mock.Mock()
        backend.value.side_effect = b.BackendError("fail", help="h")
        with pytest.raises(SystemExit):
            b._select_backend(backend, "dep", "auth")

    def test_check_backend_plans(self):
        backend = mock.Mock(plan_storage=True, tag="t")
        b._check_backend_plans(False, backend)
        backend.plan_storage = False
        with pytest.raises(SystemExit):
            b._check_backend_plans(True, backend)

    def test_init_handlers_flow(self, mocker):
        parsed = {"h": "inst"}
        ph = mocker.patch.object(b, "_parse_handlers", return_value=parsed)
        ah = mocker.patch.object(b, "_add_universal_handlers")
        chk = mocker.patch.object(b, "_check_handlers_ready")
        hc_obj = mock.Mock()
        hc_obj.keys.return_value = ["h"]
        hc = mocker.patch(
            "tfworker.handlers.collection.HandlersCollection", return_value=hc_obj
        )
        res = b._init_handlers({"h": {}})
        assert res is hc_obj
        ph.assert_called_with({"h": {}})
        ah.assert_called_with(parsed)
        hc.assert_called_with(parsed)
        chk.assert_called_with(hc_obj)

    def test_validate_handler_config_success(self, mocker):
        m = mock.Mock()
        m.model_validate.return_value = "cfg"
        g = mocker.patch(
            "tfworker.handlers.registry.HandlerRegistry.get_handler_config_model",
            return_value=m,
        )
        assert b._validate_handler_config("h", {}) == "cfg"
        g.assert_called_with("h")

    def test_validate_handler_config_error(self, mocker):
        class M(BaseModel):
            a: int

        try:
            M(a="x")
        except ValidationError as err:
            val_err = err

        class Dummy:
            def model_validate(self, _):
                raise val_err

        mocker.patch(
            "tfworker.handlers.registry.HandlerRegistry.get_handler_config_model",
            return_value=Dummy(),
        )
        mocker.patch("tfworker.commands.base.handle_config_error", side_effect=SystemExit)
        with pytest.raises(SystemExit):
            b._validate_handler_config("h", {})

    def test_initialize_handler_success(self, mocker):
        mocker.patch(
            "tfworker.handlers.registry.HandlerRegistry.get_handler",
            return_value=lambda c: f"got-{c}",
        )
        assert b._initialize_handler("h", "c") == "got-c"

    def test_initialize_handler_error(self, mocker):
        mocker.patch(
            "tfworker.handlers.registry.HandlerRegistry.get_handler",
            side_effect=b.HandlerError("bad"),
        )
        with pytest.raises(SystemExit):
            b._initialize_handler("h", mock.Mock())


class TestBaseCommandInit:
    def test_init_flow(self, mocker, mock_click_context):
        state = mock_click_context.obj
        state.freeze = mocker.Mock()
        state.root_options.log_level = "INFO"
        state.root_options.backend_prefix = "pre-{deployment}"
        ipa = mocker.patch.object(b, "_init_authenticators", return_value="a")
        ipp = mocker.patch.object(b, "_init_providers", return_value="p")
        ib = mocker.patch.object(b, "_init_backend_", return_value="b")
        idf = mocker.patch.object(b, "_init_definitions", return_value="d")
        ih = mocker.patch.object(b, "_init_handlers", return_value="h")
        rm = mocker.patch("tfworker.commands.config.resolve_model_with_cli_options")
        cmd = b.BaseCommand("dep", ctx=mock_click_context)
        assert cmd.app_state is state
        rm.assert_called_with(state)
        ipa.assert_called_with(state.root_options)
        ipp.assert_called_with(state.loaded_config.providers, "a")
        ib.assert_called_with(state)
        idf.assert_called_with(state.loaded_config.definitions)
        ih.assert_called_with(state.loaded_config.handlers)
        assert state.root_options.backend_prefix == "pre-dep"
        state.freeze.assert_called_once()

    def test_init_default_ctx_and_app_state(
        self, mocker, mock_click_context, mock_app_state
    ):
        mocker.patch("click.get_current_context", return_value=mock_click_context)
        mock_app_state.root_options.log_level = "INFO"
        mocker.patch.object(b, "_init_authenticators", return_value="a")
        mocker.patch.object(b, "_init_providers", return_value="p")
        mocker.patch.object(b, "_init_backend_", return_value="b")
        mocker.patch.object(b, "_init_definitions", return_value="d")
        mocker.patch.object(b, "_init_handlers", return_value="h")
        mocker.patch("tfworker.commands.config.resolve_model_with_cli_options")
        cmd = b.BaseCommand("dep", app_state=mock_app_state)
        assert cmd.ctx is mock_click_context
        assert cmd.app_state is mock_app_state
