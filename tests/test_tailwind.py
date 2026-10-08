"""Meaningful coverage for the optional, advisory Tailwind source audit."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agent_design_system.cli import main
from agent_design_system.format import FormatError
from agent_design_system.tailwind import (
    _base_utility,
    _color_utility,
    _literal_rgb,
    _theme_colors,
    audit_tailwind,
)
from agent_design_system.tsx import _verify_installed_parser


class TailwindAuditTests(unittest.TestCase):
    def test_variant_and_color_classification(self) -> None:
        self.assertEqual(_base_utility("hover:[&>a]:bg-[#123456]"), "bg-[#123456]")
        self.assertEqual(_color_utility("text-2xl"), None)
        self.assertEqual(_color_utility("border-2"), None)
        self.assertEqual(_color_utility("dark:bg-red-500"), ("bg", "red-500"))
        self.assertEqual(_literal_rgb("#1234"), "#112233")
        self.assertEqual(_literal_rgb("#12345678"), "#123456")

    def test_tsx_audit_reports_static_colors_and_dynamic_gap(self) -> None:
        try:
            _verify_installed_parser()
        except FormatError:
            self.skipTest("optional exact TypeScript parser is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            source.mkdir()
            (source / "View.tsx").write_text(
                "export const View = ({kind}: {kind: string}) => "
                "<main className={`bg-brand ${kind} text-white`}>"
                '<p className={cn("hover:bg-red-500", {"bg-[#123456]": true})} />'
                '<span className={kind && "text-red-600"} />'
                "</main>;\n",
                encoding="utf-8",
            )
            (root / "theme.css").write_text(
                '@import "tailwindcss";\n@theme { --color-brand: #abcdef; '
                "--color-secondary: oklch(0.5 0.1 30); }\n",
                encoding="utf-8",
            )
            (root / "canon.json").write_text(
                json.dumps({"swatches": {"web": {"hexes": ["abcdef", "ffffff"]}}}),
                encoding="utf-8",
            )
            report = audit_tailwind(
                tsx_root=source,
                theme_css=root / "theme.css",
                canon=root / "canon.json",
                canon_path="swatches.web.hexes",
            )
            self.assertEqual(report["coverage"]["tsx_files"], 1)
            self.assertEqual(report["coverage"]["dynamic_class_attributes"], 2)
            self.assertEqual(report["finding_counts"]["tsx-dynamic-class"], 2)
            self.assertEqual(report["finding_counts"]["default-palette-color"], 2)
            self.assertEqual(
                report["finding_counts"]["arbitrary-color-outside-canon"], 1
            )
            self.assertEqual(report["finding_counts"]["theme-unresolved-color"], 1)
            self.assertNotIn("theme-outside-canon", report["finding_counts"])
            self.assertEqual(
                (source / "View.tsx").read_text(encoding="utf-8").count("bg-brand"), 1
            )
            command = [
                "tailwind-audit",
                "--tsx-root",
                str(source),
                "--theme-css",
                str(root / "theme.css"),
                "--canon",
                str(root / "canon.json"),
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(command), 0)
                self.assertEqual(main(command + ["--fail-on-findings"]), 1)

    def test_symlink_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            source.mkdir()
            (root / "outside.tsx").write_text(
                "export default <div />", encoding="utf-8"
            )
            (source / "linked.tsx").symlink_to(root / "outside.tsx")
            (root / "theme.css").write_text(
                "@theme { --color-brand: #abcdef; }", encoding="utf-8"
            )
            (root / "canon.json").write_text('{"hexes":["abcdef"]}', encoding="utf-8")
            with self.assertRaisesRegex(FormatError, "symlink inside TSX root"):
                audit_tailwind(
                    tsx_root=source,
                    theme_css=root / "theme.css",
                    canon=root / "canon.json",
                    canon_path="hexes",
                )

    def test_malformed_theme_declaration_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            theme = Path(directory) / "theme.css"
            theme.write_text("@theme { --color-brand: #123456 }", encoding="utf-8")
            with self.assertRaisesRegex(FormatError, "malformed @theme color"):
                _theme_colors(theme)


if __name__ == "__main__":
    unittest.main()
