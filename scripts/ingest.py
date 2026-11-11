"""Ingest do catálogo da Rádio Academia.

Etapas:
  1. Validação de catalog/tracks.csv (Fase 1).
  2. Processamento de áudio das faixas ativas (Fase 2): corte de silêncio nas pontas,
     loudnorm em duas passagens, MP3 CBR em media/processed/{id}-{hash8}.mp3.

Faixa inválida ou com falha de processamento é registrada no log e pulada; o ingest nunca
aborta por causa de uma faixa. Só aborta (código 2) quando o catálogo ou o config.toml não
podem ser lidos, ou quando o FFmpeg não está instalado.

Uso:
    python scripts/ingest.py                  # valida e processa
    python scripts/ingest.py --validate-only  # só valida o CSV

Códigos de saída: 0 = tudo certo, 1 = alguma faixa rejeitada ou com falha, 2 = erro fatal.
"""

import argparse
import csv
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent

# Mudar esta versão força o reprocessamento de tudo quando a lógica de processamento mudar.
PIPELINE_VERSION = 1

FIELDS = [
    "id", "title", "artist", "album", "genre", "license", "license_url",
    "source_url", "attribution", "filename", "duration", "status", "date_added",
]
# duration no CSV é opcional e informativa: a duração real vem do ffprobe na saída
# processada e vai para o playlist.json (Fase 4). O ingest não reescreve o CSV.
OPTIONAL = {"album", "duration"}
ACCEPTED_LICENSES = {"CC0-1.0", "CC-BY-3.0", "CC-BY-4.0"}
STATUSES = {"active", "inactive"}
ID_RE = re.compile(r"[a-z0-9-]+")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

log = logging.getLogger("ingest")


class CatalogError(Exception):
    """Catálogo inteiro ilegível: arquivo ausente ou cabeçalho diferente do esperado."""


class ConfigError(Exception):
    """config.toml ausente, ilegível ou com valor fora da faixa permitida."""


class ProcessingError(Exception):
    """Falha ao processar uma faixa. A faixa é pulada; o ingest continua."""


@dataclass
class Track:
    line: int
    data: dict
    sha256: str = ""


@dataclass
class Rejection:
    line: int
    track_id: str
    errors: list = field(default_factory=list)

    def message(self):
        ident = self.track_id or "(sem id)"
        return f"linha {self.line}, faixa {ident}: " + "; ".join(self.errors)


def read_catalog(path):
    """Lê o CSV e devolve a lista de Track (sem validar campos). Valores com espaços nas pontas são aparados."""
    if not path.is_file():
        raise CatalogError(f"catálogo não encontrado: {path}")
    # utf-8-sig: aceita o BOM que o Excel grava ao salvar CSV UTF-8
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        if header != FIELDS:
            raise CatalogError(
                "cabeçalho do catálogo diferente do esperado.\n"
                f"  esperado:   {','.join(FIELDS)}\n"
                f"  encontrado: {','.join(header)}"
            )
        tracks = []
        for row in reader:
            extra = row.pop(None, None)  # valores além das colunas do cabeçalho
            data = {k: (v or "").strip() for k, v in row.items()}
            track = Track(line=reader.line_num, data=data)
            if extra:
                track.data["_extra"] = extra
            tracks.append(track)
    return tracks


def _is_https_url(value):
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _is_safe_filename(value):
    # ":" também é recusado: no Windows "C:arquivo.mp3" aponta para fora da pasta
    return not any(bad in value for bad in ("/", "\\", "..", ":"))


def _is_iso_date(value):
    if not DATE_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _is_duration(value):
    try:
        return float(value) > 0
    except ValueError:
        return False


def field_errors(data, originals_dir):
    """Erros de uma linha isolada (sem olhar as outras linhas)."""
    errors = []
    if "_extra" in data:
        errors.append(f"colunas a mais na linha: {data['_extra']}")

    for name in FIELDS:
        if name not in OPTIONAL and not data.get(name):
            errors.append(f"campo obrigatório vazio: {name}")

    track_id = data.get("id", "")
    if track_id and not ID_RE.fullmatch(track_id):
        errors.append(f"id inválido '{track_id}': use só letras minúsculas, números e hífen")

    lic = data.get("license", "")
    if lic and lic not in ACCEPTED_LICENSES:
        errors.append(
            f"licença não aceita '{lic}': aceitas {', '.join(sorted(ACCEPTED_LICENSES))}"
        )

    for name in ("license_url", "source_url"):
        value = data.get(name, "")
        if value and not _is_https_url(value):
            errors.append(f"{name} deve começar com https:// e ter um domínio: '{value}'")

    status = data.get("status", "")
    if status and status not in STATUSES:
        errors.append(f"status inválido '{status}': use active ou inactive")

    added = data.get("date_added", "")
    if added and not _is_iso_date(added):
        errors.append(f"date_added inválida '{added}': use AAAA-MM-DD")

    duration = data.get("duration", "")
    if duration and not _is_duration(duration):
        errors.append(f"duration inválida '{duration}': deve ser número de segundos maior que zero")

    filename = data.get("filename", "")
    if filename:
        if not _is_safe_filename(filename):
            errors.append(f"filename inseguro '{filename}': não use / \\ .. ou :")
        elif not (originals_dir / filename).is_file():
            errors.append(f"arquivo não encontrado em {originals_dir.name}/: '{filename}'")

    return errors


