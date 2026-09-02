"""Two output dialects for the generated documents: GitHub Markdown and Confluence markup.

The generators call one small vocabulary - heading, paragraph, table, diagram, code,
panel, expand - and each dialect renders it its own way. That is what keeps the page a
team reads and the file a reviewer diffs from drifting apart: they are one document
emitted twice, not two documents maintained in parallel.

Diagrams go through diagram() rather than code(), because a Confluence {code} macro
syntax-colours its body and an ASCII box drawing is not code.
"""
import sys, textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import confluence as cf


class Markdown:
    ext = "md"

    def __init__(self):
        self.o = []

    def h(self, level, text):
        self.o.append("#" * level + " " + text + "\n")

    def para(self, text):
        # Wrapped for a readable diff. Confluence must not do this: in wiki markup a bare
        # newline is a line break, not whitespace.
        self.o.append(textwrap.fill(" ".join(text.split()), width=92) + "\n")

    def table(self, headers, rows, align=None):
        align = align or ["---"] * len(headers)
        self.o.append("| " + " | ".join(headers) + " |")
        self.o.append("|" + "|".join(align) + "|")
        for r in rows:
            self.o.append("| " + " | ".join(str(x) for x in r) + " |")
        self.o.append("")

    def diagram(self, body, title=None):
        self.o.append("```")
        self.o.append(body.rstrip())
        self.o.append("```\n")

    def code(self, body, title=None, language=None):
        self.o.append("```" + (language or ""))
        self.o.append(body.rstrip())
        self.o.append("```\n")

    def panel(self, kind, body, title=None):
        out = []
        if title:
            out.append(f"**{title}**")
        for para in body.split("\n"):
            out.append(textwrap.fill(" ".join(para.split()), width=90))
        self.o.append("> " + "\n>\n> ".join(out).replace("\n", "\n> ")
                      .replace("> >", ">") + "\n")

    def raw_code(self, body, language=None):
        """A code block as a string, for nesting inside expand()."""
        return "```" + (language or "") + "\n" + body.rstrip() + "\n```"

    def bullets(self, items):
        out = []
        for it in items:
            wrapped = textwrap.fill(" ".join(it.split()), width=90,
                                    initial_indent="- ", subsequent_indent="  ")
            out.append(wrapped)
        self.o.append("\n".join(out) + "\n")

    def expand(self, title, body):
        self.o.append(f"<details><summary>{title}</summary>\n")
        self.o.append(body.rstrip() + "\n")
        self.o.append("</details>\n")

    def toc(self):
        pass

    def mono(self, text):
        return f"`{text}`"

    def anchor(self, name):
        pass

    def text(self):
        return "\n".join(self.o) + "\n"


class Confluence:
    ext = "confluence"

    def __init__(self):
        self.o = []

    def h(self, level, text):
        self.o.append(cf.h(level, text))

    def para(self, text):
        self.o.append(cf.para(text))

    def table(self, headers, rows, align=None):
        self.o.append(cf.table([self._plain(x) for x in headers],
                               [[self._plain(c) for c in r] for r in rows]))

    @staticmethod
    def _plain(x):
        # Markdown emphasis does not exist in wiki markup; the callers build cells with
        # this class's own mono()/bold(), so only stray backticks need translating.
        return str(x).replace("`", "")

    def diagram(self, body, title=None):
        self.o.append(cf.noformat(body, title))

    def code(self, body, title=None, language=None):
        self.o.append(cf.code(body, title, language))

    def panel(self, kind, body, title=None):
        self.o.append(cf.panel(kind, body, title))

    def raw_code(self, body, language=None):
        """A code block as a string, for nesting inside expand()."""
        return cf.code(body, language=language)

    def bullets(self, items):
        # Wiki-markup list items must be consecutive lines; a blank line ends the list.
        self.o.append("\n".join("* " + " ".join(it.split()) for it in items) + "\n")

    def expand(self, title, body):
        self.o.append(cf.expand(title, body))

    def toc(self):
        self.o.append(cf.toc())

    def mono(self, text):
        return cf.mono(text)

    def anchor(self, name):
        self.o.append(cf.anchor(name))

    def text(self):
        return "\n".join(self.o)


