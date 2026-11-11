"""Testes do processamento de áudio (Fase 2).

Unitários: análise da saída do silencedetect e do loudnorm, nome de saída, config.
Integração: usa um tom senoidal sintético gerado pelo FFmpeg (não é música); pulados se o
FFmpeg não estiver no PATH.

Rodar:  python -m unittest discover -s tests -v
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ingest  # noqa: E402

CFG = {
    "target_lufs": -14.0,
    "true_peak": -1.5,
    "loudness_range": 20.0,
    "bitrate_kbps": 160,
    "sample_rate": 44100,
    "silence_threshold_db": -50.0,
    "silence_min_seconds": 1.0,
    "lufs_tolerance": 1.0,
}

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class TestSilenceParsing(unittest.TestCase):
    STDERR = (
        "[silencedetect @ 0x1] silence_start: 0\n"
        "[silencedetect @ 0x1] silence_end: 2.0005 | silence_duration: 2.0005\n"
        "size=N/A time=00:00:05.00\n"
        "[silencedetect @ 0x1] silence_start: 5.0\n"
        "[silencedetect @ 0x1] silence_end: 7.0 | silence_duration: 2\n"
        "[silencedetect @ 0x1] silence_start: 10.0\n"
        "[silencedetect @ 0x1] silence_end: 13.0 | silence_duration: 3\n"
    )

    def test_periods(self):
        self.assertEqual(
            ingest.silence_periods(self.STDERR),
            [(0.0, 2.0005), (5.0, 7.0), (10.0, 13.0)],
        )

    def test_trims_only_edges(self):
        start, end = ingest.trim_range(ingest.silence_periods(self.STDERR), 13.0)
        self.assertAlmostEqual(start, 2.0005)
        self.assertAlmostEqual(end, 10.0)  # o silêncio de 5 a 7 s fica

    def test_trailing_silence_without_end_line(self):
        start, end = ingest.trim_range([(4.0, None)], 6.0)
        self.assertEqual((start, end), (0.0, 4.0))

    def test_no_silence_keeps_everything(self):
        self.assertEqual(ingest.trim_range([], 30.0), (0.0, 30.0))

    def test_middle_silence_only_is_kept(self):
        self.assertEqual(ingest.trim_range([(10.0, 12.0)], 30.0), (0.0, 30.0))

    def test_all_silent_is_error(self):
        with self.assertRaisesRegex(ingest.ProcessingError, "inteira"):
            ingest.trim_range([(0.0, None)], 30.0)
        with self.assertRaisesRegex(ingest.ProcessingError, "inteira"):
            ingest.trim_range([(0.0, 30.0)], 30.0)

    def test_too_short_after_trim_is_error(self):
        with self.assertRaisesRegex(ingest.ProcessingError, "menos de 1 s"):
            ingest.trim_range([(0.0, 2.0), (2.5, None)], 5.0)


class TestLoudnormParsing(unittest.TestCase):
    def test_extracts_last_json_block(self):
        stderr = (
            "Input #0, mp3 ... {not json\n"
            "[Parsed_loudnorm_0 @ 0x1] \n{\n"
            '\t"input_i" : "-20.31",\n\t"input_tp" : "-3.10",\n'
            '\t"normalization_type" : "linear"\n}\n'
        )
        stats = ingest.loudnorm_stats(stderr)
        self.assertEqual(stats["input_i"], "-20.31")
        self.assertEqual(stats["normalization_type"], "linear")

    def test_missing_block_is_error(self):
        with self.assertRaises(ingest.ProcessingError):
            ingest.loudnorm_stats("nada aqui")


class TestOutputName(unittest.TestCase):
    def test_format_and_stability(self):
        name = ingest.output_name("faixa-1", "a" * 64, CFG)
        self.assertRegex(name, r"^faixa-1-[0-9a-f]{8}\.mp3$")
        self.assertEqual(name, ingest.output_name("faixa-1", "a" * 64, dict(CFG)))

    def test_changes_with_source_or_params(self):
        base = ingest.output_name("faixa-1", "a" * 64, CFG)
        self.assertNotEqual(base, ingest.output_name("faixa-1", "b" * 64, CFG))
        self.assertNotEqual(base, ingest.output_name("faixa-1", "a" * 64, {**CFG, "target_lufs": -16.0}))


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, **overrides):
        values = {**CFG, **overrides}
        lines = ["[audio]"] + [f"{k} = {v}" for k, v in values.items() if v is not None]
        self.path.write_text("\n".join(lines), encoding="utf-8")

    def test_project_config_is_valid(self):
        cfg = ingest.load_config(ingest.ROOT / "scripts" / "config.toml")
        self.assertEqual(set(cfg), set(ingest.AUDIO_LIMITS))

    def test_out_of_range(self):
        self.write(target_lufs=-23.0, bitrate_kbps=320)
        with self.assertRaises(ingest.ConfigError) as ctx:
            ingest.load_config(self.path)
        self.assertIn("target_lufs", str(ctx.exception))
        self.assertIn("bitrate_kbps", str(ctx.exception))

    def test_missing_key(self):
        self.write(true_peak=None)
        with self.assertRaisesRegex(ingest.ConfigError, "true_peak ausente"):
            ingest.load_config(self.path)

    def test_missing_file(self):
        with self.assertRaisesRegex(ingest.ConfigError, "não encontrado"):
            ingest.load_config(self.path)


@unittest.skipUnless(HAS_FFMPEG, "FFmpeg não está no PATH")
class TestProcessingIntegration(unittest.TestCase):
    """Tom de 440 Hz: 2 s de silêncio, 3 s de tom, 2 s de silêncio, 3 s de tom, 3 s de silêncio."""

    TONE = "if(between(t,2,10),if(between(t,5,7),0,0.1*sin(2*PI*440*t)),0)"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.originals = cls.tmp / "originals"
        cls.originals.mkdir()
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi",
             "-i", f"aevalsrc='{cls.TONE}':s=44100:d=13", "-ac", "2",
             str(cls.originals / "tom.wav")],
            check=True,
        )
        (cls.originals / "corrompido.mp3").write_bytes(b"isto nao e audio" * 1000)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.processed = self.tmp / f"processed-{self._testMethodName}"

    def track(self, track_id, filename):
        t = ingest.Track(line=2, data={"id": track_id, "filename": filename, "status": "active"})
        t.sha256 = ingest.sha256_of(self.originals / filename)
        return t

    def test_process_trims_edges_normalizes_and_is_idempotent(self):
        track = self.track("tom", "tom.wav")
        first = ingest.process_track(track, self.originals, self.processed, CFG)
        self.assertFalse(first.skipped)
        self.assertRegex(first.file.name, r"^tom-[0-9a-f]{8}\.mp3$")
        # 13 s - 2 s no início - 3 s no fim = 8 s, com o silêncio do meio preservado
        self.assertAlmostEqual(first.duration, 8.0, delta=0.15)

        info = ingest.probe(first.file)
        self.assertEqual(info["codec"], "mp3")
        self.assertEqual(info["sample_rate"], 44100)
        self.assertAlmostEqual(info["bit_rate"] / 1000, 160, delta=3)

        lufs = float(ingest.measure_loudness(first.file, CFG)["input_i"])
        self.assertAlmostEqual(lufs, CFG["target_lufs"], delta=CFG["lufs_tolerance"])

        second = ingest.process_track(track, self.originals, self.processed, CFG)
        self.assertTrue(second.skipped)
        self.assertEqual(second.file, first.file)
        self.assertEqual(list(self.processed.glob("*.part")), [])

    def test_param_change_reprocesses_with_new_name(self):
        track = self.track("tom", "tom.wav")
        a = ingest.process_track(track, self.originals, self.processed, CFG)
        b = ingest.process_track(track, self.originals, self.processed, {**CFG, "target_lufs": -16.0})
        self.assertFalse(b.skipped)
        self.assertNotEqual(a.file.name, b.file.name)
        lufs = float(ingest.measure_loudness(b.file, CFG)["input_i"])
        self.assertAlmostEqual(lufs, -16.0, delta=1.0)

    def test_corrupt_input_fails_without_leaving_files(self):
        track = self.track("ruim", "corrompido.mp3")
        with self.assertRaises(ingest.ProcessingError):
            ingest.process_track(track, self.originals, self.processed, CFG)
        self.assertFalse(self.processed.exists() and any(self.processed.iterdir()))

    def test_process_all_continues_after_failure(self):
        tracks = [self.track("ruim", "corrompido.mp3"), self.track("tom", "tom.wav")]
        inactive = self.track("parada", "tom.wav")
        inactive.data["status"] = "inactive"
        done, failed = ingest.process_all(tracks + [inactive], self.originals, self.processed, CFG)
        self.assertEqual([p.track_id for p in done], ["tom"])
        self.assertEqual([f.track_id for f in failed], ["ruim"])
        self.assertIn("processamento:", failed[0].message())


if __name__ == "__main__":
    unittest.main()