def sha256_of(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def validate(tracks, originals_dir):
    """Valida todas as faixas. Devolve (válidas, rejeitadas).

    Em id ou arquivo duplicado, a primeira ocorrência vale e as seguintes são rejeitadas.
    """
    valid, rejected = [], []
    seen_ids, seen_hashes = {}, {}

    for track in tracks:
        data = track.data
        errors = field_errors(data, originals_dir)

        track_id = data.get("id", "")
        if track_id:
            if track_id in seen_ids:
                errors.append(f"id duplicado: já usado na linha {seen_ids[track_id]}")
            else:
                seen_ids[track_id] = track.line

        if not errors:
            try:
                track.sha256 = sha256_of(originals_dir / data["filename"])
            except OSError as exc:
                errors.append(f"não foi possível ler o arquivo: {exc}")
            else:
                first = seen_hashes.get(track.sha256)
                if first:
                    errors.append(
                        f"arquivo idêntico ao da faixa '{first.data['id']}' (linha {first.line})"
                    )
                else:
                    seen_hashes[track.sha256] = track

        if errors:
            rejected.append(Rejection(track.line, track_id, errors))
        else:
            valid.append(track)

    return valid, rejected


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

AUDIO_LIMITS = {
    # chave: (mínimo, máximo)
    "target_lufs": (-16.0, -14.0),
    "true_peak": (-9.0, 0.0),
    "loudness_range": (1.0, 50.0),
    "bitrate_kbps": (128, 160),
    "sample_rate": (44100, 48000),
    "silence_threshold_db": (-90.0, -30.0),
    "silence_min_seconds": (0.1, 10.0),
    "lufs_tolerance": (0.1, 3.0),
}


def load_config(path):
    """Lê e valida a seção [audio] do config.toml. Devolve um dict com todas as chaves de AUDIO_LIMITS."""
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config não encontrado: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config.toml inválido: {exc}") from None

    audio = data.get("audio", {})
    errors = []
    for key, (low, high) in AUDIO_LIMITS.items():
        value = audio.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            errors.append(f"[audio].{key} ausente ou não numérico")
        elif not low <= value <= high:
            errors.append(f"[audio].{key} = {value} fora da faixa {low} a {high}")
    if errors:
        raise ConfigError("config.toml: " + "; ".join(errors))
    return {key: audio[key] for key in AUDIO_LIMITS}


# ---------------------------------------------------------------------------
# Processamento de áudio (FFmpeg)
# ---------------------------------------------------------------------------

def run(cmd):
    """Roda um comando e devolve o resultado. Código de saída diferente de zero vira ProcessingError."""
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        tail = " | ".join(result.stderr.strip().splitlines()[-3:])
        raise ProcessingError(f"{Path(cmd[0]).stem} falhou (código {result.returncode}): {tail}")
    return result


def probe(path):
    """Integridade via ffprobe: precisa ter um stream de áudio e duração positiva."""
    result = run([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,sample_rate,channels:format=duration,bit_rate",
        "-of", "json", str(path),
    ])
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise ProcessingError(f"sem stream de áudio: {path.name}")
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, ValueError):
        raise ProcessingError(f"duração ilegível: {path.name}") from None
    if duration <= 0:
        raise ProcessingError(f"duração zero: {path.name}")
    stream = streams[0]
    return {
        "codec": stream.get("codec_name"),
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": stream.get("channels"),
        "duration": duration,
        "bit_rate": int(data["format"].get("bit_rate") or 0),
    }


SILENCE_START_RE = re.compile(r"silence_start: (-?[\d.]+)")
SILENCE_END_RE = re.compile(r"silence_end: (-?[\d.]+)")
OUT_TIME_RE = re.compile(r"out_time_us=(\d+)")
EDGE_TOLERANCE = 0.1  # segundos: folga para considerar que um silêncio encosta na ponta


