from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "extension" / "CommonModules"


class BslComponentContractTests(unittest.TestCase):
    def test_queue_policy_has_complete_lease_lifecycle(self) -> None:
        exports = self._exported_functions("HrIntegrationQueuePolicy")

        self.assertTrue(
            {
                "НоваяПолитика",
                "МожноЗахватить",
                "ПодготовитьЗахват",
                "ПодготовитьЗавершение",
                "ПодготовитьОшибку",
                "ЗадержкаПередПовтором",
            }.issubset(exports)
        )

    def test_repository_uses_optimistic_compare_and_swap(self) -> None:
        text = self._module_text("HrIntegrationQueueRepository")

        self.assertIn("ОбновитьЕслиВерсия", text)
        self.assertIn("ДобавитьЕслиОтсутствует", text)
        self.assertIn("ПолучитьКандидатов", text)

    def test_dto_and_attachment_components_expose_neutral_contracts(self) -> None:
        self.assertTrue(
            {"НовоеПравило", "Преобразовать"}.issubset(self._exported_functions("HrIntegrationDTOMapper"))
        )
        self.assertTrue(
            {"ПодготовитьВложение", "СохранитьЕслиОтсутствует"}.issubset(
                self._exported_functions("HrIntegrationAttachments")
            )
        )

    def test_bsl_lines_do_not_exceed_120_characters(self) -> None:
        violations: list[str] = []
        for path in sorted(MODULES.rglob("*.bsl")):
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if len(line) > 120:
                    violations.append(f"{path.relative_to(ROOT)}:{line_number}:{len(line)}")

        self.assertEqual([], violations)

    def _exported_functions(self, module_name: str) -> set[str]:
        text = self._module_text(module_name)
        pattern = r"(?ims)^Функция\s+([А-ЯA-ZЁа-яa-zё0-9_]+)(?:(?!^КонецФункции).)*?\bЭкспорт\b"
        return set(re.findall(pattern, text))

    def _module_text(self, module_name: str) -> str:
        path = MODULES / module_name / "Ext" / "Module.bsl"
        return path.read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
