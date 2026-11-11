"""Testes da validação do catálogo (Fase 1).

Os arquivos criados aqui são bytes arbitrários em pastas temporárias: servem só para testar
existência e duplicata por hash, não representam músicas.

Rodar:  python -m unittest discover -s tests -v
"""

import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ingest  # noqa: E402


def base_row(**overrides):
    row = {
        "id": "faixa-1",
        "title": "Titulo",
        "artist": "Artista",
        "album": "",
        "genre": "Eletronica",
        "license": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "source_url": "https://example.org/faixa-1",
        "attribution": "Titulo por Artista, CC BY 4.0",
        "filename": "faixa-1.mp3",
        "duration": "",
        "status": "active",
        "date_added": "2026-11-11",
    }
    row.update(overrides)
    return row


class CatalogTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.originals = self.tmp / "originals"
        self.originals.mkdir()
        self.catalog = self.tmp / "tracks.csv"

    def tearDown(self):
        self._tmp.cleanup()

    def make_file(self, name, content=None):
        (self.originals / name).write_bytes(content if content is not None else name.encode())

    def write_catalog(self, rows, header=ingest.FIELDS):
        with self.catalog.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for row in rows:
                writer.writerow(row if isinstance(row, list) else [row[k] for k in ingest.FIELDS])

    def run_validation(self, rows):
        self.write_catalog(rows)
        return ingest.validate(ingest.read_catalog(self.catalog), self.originals)

    def assert_rejected_with(self, row, fragment):
        valid, rejected = self.run_validation([row])
        self.assertEqual(valid, [])
        self.assertEqual(len(rejected), 1)
        self.assertIn(fragment, rejected[0].message())


class TestValidRows(CatalogTestCase):
    def test_valid_row_passes_and_gets_hash(self):
        self.make_file("faixa-1.mp3")
        valid, rejected = self.run_validation([base_row()])
        self.assertEqual(rejected, [])
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(valid[0].sha256), 64)

    def test_optional_fields_may_be_empty(self):
        self.make_file("faixa-1.mp3")
        valid, rejected = self.run_validation([base_row(album="", duration="")])
        self.assertEqual(rejected, [])

    def test_all_accepted_licenses(self):
        for i, lic in enumerate(sorted(ingest.ACCEPTED_LICENSES)):
            self.make_file(f"f{i}.mp3")
        rows = [
            base_row(id=f"f{i}", filename=f"f{i}.mp3", license=lic)
            for i, lic in enumerate(sorted(ingest.ACCEPTED_LICENSES))
        ]
        valid, rejected = self.run_validation(rows)
        self.assertEqual(rejected, [])
        self.assertEqual(len(valid), 3)

    def test_whitespace_is_trimmed(self):
        self.make_file("faixa-1.mp3")
        valid, rejected = self.run_validation([base_row(id=" faixa-1 ", status="active ")])
        self.assertEqual(rejected, [])
        self.assertEqual(valid[0].data["id"], "faixa-1")

    def test_excel_bom_is_accepted(self):
        self.make_file("faixa-1.mp3")
        self.write_catalog([base_row()])
        content = self.catalog.read_bytes()
        self.catalog.write_bytes(b"\xef\xbb\xbf" + content)
        valid, rejected = ingest.validate(ingest.read_catalog(self.catalog), self.originals)
        self.assertEqual((len(valid), rejected), (1, []))


class TestRejections(CatalogTestCase):
    def setUp(self):
        super().setUp()
        self.make_file("faixa-1.mp3")

    def test_required_field_empty(self):
        for name in ("id", "title", "artist", "genre", "attribution", "status", "date_added"):
            with self.subTest(campo=name):
                self.assert_rejected_with(base_row(**{name: ""}), f"campo obrigatório vazio: {name}")

    def test_invalid_id(self):
        for bad in ("Faixa-1", "faixa_1", "faixa 1", "faixa.1"):
            with self.subTest(id=bad):
                self.assert_rejected_with(base_row(id=bad), "id inválido")

    def test_rejected_licenses(self):
        for lic in ("CC-BY-NC-4.0", "CC-BY-ND-4.0", "CC-BY-SA-4.0", "cc-by-4.0", "MIT"):
            with self.subTest(licenca=lic):
                self.assert_rejected_with(base_row(license=lic), "licença não aceita")

    def test_urls_must_be_https(self):
        for bad in ("http://example.org/x", "example.org/x", "https://", "ftp://example.org"):
            with self.subTest(url=bad):
                self.assert_rejected_with(base_row(source_url=bad), "source_url deve começar com https://")
                self.assert_rejected_with(base_row(license_url=bad), "license_url deve começar com https://")

    def test_unsafe_filename(self):
        for bad in ("../faixa-1.mp3", "sub/faixa-1.mp3", "sub\\faixa-1.mp3", "C:faixa-1.mp3", "a..mp3"):
            with self.subTest(filename=bad):
                self.assert_rejected_with(base_row(filename=bad), "filename inseguro")

    def test_missing_file(self):
        self.assert_rejected_with(base_row(filename="nao-existe.mp3"), "arquivo não encontrado")

    def test_invalid_status(self):
        self.assert_rejected_with(base_row(status="ativo"), "status inválido")

    def test_invalid_date(self):
        for bad in ("11/11/2026", "2026-2-1", "2026-02-30", "20261111"):
            with self.subTest(data=bad):
                self.assert_rejected_with(base_row(date_added=bad), "date_added inválida")

    def test_invalid_duration(self):
        for bad in ("abc", "0", "-3"):
            with self.subTest(duration=bad):
                self.assert_rejected_with(base_row(duration=bad), "duration inválida")

    def test_extra_columns(self):
        row = [base_row()[k] for k in ingest.FIELDS] + ["sobrando"]
        self.assert_rejected_with(row, "colunas a mais")

    def test_several_errors_reported_together(self):
        valid, rejected = self.run_validation([base_row(license="CC-BY-NC-4.0", status="x")])
        message = rejected[0].message()
        self.assertIn("licença não aceita", message)
        self.assertIn("status inválido", message)