def silence_periods(stderr):
    """Lista de (início, fim) dos silêncios relatados pelo silencedetect. Fim None = vai até o fim do arquivo."""
    periods = []
    for line in stderr.splitlines():
        start = SILENCE_START_RE.search(line)
        if start:
            periods.append([float(start.group(1)), None])
            continue
        end = SILENCE_END_RE.search(line)
        if end and periods and periods[-1][1] is None:
            periods[-1][1] = float(end.group(1))
    return [tuple(p) for p in periods]


def trim_range(periods, decoded_duration):
    """Trecho a manter (início, fim) cortando só silêncios que encostam nas pontas.

    Silêncios no meio da faixa nunca são cortados (Decisão 7).
    """
    start, end = 0.0, decoded_duration
    if periods:
        first_start, first_end = periods[0]
        if first_start <= EDGE_TOLERANCE:
            if first_end is None or first_end >= decoded_duration - EDGE_TOLERANCE:
                raise ProcessingError("a faixa inteira está abaixo do limiar de silêncio")
            start = first_end
        last_start, last_end = periods[-1]
        if last_start > start and (last_end is None or last_end >= decoded_duration - EDGE_TOLERANCE):
            end = last_start
    if end - start < 1.0:
        raise ProcessingError(f"sobra menos de 1 s de áudio após o corte de silêncio ({start:.2f}–{end:.2f} s)")
    return start, end


def loudnorm_stats(stderr):
    """Extrai o bloco JSON que o loudnorm imprime no fim do stderr (print_format=json)."""
    begin = stderr.rfind("{")
    finish = stderr.find("}", begin)
    if begin == -1 or finish == -1:
        raise ProcessingError("loudnorm não imprimiu as medições")
    try:
        return json.loads(stderr[begin:finish + 1])
    except json.JSONDecodeError as exc:
        raise ProcessingError(f"medições do loudnorm ilegíveis: {exc}") from None


def output_name(track_id, source_sha256, audio_cfg):
    """{id}-{hash8}.mp3, onde o hash cobre o original, os parâmetros de áudio e a versão do pipeline."""
    key = json.dumps(
        {"source": source_sha256, "audio": audio_cfg, "pipeline": PIPELINE_VERSION}, sort_keys=True
    )
    return f"{track_id}-{hashlib.sha256(key.encode()).hexdigest()[:8]}.mp3"


FFMPEG = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats"]


def _loudnorm_filter(cfg, measured=None):
    base = f"loudnorm=I={cfg['target_lufs']}:TP={cfg['true_peak']}:LRA={cfg['loudness_range']}"
    if measured:
        base += (
            f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true"
        )
    return base + ":print_format=json"


def measure_loudness(path, cfg, pre_filter=""):
    """Primeira passagem do loudnorm (só mede). pre_filter é aplicado antes, ex.: o corte."""
    chain = (pre_filter + "," if pre_filter else "") + _loudnorm_filter(cfg)
    result = run(FFMPEG + ["-i", str(path), "-map", "0:a:0", "-af", chain, "-f", "null", "-"])
    stats = loudnorm_stats(result.stderr)
    if stats.get("input_i") in ("-inf", "inf", None):
        raise ProcessingError("volume não mensurável (áudio silencioso?)")
    return stats


@dataclass
class Processed:
    track_id: str
    file: Path
    duration: float
    skipped: bool


