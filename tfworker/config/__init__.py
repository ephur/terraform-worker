# TODO YAML schema
class Config:
    def __init__(
        self, stream, args, Loader=yaml.SafeLoader, object_pairs_hook=OrderedDict
    ):
        self._args = args
        loader = self._make_loader(Loader, object_pairs_hook)
        self._config = yaml.load(stream, loader)

    def _make_loader(self, Loader, object_pairs_hook):
        """
        Load a yaml config, and replace templated items.

        Derived from:
        https://stackoverflow.com/questions/5121931/in-python-how-can-you-load-yaml-mappings-as-ordereddicts
        """
        class OrderedLoader(Loader):
            pass

        def construct_mapping(loader, node):
            for item in node.value:
                if isinstance(item, tuple):
                    for oneitem in item:
                        if isinstance(oneitem, yaml.ScalarNode):
                            oneitem.value = replace_vars(oneitem.value, args)

            loader.flatten_mapping(node)
            return object_pairs_hook(loader.construct_pairs(node))

        OrderedLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
        )
        return OrderedLoader

    def replace_vars(self, var):
        """Replace variables with template values."""
        var_pattern = r"//\s*(\S*)\s*//"
        match = re.match(var_pattern, var, flags=0)
        if not match:
            return var
        try:
            var = getattr(args, match.group(1).replace("-", "_"))
        except AttributeError:
            raise (ValueError("substitution not found for {}".format(var)))
        return var
