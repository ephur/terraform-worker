import json


class ProviderError(Exception):
    pass


class StateError(Exception):
    pass


def validate_state_empty(state):
    """
    validate_empty_state ensures that the provided state file
    is empty
    """

    try:
        if len(state["resources"]) > 0:
            return False
        else:
            return True
    except KeyError:
        raise StateError("resources key does not exist in state!")


def validate_state_region(state):
    """
    validate_state_region validates that a statefile
    was previously used in the region the current
    deployment is being created for
    """
