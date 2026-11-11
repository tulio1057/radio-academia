"""Ingest do catálogo da Rádio Academia.

Fase 1: só validação de catalog/tracks.csv.
Faixa inválida é registrada no log e pulada; o ingest nunca aborta por causa de uma faixa.
Só aborta (código 2) quando o próprio CSV não pode ser lido (arquivo ausente ou cabeçalho errado).

Uso:
    python scripts/ingest.py [--catalog CAMINHO] [--originals PASTA]

Códigos de saída: 0 = tudo válido, 1 = alguma faixa rejeitada, 2 = catálogo ilegível.
"""

import argparse
import csv
import hashlib
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent

FIELDS = [
    "id", "title", "artist", "album", "genre", "license", "license_url",
    "source_url", "attribution", "filename", "duration", "status", "date_added",
]
OPTIONAL = {"album", "duration"}  # duration é preenchida pelo ingest via ffprobe (Fase 2)
ACCEPTED_LICENSES = {"CC0-1.0", "CC-BY-3.0", "CC-BY-4.0"}
STATUSES = {"active", "inactive"}
ID_RE = re.compile(r"[a-z0-9-]+")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

log = logging.getLogger("ingest")


class CatalogError(Exception):
    """Catálogo inteiro ilegível: arquivo ausente ou cabeçalho diferente do esperado."""


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
    parser = argparse.ArgumentParser(description="Valida o catálogo da Rádio Academia.")
    parser.add_argument("--catalog", type=Path, default=ROOT / "catalog" / "tracks.csv")
    parser.add_argument("--originals", type=Path, default=ROOT / "media" / "originals")
    parser.add_argument("--logs", type=Path, default=ROOT / "logs")
    args = parser.parse_args(argv)

    log_file = setup_logging(args.logs)
    log.info("ingest iniciado; catálogo %s", args.catalog)

    try:
        tracks = read_catalog(args.catalog)
    except CatalogError as exc:
        log.error("%s", exc)
        return 2

    valid, rejected = validate(tracks, args.originals)
    for r in rejected:
        log.warning("REJEITADA %s", r.message())
    for t in valid:
        log.info("ok %s (%s)", t.data["id"], t.data["status"])

    log.info(
        "fim: %d faixas lidas, %d válidas, %d rejeitadas. Log: %s",
        len(tracks), len(valid), len(rejected), log_file,
    )
    return 1 if rejected else 0


if __name__ == "__main__":
    sys.exit(main())
