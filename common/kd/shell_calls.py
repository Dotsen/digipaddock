"""Literal calls of named commands in a Bash command line.

The PreToolUse handler records the exact argv each channel call will get, so the words must be
the ones bash passes: quotes removed as bash removes them, redirections and their targets left
out, here-document bodies skipped, comments dropped. A call whose words bash would still expand
(any $ outside single quotes, backquotes, globs, braces, a leading ~, a line continuation) is not
literal: its argv is unknown before bash runs it, so it is refused rather than guessed.

This is a lexer for simple commands, not a full shell parser: anything it does not model (a
command behind `timeout`, `xargs`, `bash -c`) is simply not a call, and the channel then refuses
it as unexpected.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Collection, List

RESERVED = frozenset(
    {
        "if",
        "then",
        "else",
        "elif",
        "do",
        "while",
        "until",
        "!",
        "time",
        "{",
        "}",
        "fi",
        "done",
        "esac",
    }
)
_SEPARATORS = ";&|()\n"
_EXPANDING = "`{}$*?["


class NotLiteral(ValueError):
    """A call of `command` has words bash would expand."""

    def __init__(self, command: str):
        super().__init__(f"arguments of {command} are not literal")
        self.command = command


@dataclass
class Segment:
    words: List[str] = field(default_factory=list)
    bad: bool = False  # a kept word bash would expand, or the segment does not parse
    redirect: bool = False  # any redirection or here-document
    dashdash: bool = False  # a "--" after the first word

    def command(self) -> List[str]:
        """The words from the command word on (leading reserved words skipped)."""
        k = 0
        while k < len(self.words) and self.words[k] in RESERVED:
            k += 1
        return self.words[k:]


def segments(command: str) -> List[Segment]:
    out: List[Segment] = []
    seg = Segment()
    token, in_token, token_bad, token_quoted = "", False, False, False
    quote = ""
    drop_next, heredoc_next = (
        False,
        None,
    )  # the next word is a redirection target / a here-doc delimiter
    heredocs: List[tuple] = []

    def end_token():
        nonlocal token, in_token, token_bad, token_quoted, drop_next, heredoc_next
        if in_token:
            if drop_next:
                if heredoc_next is not None:
                    heredocs.append((token, heredoc_next))
                drop_next, heredoc_next = False, None
            else:
                if token in ("{", "}") and not token_quoted:
                    token_bad = False  # the reserved word of a brace group
                seg.bad = seg.bad or token_bad
                if token == "--" and seg.words:
                    seg.dashdash = True
                seg.words.append(token)
        token, in_token, token_bad, token_quoted = "", False, False, False

    def end_segment():
        nonlocal seg, drop_next, heredoc_next
        end_token()
        if drop_next:  # a redirection without a target
            seg.bad = True
            drop_next, heredoc_next = False, None
        if seg.words or seg.bad or seg.redirect:
            out.append(seg)
        seg = Segment()

    i, n = 0, len(command)
    while i < n:
        ch, nxt = command[i], command[i + 1 : i + 2]
        if quote == "'":
            if ch == "'":
                quote = ""
            else:
                token += ch
        elif quote == '"':
            if ch == '"':
                quote = ""
            elif ch in "`$":
                token_bad = True
                token += ch
            elif ch == "\\":
                if nxt in ("$", "`", '"', "\\"):
                    token += nxt
                    i += 1
                elif nxt == "\n":
                    token_bad = True
                    i += 1
                else:
                    token += "\\"
            else:
                token += ch
        elif ch == "\\":
            in_token = True
            if nxt == "\n" or nxt == "":
                token_bad = True  # line continuation (or a dangling backslash)
            else:
                token += nxt
            i += 1
        elif ch in "'\"":
            quote, in_token, token_quoted = ch, True, True
        elif ch == "#" and not in_token:
            while i < n and command[i] != "\n":
                i += 1
            continue
        elif ch in "<>" or (ch == "&" and nxt == ">"):
            if ch in "<>" and nxt == "(":
                seg.bad = True  # process substitution
                end_token()
                i += 1
                continue
            if in_token and not token_quoted and token.isdigit():
                token, in_token = "", False  # the fd number of 2>…
            else:
                end_token()
            seg.redirect = True
            j = i + 1 if ch == "&" else i
            op = command[j]
            j += 1
            heredoc = None
            if op == "<" and command[j : j + 1] == "<":
                j += 1
                if command[j : j + 1] == "<":
                    j += 1  # here-string: its word is a target
                else:
                    heredoc = command[j : j + 1] == "-"
                    j += 1 if heredoc else 0
            elif command[j : j + 1] in (">", "&", "|") or (
                op == "<" and command[j : j + 1] == ">"
            ):
                j += 1
            drop_next, heredoc_next = True, heredoc
            i = j
            continue
        elif ch in _SEPARATORS:
            end_segment()
            if ch == "\n":
                i += 1
                for delimiter, strip_tabs in heredocs:
                    while i < n:
                        eol = command.find("\n", i)
                        eol = n if eol == -1 else eol
                        body = command[i:eol]
                        i = eol + 1
                        if (body.lstrip("\t") if strip_tabs else body) == delimiter:
                            break
                heredocs.clear()
                continue
        elif ch.isspace():
            end_token()
        elif ch in _EXPANDING or (
            ch == "~" and (not in_token or token[-1:] in ("=", ":"))
        ):
            token_bad = True
            token += ch
            in_token = True
        else:
            token += ch
            in_token = True
        i += 1
    if quote:
        seg.bad = True  # an unclosed quote: bash refuses the whole line
    end_segment()
    return out


def calls(command: str, names: Collection[str]) -> List[List[str]]:
    """The argv of every call whose command word is exactly one of `names`; NotLiteral if one is not literal."""
    found = []
    for seg in segments(command):
        words = seg.command()
        if words and words[0] in names:
            if seg.bad:
                raise NotLiteral(words[0])
            found.append(list(words))
    return found


def cd_compound(command: str) -> bool:
    """True for `cd X` together with other commands in one Bash call (Claude Code asks the owner for it)."""
    parts = [seg.command() for seg in segments(command)]
    parts = [words for words in parts if words]
    return len(parts) > 1 and any(words[0] == "cd" for words in parts)


def unsafe_call_of(command: str, name: str) -> bool:
    """True if a call of `name` (also by path) has expansions, redirections or "--"."""
    for seg in segments(command):
        words = seg.command()
        if (
            words
            and posixpath.basename(words[0]) == name
            and (seg.bad or seg.redirect or seg.dashdash)
        ):
            return True
    return False
