from __future__ import annotations

from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

from prediction_bot.dashboard import render_dashboard
from prediction_bot.paper.ledger import Ledger


class ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self.current: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.current = []

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.current is not None:
            self.scripts.append("".join(self.current))
            self.current = None


def _assert_script_strings_are_line_safe(script: str) -> None:
    index = 0
    regex_allowed = True
    quote: str | None = None
    while index < len(script):
        char = script[index]
        if quote is not None:
            assert char not in "\r\n", "JavaScript string literal crosses a line break"
            if char == "\\":
                assert index + 1 < len(script), "trailing escape in JavaScript string"
                assert script[index + 1] not in "\r\n", "escaped line break in JavaScript string"
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue

        if script.startswith("//", index):
            newline = script.find("\n", index)
            index = len(script) if newline == -1 else newline
            continue
        if script.startswith("/*", index):
            end = script.find("*/", index + 2)
            assert end != -1, "unterminated JavaScript block comment"
            index = end + 2
            continue

        if char in "'\"`":
            quote = char
            index += 1
            regex_allowed = False
            continue
        if char == "/":
            if regex_allowed:
                index += 1
                in_character_class = False
                while index < len(script):
                    char = script[index]
                    assert char not in "\r\n", "JavaScript regular expression crosses a line break"
                    if char == "\\":
                        index += 2
                        continue
                    if char == "[":
                        in_character_class = True
                    elif char == "]":
                        in_character_class = False
                    elif char == "/" and not in_character_class:
                        index += 1
                        while index < len(script) and script[index].isalpha():
                            index += 1
                        break
                    index += 1
                else:
                    raise AssertionError("unterminated JavaScript regular expression")
                regex_allowed = False
                continue
            regex_allowed = True
            index += 1
            continue
        if char.isspace():
            index += 1
            continue
        if char.isalpha() or char in "_$":
            end = index + 1
            while end < len(script) and (script[end].isalnum() or script[end] in "_$"):
                end += 1
            regex_allowed = script[index:end] in {
                "case",
                "delete",
                "in",
                "instanceof",
                "of",
                "return",
                "throw",
                "typeof",
                "void",
                "yield",
            }
            index = end
            continue
        if char.isdigit():
            index += 1
            while index < len(script) and (script[index].isalnum() or script[index] in "._"):
                index += 1
            regex_allowed = False
            continue
        regex_allowed = char in "([{,:;=!?&|+-*%^~<>"
        index += 1

    assert quote is None, "unterminated JavaScript string literal"


def test_rendered_scripts_have_balanced_line_safe_strings(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "dashboard-script.sqlite", Decimal("1000"))
    parser = ScriptCollector()
    parser.feed(render_dashboard(ledger))

    assert parser.scripts
    for script in parser.scripts:
        _assert_script_strings_are_line_safe(script)