def process_track(track, originals_dir, processed_dir, cfg):
    """Processa uma faixa válida. Se a saída com o mesmo hash já existe, só confere e pula."""
    data = track.data
    out = processed_dir / output_name(data["id"], track.sha256, cfg)

    if out.is_file():
        info = probe(out)
        return Processed(data["id"], out, info["duration"], skipped=True)

    src = originals_dir / data["filename"]
    probe(src)  # integridade da entrada

    # 1. Silêncio nas pontas. -progress dá a duração realmente decodificada.
    result = run(FFMPEG + [
        "-i", str(src), "-map", "0:a:0",
        "-af", f"silencedetect=noise={cfg['silence_threshold_db']}dB:d={cfg['silence_min_seconds']}",
        "-progress", "pipe:1", "-f", "null", "-",
    ])
    times = OUT_TIME_RE.findall(result.stdout)
    if not times:
        raise ProcessingError("não foi possível obter a duração decodificada")
    decoded = int(times[-1]) / 1_000_000
    start, end = trim_range(silence_periods(result.stderr), decoded)
    trim = f"atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS"

    # 2. Medição do volume no trecho já cortado.
    measured = measure_loudness(src, cfg, pre_filter=trim)

    # 3. Aplicação + codificação. Grava em .part e só renomeia depois de verificar.
    processed_dir.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(".part")
    try:
        result = run(FFMPEG + [
            "-y", "-i", str(src), "-map", "0:a:0", "-map_metadata", "0",
            "-af", trim + "," + _loudnorm_filter(cfg, measured),
            "-ar", str(cfg["sample_rate"]), "-c:a", "libmp3lame", "-b:a", f"{cfg['bitrate_kbps']}k",
            "-id3v2_version", "3", "-f", "mp3", str(part),
        ])
        applied = loudnorm_stats(result.stderr)

        # 4. Verificação da saída.
        info = probe(part)
        if info["codec"] != "mp3" or info["sample_rate"] != cfg["sample_rate"]:
            raise ProcessingError(f"saída inesperada: {info['codec']} {info['sample_rate']} Hz")
        final = measure_loudness(part, cfg)
        part.replace(out)
    finally:
        part.unlink(missing_ok=True)

    mode = applied.get("normalization_type", "?")
    if mode != "linear":
        log.warning(
            "AVISO %s: loudnorm usou modo %s (o alvo não cabia no modo linear; a dinâmica foi comprimida)",
            data["id"], mode,
        )
    lufs = float(final["input_i"])
    if abs(lufs - cfg["target_lufs"]) > cfg["lufs_tolerance"]:
        log.warning(
            "AVISO %s: saída mede %.1f LUFS, fora de %.1f ± %.1f",
            data["id"], lufs, cfg["target_lufs"], cfg["lufs_tolerance"],
        )
    log.info(
        "processada %s → %s (%s, %.1f LUFS, pico %s dBTP, corte %.2f s início / %.2f s fim, %.1f s)",
        data["id"], out.name, mode, lufs, final["input_tp"], start, decoded - end, info["duration"],
    )
    return Processed(data["id"], out, info["duration"], skipped=False)


def process_all(tracks, originals_dir, processed_dir, cfg):
    """Processa as faixas ativas. Devolve (processadas, falhas) com falhas como Rejection."""
    done, failed = [], []
    for track in tracks:
        if track.data["status"] != "active":
            continue
        try:
            result = process_track(track, originals_dir, processed_dir, cfg)
        except ProcessingError as exc:
            failed.append(Rejection(track.line, track.data["id"], [f"processamento: {exc}"]))
            continue
        if result.skipped:
            log.info("já processada %s (%s)", result.track_id, result.file.name)
        done.append(result)
    return done, failed


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------

def setup_logging(log_dir):
    # Console do Windows fora do terminal nativo (Git Bash, redirecionamento) usa cp1252: força UTF-8
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    for old in list(log.handlers):  # main() pode rodar mais de uma vez no mesmo processo (testes)
        old.close()
        log.removeHandler(old)
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"ingest-{date.today():%Y%m%d}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(file_handler)
    log.addHandler(console)
    log.setLevel(logging.INFO)
    return log_file


def main(argv=None):
    parser = argparse.ArgumentParser(description="Valida e processa o catálogo da Rádio Academia.")
    parser.add_argument("--catalog", type=Path, default=ROOT / "catalog" / "tracks.csv")
    parser.add_argument("--originals", type=Path, default=ROOT / "media" / "originals")
    parser.add_argument("--processed", type=Path, default=ROOT / "media" / "processed")
    parser.add_argument("--config", type=Path, default=ROOT / "scripts" / "config.toml")
    parser.add_argument("--logs", type=Path, default=ROOT / "logs")
    parser.add_argument("--validate-only", action="store_true", help="só valida o CSV, sem processar áudio")
    args = parser.parse_args(argv)

    log_file = setup_logging(args.logs)
    log.info("ingest iniciado; catálogo %s", args.catalog)

    try:
        tracks = read_catalog(args.catalog)
        cfg = None if args.validate_only else load_config(args.config)
    except (CatalogError, ConfigError) as exc:
        log.error("%s", exc)
        return 2

    if not args.validate_only:
        missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
        if missing:
            log.error(
                "não encontrado no PATH: %s. Instale com: winget install --id Gyan.FFmpeg -e "
                "(depois abra um terminal novo)", ", ".join(missing),
            )
            return 2

    valid, rejected = validate(tracks, args.originals)
    for r in rejected:
        log.warning("REJEITADA %s", r.message())
    for t in valid:
        log.info("ok %s (%s)", t.data["id"], t.data["status"])

    processed, failed = [], []
    if not args.validate_only:
        processed, failed = process_all(valid, args.originals, args.processed, cfg)
        for r in failed:
            log.warning("FALHOU %s", r.message())

    log.info(
        "fim: %d faixas lidas, %d válidas, %d rejeitadas; %d processadas (%d novas), %d falhas. Log: %s",
        len(tracks), len(valid), len(rejected),
        len(processed), sum(not p.skipped for p in processed), len(failed), log_file,
    )
    return 1 if rejected or failed else 0


if __name__ == "__main__":
    sys.exit(main())
