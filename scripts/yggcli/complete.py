"""Tab completion: `ygg completion bash` prints the script; the script asks `ygg __complete '<line>'`
for the words, which come from the command tree and its pick lists (the menu's own)."""

import shlex

from . import context, sources, tree

SCRIPT = r"""# bash completion for ygg: `ygg completion bash` prints it, `ygg self-install` installs it.
_ygg() {
  local IFS=$'\n'
  # shellcheck disable=SC2207
  COMPREPLY=($(ygg __complete "${COMP_LINE:0:COMP_POINT}" 2>/dev/null))
  # KEY= waits for its value: no space after it.
  if [[ ${#COMPREPLY[@]} -eq 1 && ${COMPREPLY[0]} == *= ]]; then compopt -o nospace; fi
}
complete -o default -F _ygg ygg
"""

# Bash splits completion words at these (COMP_WORDBREAKS) and replaces only what follows the last.
BREAKS = "@:="


def script():
    return SCRIPT


def _words(line):
    try:
        words = shlex.split(line)
    except ValueError:  # an unclosed quote: complete as the shell sees it so far
        words = line.split()
    if not line or line[-1].isspace():
        words.append("")
    return words


def complete(line, ctx=None):
    ctx = ctx or context.Context()
    words = _words(line)[1:]
    if not words:
        words = [""]
    current, before = words[-1], words[:-1]
    matching = [c for c in _candidates(before, current, ctx) if c.startswith(current)]
    cut = max(current.rfind(ch) for ch in BREAKS) + 1
    return [c[cut:] for c in matching]


def _candidates(before, current, ctx):
    command, used = tree.deepest(before)
    if command is None:
        depth = len(before)
        return sorted({c.path[depth] for c in tree.commands_list()
                       if len(c.path) > depth and c.path[:depth] == tuple(before)})
    options = {flag: arg for arg in command.args for flag in arg.flags}
    values, positionals, waiting = {}, [], None
    for word in before[used:]:
        if waiting is not None:
            values[waiting.field] = word
            waiting = None
        elif word in options:
            if options[word].is_flag:
                values[options[word].field] = True
            else:
                waiting = options[word]
        elif not word.startswith("-"):
            positionals.append(word)
    if waiting is not None:
        return _values(waiting, ctx, values)
    if current.startswith("-"):
        return sorted(f for f, a in options.items() if not values.get(a.field))
    target, index = None, 0
    for arg in (a for a in command.args if a.positional):
        if arg.many:
            values[arg.dest] = positionals[index:]
            target = arg
            break
        if index < len(positionals):
            values[arg.dest] = positionals[index]
            index += 1
        else:
            target = arg
            break
    if target is None:
        return sorted(options)
    return _values(target, ctx, values)


def _values(arg, ctx, values):
    if arg.choices:
        return [str(c) for c in arg.choices]
    if arg.source is None or arg.source in sources.TYPED:
        return []
    if arg.source == "assignment":
        return [c.value + "=" for c in sources.choices("key", ctx, values)]
    if arg.source == "command":  # `ygg help vars get`: one word of a command's path at a time
        given = tuple(values.get(arg.dest) or [])
        return sorted({c.path[len(given)] for c in tree.commands_list()
                       if len(c.path) > len(given) and c.path[:len(given)] == given})
    return [c.value for c in sources.choices(arg.source, ctx, values)]


def main(argv):
    for word in complete(argv[0] if argv else ""):
        print(word)
    return 0
