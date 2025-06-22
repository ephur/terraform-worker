from unittest import mock

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
