"""The values of the arguments that take one (tree.Arg.source): the pick lists the menu offers and
the completion completes, the defaults of typed values, and the checks typed text must pass."""

import collections
import re

from . import context

Choice = collections.namedtuple("Choice", "value detail note", defaults=("", ""))

# Typed, not picked: the menu offers a default, the completion leaves them to the shell.
TYPED = {"directory", "file", "version", "number"}
# Picked, but text that matches nothing is taken as typed (a new variable, a scope, a change id).
OPEN = {"scope", "key", "assignment", "change"}
KNOWN = TYPED | OPEN | {"application", "host-application", "app-of-environment", "environment",
                        "host-environment", "app-environment", "app-host-environment", "service",
                        "app-option", "environment-option", "command"}

KEY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def check_scope(text):
    vars_module = context.vars_module()
    try:
        vars_module.parse_scope(text, None)
    except vars_module.VarsError as error:
        return str(error)
    return None


def check_key(text):
    return None if KEY_NAME.match(text) else "a variable name: letters, digits and underscores, not starting with a digit"


def check_number(text):
    return None if text == "" or text.isdigit() else "a number"


def check_value(text):
    vars_module = context.vars_module()
    try:
        vars_module.validate_value(text)
    except vars_module.VarsError as error:
        return str(error)
    return None


def checker(source):
    """The check typed text for this source must pass, or None."""
    return {"scope": check_scope, "key": check_key, "assignment": check_key, "change": check_number,
            "number": check_number}.get(source)