class TestCrossRowRules(CatalogTestCase):
    def test_duplicate_id_rejects_later_row(self):
        self.make_file("a.mp3")
        self.make_file("b.mp3")
        valid, rejected = self.run_validation(
            [base_row(id="mesma", filename="a.mp3"), base_row(id="mesma", filename="b.mp3")]
        )
        self.assertEqual([t.data["filename"] for t in valid], ["a.mp3"])
        self.assertIn("id duplicado: já usado na linha 2", rejected[0].message())

    def test_duplicate_file_content_rejects_later_row(self):
        self.make_file("a.mp3", b"mesmo conteudo")
        self.make_file("b.mp3", b"mesmo conteudo")
        valid, rejected = self.run_validation(
            [base_row(id="a", filename="a.mp3"), base_row(id="b", filename="b.mp3")]
        )
        self.assertEqual([t.data["id"] for t in valid], ["a"])
        self.assertIn("arquivo idêntico ao da faixa 'a'", rejected[0].message())

    def test_invalid_row_does_not_stop_others(self):
        self.make_file("a.mp3")
        self.make_file("c.mp3")
        valid, rejected = self.run_validation([
            base_row(id="a", filename="a.mp3"),
            base_row(id="b", filename="nao-existe.mp3"),
            base_row(id="c", filename="c.mp3"),
        ])
        self.assertEqual([t.data["id"] for t in valid], ["a", "c"])
        self.assertEqual([r.track_id for r in rejected], ["b"])
        self.assertEqual(rejected[0].line, 3)


class TestCatalogFile(CatalogTestCase):
    def test_missing_catalog(self):
        with self.assertRaisesRegex(ingest.CatalogError, "não encontrado"):
            ingest.read_catalog(self.tmp / "nao-existe.csv")

    def test_wrong_header(self):
        self.write_catalog([], header=ingest.FIELDS[:-1])
        with self.assertRaisesRegex(ingest.CatalogError, "cabeçalho"):
            ingest.read_catalog(self.catalog)

    def test_empty_catalog_is_valid(self):
        valid, rejected = self.run_validation([])
        self.assertEqual((valid, rejected), ([], []))


class TestMain(CatalogTestCase):
    def tearDown(self):
        for handler in list(ingest.log.handlers):
            handler.close()
            ingest.log.removeHandler(handler)
        super().tearDown()

    def run_main(self):
        return ingest.main([
            "--catalog", str(self.catalog),
            "--originals", str(self.originals),
            "--logs", str(self.tmp / "logs"),
            "--validate-only",
        ])

    def read_log(self):
        files = list((self.tmp / "logs").glob("ingest-*.log"))
        self.assertEqual(len(files), 1)
        return files[0].read_text(encoding="utf-8")

    def test_exit_codes_and_log_file(self):
        self.make_file("faixa-1.mp3")
        self.write_catalog([base_row()])
        self.assertEqual(self.run_main(), 0)
        self.assertIn("1 válidas, 0 rejeitadas", self.read_log())

        self.write_catalog([base_row(), base_row(id="outra", filename="falta.mp3")])
        self.assertEqual(self.run_main(), 1)
        self.assertIn("WARNING REJEITADA linha 3, faixa outra", self.read_log())

        self.write_catalog([], header=["id"])
        self.assertEqual(self.run_main(), 2)
        self.assertIn("ERROR cabeçalho do catálogo diferente", self.read_log())


if __name__ == "__main__":
    unittest.main()
