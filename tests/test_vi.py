import json
import shutil
import subprocess
import unittest
from pathlib import Path

from agent_journal.vi import Editor


ESC, ENTER, BLOCK, REDO = "Escape", "Enter", "\x16", "\x12"
CASES = {
    "operator count": ("one two three four", list("d2w"), "three four"),
    "combined counts": ("one two three four five", list("2d2w"), "five"),
    "inner word": ("prefix old-word", list("wciwnew") + [ESC], "prefix new-word"),
    "line end lands on the last character": ("one\nend👩‍💻", list("2$x"), "one\nend"),
    "delete to line end preserves the newline": ("abc\nnext", list("ld$"), "a\nnext"),
    "visual line end excludes the newline": ("abc\nnext", list("v$d"), "\nnext"),
    "inner quote": ('prefix "old words" suffix', list('f"ci"new') + [ESC], 'prefix "new" suffix'),
    "nested brackets": ("outer(inner(old))", list("f(f(lci(new") + [ESC], "outer(inner(new))"),
    "visual word object": ("first second", list("wviwU"), "first SECOND"),
    "line register": ("first\nsecond\nthird", list('"a2yyG"ap'), "first\nsecond\nthird\nfirst\nsecond"),
    "black hole register": ("one two", list('yw"_dwp'), "tone wo"),
    "visual character": ("abc def", list("v2ld"), " def"),
    "visual line indent": ("first\nsecond\nthird", list("Vj>"), "    first\n    second\nthird"),
    "visual block change": ("abc\ndef\nghi", [BLOCK, *"jlc", "X", ESC], "Xc\nXf\nghi"),
    "visual block insert": ("abc\ndef\nghi", ["l", BLOCK, *"jjI", "!", ESC], "a!bc\nd!ef\ng!hi"),
    "visual block yank and paste": ("abc\ndef\nghi", [BLOCK, *"jlyG0p"], "abc\ndef\ngabhi\n de"),
    "undo whole insert": ("base", list("Axyz") + [ESC, "u"], "base"),
    "redo whole insert": ("base", list("Axyz") + [ESC, "u", REDO], "basexyz"),
    "repeat whole insert": ("first\nsecond", list("A!") + [ESC, "j", "."], "first!\nsecond!"),
    "counted insert": ("rest", list("3iab") + [ESC], "abababrest"),
    "find repeat and reverse": ("a1b1c1d", list("f1;,x"), "ab1c1d"),
    "replace mode": ("abc def", list("RXY") + [ESC], "XYc def"),
    "replace character and join": ("abc\nnext", list("rXJ"), "Xbc next"),
    "search and reverse": ("one two one", list("/one") + [ENTER, "N", "x"], "ne two one"),
    "substitution with captures": (
        "item 12 item 34\nitem 56", list(r":%s/item (\d+)/#\1/g") + [ENTER], "#12 #34\n#56",
    ),
    "current line substitution": ("foo foo\nfoo", list(":s/foo/bar/") + [ENTER], "bar foo\nfoo"),
    "mark exact": ("first\nsecond", list("lmaG" + chr(96) + "ax"), "frst\nsecond"),
    "macro and repeating the last macro": (
        "one\ntwo\nthree", list("qaA!") + [ESC, "q", "j", "@", "a", "j", "@", "@"],
        "one!\ntwo!\nthree!",
    ),
    "grapheme deletion": ("가나다 👩‍💻 끝", ["w", "x"], "가나다  끝"),
}


class ViTests(unittest.TestCase):
    def test_terminal_sequences(self):
        for name, (text, keys, expected) in CASES.items():
            with self.subTest(name=name):
                editor = Editor({"body": text})
                for key in keys:
                    editor.input({ESC: "\x1b", ENTER: "\n"}.get(key, key))
                self.assertEqual(editor.text, expected)
                self.assertEqual(editor.mode, "Normal")

    @unittest.skipUnless(shutil.which("node"), "requires Node.js")
    def test_web_sequences_match_terminal(self):
        program = """
            const fs = require('node:fs');
            const Editor = require(process.argv[1]);
            const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
            const results = {};
            for (const [name, [text, keys]] of Object.entries(cases)) {
                const editor = new Editor(text);
                for (const key of keys) editor.input(key);
                results[name] = [editor.text, editor.mode, [...editor.text.slice(0, editor.position)].length];
            }
            console.log(JSON.stringify(results));
        """
        path = Path(__file__).resolve().parents[1] / "src/agent_journal/static/vi.js"
        result = subprocess.run(
            ["node", "-e", program, str(path)], input=json.dumps(CASES), check=True, capture_output=True, text=True,
        )
        web = json.loads(result.stdout)
        for name, (text, keys, expected) in CASES.items():
            with self.subTest(name=name):
                editor = Editor({"body": text})
                for key in keys:
                    editor.input({ESC: "\x1b", ENTER: "\n"}.get(key, key))
                self.assertEqual(web[name], [expected, "Normal", editor.position])

    def test_terminal_wrapped_row_motions_keep_source_line_commands(self):
        editor = Editor({"body": "abcdefghijkl\nshort"})
        editor.width = 4
        for key in "gjgj":
            editor.input(key)
        self.assertEqual(editor.position, 8)
        for key in "gk":
            editor.input(key)
        self.assertEqual(editor.position, 4)
        editor.input("A")
        self.assertEqual(editor.position, 12)

    def test_too_large_paste_preserves_note(self):
        editor = Editor({"body": "x" * 100})
        for key in "yy9999p":
            editor.input(key)
        self.assertEqual(editor.text, "x" * 100)
        self.assertEqual(editor.message, "Note is too long")

    def test_recorded_macro_cannot_recurse_indefinitely(self):
        editor = Editor({"body": "first"})
        for key in "qa@aq@a":
            editor.input(key)
        self.assertEqual(editor.text, "first")
        self.assertEqual(editor.playing, 0)
