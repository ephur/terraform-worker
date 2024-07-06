import re
from enum import Enum
from functools import partial
from typing import Any, Dict, List, Union

from click import secho

from tfworker.constants import REDACTED_ITEMS


class LogLevel(Enum):
    TRACE = 0
    DEBUG = 1
    INFO = 2
    WARN = 3
    ERROR = 4


log_level = LogLevel.ERROR


def log(
    msg: Union[str | Dict[str, Any]], level: LogLevel = LogLevel.INFO, redact=False
) -> None:
    """
    Out currently emits to stdout via click, but exists to easily
    change to a python logger for alternate output options in the
    future

    Args:
        msg ()
    """
    global log_level
    level_colors = {
        log_level.TRACE: "cyan",
        log_level.DEBUG: "blue",
        log_level.INFO: "green",
        log_level.WARN: "yellow",
        log_level.ERROR: "red",
    }

    if redact:
        msg = redact_items_token(msg)

    if level.value >= log_level.value:
        secho(msg, fg=level_colors[level])
    return


def redact_items_token(
    items: Union[Dict[str, Any], str], redact: List[str] = REDACTED_ITEMS
) -> Union[Dict[str, Any], str]:
    """
    Redact items from a dictionary or string using tokenization

    Args:
        items (Union[Dict[str, Any], str]): Items to redact
        redact (List[str]): List of items to redact

    Returns:
        Union[Dict[str, Any], str]: Redacted items

    Raises:
        ValueError: If passed an item that is not a dictionary or string
    """

    if isinstance(items, str):
        """
        Redacting items from a string is a bit more complex, since the items
        in the string could have many variations. For example, a key could be
        followed by a space, a colon, an equals sign, or a tab. The value could
        be enclosed in quotes or not. The value could be a string, a number, or
        a boolean. This function will attempt to redact the items in the string
        while preserving the structure of the string.
        """
        result = []
        i = 0
        while i < len(items):
            # check if a key has been found
            found = False
            for key in redact:
                # check if the key is in the items
                if items[i : i + len(key)] == key:  # noqa: E203
                    found = True
                    result.append(key)
                    i += len(key)
                    # Include delimiters after the key (spaces, tabs, colons, equals signs)
                    while i < len(items) and items[i] in " \t=:":
                        result.append(items[i])
                        i += 1
                    # Check if the value is enclosed in quotes
                    if i < len(items) and items[i] in "\"'":
                        # handle quoted values
                        quote = items[i]
                        result.append(quote)
                        i += 1
                        # skip chars until the closing quote
                        while i < len(items) and items[i] != quote:
                            i += 1
                        result.append("REDACTED")
                        # include the closing quote if present
                        if i < len(items) and items[i] == quote:
                            result.append(quote)
                            i += 1
                    else:
                        # handle unquoted values
                        while i < len(items) and items[i] not in " \t,:;\n":
                            i += 1
                        result.append("REDACTED")
            if not found:
                # the token was not in the redact list
                result.append(items[i])
                i += 1
        return "".join(result)

    elif isinstance(items, dict):
        for k, v in items.items():
            if isinstance(v, dict):
                items[k] = redact_items_token(v, redact)
            elif k in redact:
                items[k] = "REDACTED"
            elif isinstance(v, str):
                items[k] = redact_items_token(v, redact)
        return items

    else:
        raise ValueError("Items must be a dictionary or a string")


def redact_items_re(
    items: Union[Dict[str, Any], str], redact: List[str] = REDACTED_ITEMS
) -> Union[Dict[str, Any], str]:
    """
    Redact items from a dictionary or string using regex

    Args:
        items (Union[Dict[str, Any], str]): Items to redact
        redact (List[str]): List of items to redact

    Returns:
        Union[Dict[str, Any], str]: Redacted items

    Raises:
        ValueError: If passed an item that is not a dictionary or string
    """
    if isinstance(items, str):
        # The regex pattern is designed to match and redact sensitive information from a string, preserving the original key, delimiter, and quote style.
        #
        # Pattern Components:
        # r'(' + '|'.join(re.escape(key) for key in redact) + r')': This part dynamically constructs a regex group that matches any of the keys specified in the 'redact' list. 're.escape' ensures that any special characters in the keys are treated as literals.
        #
        # (\s*[:=]\s*|\s+): This group matches the delimiter that follows the key. It accounts for zero or more spaces (\s*) followed by either a colon (:) or an equals sign (=), again followed by zero or more spaces. Alternatively, it matches one or more spaces (\s+), allowing for different styles of key-value separation.
        #
        # (["\']?): This optional group matches either a single quote ('), a double quote ("), or no quote at all, capturing the opening quote style if present.
        #
        # (.*?): This non-greedy group matches the value associated with the key. The non-greedy qualifier (?) ensures that it stops matching at the first instance of the following group, which is the closing quote or the end of the value.
        #
        # (\3): This group is a backreference to the third group, matching the same quote style as the opening quote to ensure the closing quote is identical. If the opening quote was absent, this group matches nothing.
        #
        # (?=\s|$): This positive lookahead asserts that the character following the value (or closing quote if present) is either a whitespace character (\s) or the end of the string ($). This ensures that the match ends at the correct point without consuming any characters, allowing for subsequent matches to proceed correctly.
        #
        # The 'pattern.sub(r'\1\2\3REDACTED\5', items)' call replaces the matched value with 'REDACTED', preserving the key, delimiter, and quote style. The replacement string uses backreferences (\1, \2, \3, \5) to reconstruct the original text around 'REDACTED'.
        pattern = re.compile(
            r"("
            + "|".join(re.escape(key) for key in redact)
            + r')(\s*[:=]\s*|\s+)(["\']?)(.*?)(\3)(?=\s|$)',
            re.IGNORECASE,
        )
        return pattern.sub(r"\1\2\3REDACTED\5", items)

    elif isinstance(items, dict):
        for k, v in items.items():
            if isinstance(v, dict):
                items[k] = redact_items_re(v, redact)
            elif k in redact:
                items[k] = "REDACTED"
            elif isinstance(v, str):
                items[k] = redact_items_re(v, redact)
        return items

    else:
        raise ValueError("Items must be a dictionary or a string")


# Allow a non stuttering method when importing the library to print
msg = log

# these partials allow easy logging at a level via: log.<level>("message")
trace = partial(log, level=LogLevel["TRACE"])
safe_trace = partial(log, level=LogLevel["TRACE"], redact=True)
debug = partial(log, level=LogLevel["DEBUG"])
safe_debug = partial(log, level=LogLevel["DEBUG"], redact=True)
info = partial(log, level=LogLevel["INFO"])
safe_info = partial(log, level=LogLevel["INFO"], redact=True)
warn = partial(log, level=LogLevel["WARN"])
safe_warn = partial(log, level=LogLevel["WARN"], redact=True)
error = partial(log, level=LogLevel["ERROR"])
safe_error = partial(log, level=LogLevel["ERROR"], redact=True)
