"""Testes do upload e das saídas geradas (Fase 4).

Unitários: leitura da saída do rclone check, config [r2], playlist.json, créditos.
Integração: o "bucket" é uma pasta temporária local (o rclone aceita caminho local como destino),
então nada sai da máquina. Pulados se rclone (e, no teste ponta a ponta, FFmpeg) não estiver no PATH.
O áudio de teste é um tom senoidal sintético, não música.

Rodar:  python -m unittest discover -s tests -v
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ingest  # noqa: E402

HAS_RCLONE = bool(shutil.which("rclone"))
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
BASE = "https://pub-teste.r2.dev"


def entry(track_id, **overrides):
    e = {
        "id": track_id, "title": f"Titulo {track_id}", "artist": "Artista", "album": "",
        "genre": "Eletronica", "license": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "source_url": f"https://example.org/{track_id}", "attribution": f"{track_id} por Artista, CC BY 4.0",
        "url": f"{BASE}/{track_id}-00000000.mp3", "duration": 100.0,
    }
    e.update(overrides)
    return e


class TestParseCheck(unittest.TestCase):
    def test_markers(self):
        out = "= a.mp3\n+ c.mp3\n* b.mp3\n! d.mp3\nlinha qualquer\n"
        self.assertEqual(
            ingest.parse_check(out), {"a.mp3": "=", "c.mp3": "+", "b.mp3": "*", "d.mp3": "!"}
        )

    def test_empty(self):
        self.assertEqual(ingest.parse_check(""), {})


class TestPublishConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def load(self, body):
        self.path.write_text(body, encoding="utf-8")
        return ingest.load_publish_config(self.path)

    def test_project_config_is_valid(self):
        cfg = ingest.load_publish_config(ingest.ROOT / "scripts" / "config.toml")
        self.assertTrue(cfg["public_base_url"].startswith("https://"))
        self.assertTrue(cfg["remote"])

    def test_trailing_slash_removed(self):
        cfg = self.load('[r2]\nremote = "r2:b"\npublic_base_url = "https://x.r2.dev/"\n')
        self.assertEqual(cfg["public_base_url"], "https://x.r2.dev")

    def test_missing_or_invalid(self):
        with self.assertRaises(ingest.ConfigError) as ctx:
            self.load('[r2]\npublic_base_url = "http://x.r2.dev"\n')
        self.assertIn("remote", str(ctx.exception))
        self.assertIn("public_base_url", str(ctx.exception))


class TestPlaylist(unittest.TestCase):
    def test_entry_from_track(self):
        data = {k: f"v-{k}" for k in ingest.FIELDS}
        track = ingest.Track(line=2, data=data)
        processed = ingest.Processed("x", Path("media/processed/x-1234abcd.mp3"), 123.456789, False)
        e = ingest.playlist_entry(track, processed, BASE)
        self.assertEqual(e["url"], f"{BASE}/x-1234abcd.mp3")
        self.assertEqual(e["duration"], 123.46)
        self.assertNotIn("filename", e)
        self.assertNotIn("status", e)
        self.assertNotIn("date_added", e)

    def test_sorted_by_id_and_deterministic(self):
        a = ingest.build_playlist([entry("b"), entry("a"), entry("c")])
        b = ingest.build_playlist([entry("c"), entry("a"), entry("b")])
        self.assertEqual(a, b)
        doc = json.loads(a)
        self.assertEqual([t["id"] for t in doc["tracks"]], ["a", "b", "c"])
        self.assertEqual(doc["version"], 1)
        self.assertTrue(a.endswith("}\n"))

    def test_keys_sorted_and_accents_kept(self):
        text = ingest.build_playlist([entry("a", title="Canção")])
        self.assertIn("Canção", text)
        track = json.loads(text)["tracks"][0]
        self.assertEqual(list(track), sorted(track))

    def test_empty(self):
        self.assertEqual(json.loads(ingest.build_playlist([]))["tracks"], [])


class TestCredits(unittest.TestCase):
    def test_lists_tracks_with_links(self):
        page = ingest.build_credits([entry("b", album="Disco"), entry("a")])
        self.assertLess(page.index("Titulo a"), page.index("Titulo b"))
        self.assertIn('href="https://creativecommons.org/licenses/by/4.0/" rel="license">CC BY 4.0</a>', page)
        self.assertIn('href="https://example.org/a"', page)
        self.assertIn("Artista · Disco", page)
        self.assertIn(ingest.GENERATED_NOTICE, page)

    def test_escapes_catalog_text(self):
        page = ingest.build_credits([entry("a", title="<script>x</script>", artist="A & B")])
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("A &amp; B", page)

    def test_empty(self):
        self.assertIn("Nenhuma faixa publicada", ingest.build_credits([]))


class TestWriteIfChanged(unittest.TestCase):
    def test_writes_only_on_change_with_lf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "f.txt"
            self.assertTrue(ingest.write_if_changed(path, "a\nb\n"))
            self.assertFalse(ingest.write_if_changed(path, "a\nb\n"))
            self.assertEqual(path.read_bytes(), b"a\nb\n")
            self.assertTrue(ingest.write_if_changed(path, "c\n"))


@unittest.skipUnless(HAS_RCLONE, "rclone não está no PATH")
class TestUploadLocal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.local = tmp / "processed"
        self.bucket = tmp / "bucket"
        self.local.mkdir()
        self.bucket.mkdir()
        for name in ("a-1.mp3", "b-2.mp3"):
            (self.local / name).write_bytes(name.encode() * 100)
        (self.local / "velho-0.mp3").write_bytes(b"fora da lista")

    def tearDown(self):
        self._tmp.cleanup()

    def test_upload_then_nothing_to_send(self):
        names = ["a-1.mp3", "b-2.mp3"]
        confirmed, sent = ingest.upload(names, self.local, str(self.bucket))
        self.assertEqual((confirmed, sent), (set(names), 2))
        self.assertEqual(sorted(p.name for p in self.bucket.iterdir()), names)  # só os da lista

        confirmed, sent = ingest.upload(names, self.local, str(self.bucket))
        self.assertEqual((confirmed, sent), (set(names), 0))

    def test_different_file_in_bucket_is_resent(self):
        (self.bucket / "a-1.mp3").write_bytes(b"corrompido")
        confirmed, sent = ingest.upload(["a-1.mp3"], self.local, str(self.bucket))
        self.assertEqual((confirmed, sent), ({"a-1.mp3"}, 1))
        self.assertEqual((self.bucket / "a-1.mp3").read_bytes(), (self.local / "a-1.mp3").read_bytes())

    def test_unreachable_remote_is_fatal(self):
        with self.assertRaises(ingest.PublishError):
            ingest.upload(["a-1.mp3"], self.local, "remote-que-nao-existe:bucket")

    def test_nothing_to_upload(self):
        self.assertEqual(ingest.upload([], self.local, "remote-que-nao-existe:bucket"), (set(), 0))


@unittest.skipUnless(HAS_RCLONE and HAS_FFMPEG, "rclone ou FFmpeg não está no PATH")
class TestMainEndToEnd(unittest.TestCase):
    """Critério de pronto da Fase 4: a segunda execução não envia nada e não muda as saídas."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.originals = self.tmp / "originals"
        self.originals.mkdir()
        for name, freq in (("tom.wav", 440), ("parado.wav", 550)):
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi",
                 "-i", f"sine=frequency={freq}:sample_rate=44100:duration=3", "-ac", "2",
                 str(self.originals / name)],
                check=True,
            )
        self.catalog = self.tmp / "tracks.csv"
        self.catalog.write_text(
            ",".join(ingest.FIELDS) + "\n"
            "tom,Tom de teste,Gerador,,Teste,CC0-1.0,https://creativecommons.org/publicdomain/zero/1.0/,"
            "https://example.org/tom,Tom de teste (fixture),tom.wav,,active,2026-11-12\n"
            "parado,Parado,Gerador,,Teste,CC0-1.0,https://creativecommons.org/publicdomain/zero/1.0/,"
            "https://example.org/parado,Parado (fixture),parado.wav,,inactive,2026-11-12\n",
            encoding="utf-8",
        )
        self.bucket = self.tmp / "bucket"
        self.bucket.mkdir()
        self.site = self.tmp / "site"
        self.config = self.tmp / "config.toml"
        self.write_config(str(self.bucket))

    def tearDown(self):
        for handler in list(ingest.log.handlers):
            handler.close()
            ingest.log.removeHandler(handler)
        self._tmp.cleanup()

    def write_config(self, remote):
        project = (ingest.ROOT / "scripts" / "config.toml").read_text(encoding="utf-8")
        audio = project[project.index("\n[audio]\n") + 1:]
        self.config.write_text(
            f"[r2]\nremote = '{remote}'\npublic_base_url = \"{BASE}\"\n\n{audio}", encoding="utf-8"
        )

    def run_main(self):
        return ingest.main([
            "--catalog", str(self.catalog), "--originals", str(self.originals),
            "--processed", str(self.tmp / "processed"), "--config", str(self.config),
            "--logs", str(self.tmp / "logs"), "--site", str(self.site),
        ])

    def log_text(self):
        return next((self.tmp / "logs").glob("ingest-*.log")).read_text(encoding="utf-8")

    def outputs(self):
        return [(self.site / p).read_bytes() for p in ("playlist.json", "creditos/index.html")]

    def test_second_run_sends_nothing_and_changes_nothing(self):
        self.assertEqual(self.run_main(), 0)
        first = self.outputs()
        playlist = json.loads(first[0])
        self.assertEqual([t["id"] for t in playlist["tracks"]], ["tom"])  # inativa fica de fora
        name = playlist["tracks"][0]["url"].removeprefix(BASE + "/")
        self.assertTrue((self.bucket / name).is_file())
        self.assertIn("1 enviadas, 1 na playlist", self.log_text())

        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.outputs(), first)
        self.assertIn("0 enviadas, 1 na playlist", self.log_text())
        self.assertIn("inalterado", self.log_text())

    def test_unreachable_bucket_keeps_previous_outputs(self):
        self.assertEqual(self.run_main(), 0)
        first = self.outputs()
        self.write_config("remote-que-nao-existe:bucket")
        self.assertEqual(self.run_main(), 2)
        self.assertEqual(self.outputs(), first)
        self.assertIn("NÃO foram regenerados", self.log_text())


if __name__ == "__main__":
    unittest.main()
