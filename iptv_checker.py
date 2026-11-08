#!/usr/bin/env python3
"""IPTV M3U / Xtream playlist checker.

input.txt içindeki her adres için:
  1. Playlist adresi açılıyor mu?
  2. Playlist geçerli mi?
  3. İlk 500 satırdan seçilen 5 yayından en az 2'si oynuyor mu?

Sonuçlar durumlara göre ayrı dosyalara yazılır:
  saglikli.txt, zayif.txt, yayin_yok.txt, playlist_hatali.txt, ozet.txt

Panel ve kullanıcı bilgileri paneller/ klasörüne eklenir:
  Paneller.txt, Userpass.txt, AyniUserpass.txt, eslesmeler.txt
"""

import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlsplit

import requests

# Windows konsolunda emoji/Türkçe karakter sorununu önle.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

CONNECT_TIMEOUT = 5
PLAYLIST_READ_TIMEOUT = 10
STREAM_READ_TIMEOUT = 8

MAX_PLAYLIST_LINES = 500
MAX_PLAYLIST_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_PROBE_BYTES = 64 * 1024

SAMPLE_SIZE = 5
MIN_WORKING = 2

HEADERS = {
    "User-Agent": "VLC/3.0.0-git LibVLC/3.0.0-git",
    "Accept": "*/*",
}

STATUS_HEALTHY = "SAĞLIKLI"
STATUS_WEAK = "ZAYIF"
STATUS_NONE = "YAYIN YOK"
STATUS_BAD = "PLAYLIST HATALI"


@dataclass
class Channel:
    name: str
    url: str


@dataclass
class ChannelResult:
    channel: Channel
    ok: bool
    reason: str = ""


@dataclass
class Result:
    index: int
    url: str
    status: str
    source_type: str = "playlist"
    http_code: int | None = None
    available: int = 0
    checked: int = 0
    working: int = 0
    reason: str = ""
    elapsed: float = 0.0
    playlist_opened: bool = False
    channels: list[ChannelResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def status_label(result: Result) -> str:
    if result.status == STATUS_BAD:
        return STATUS_BAD
    return f"{result.status} ({result.working}/{result.checked})"


def parse_extinf_name(line: str) -> str:
    """#EXTINF satırında tırnak dışındaki ilk virgülden sonraki kanal adını alır."""
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            return line[i + 1:].strip()
    return ""


def is_html_response(data: bytes, content_type: str) -> bool:
    if "text/html" in content_type or "application/xhtml" in content_type:
        return True
    return data.lstrip()[:1] == b"<"


def looks_like_media(data: bytes, content_type: str) -> bool:
    """Gerçekten medya/binary veri mi geliyor, yoksa hata sayfası mı?"""
    if not data:
        return False
    if is_html_response(data, content_type):
        return False

    # MPEG-TS
    if data[0] == 0x47:
        return True

    # MP4 / fMP4
    if b"ftyp" in data[:32]:
        return True

    # Açıkça metin ise yayın değildir.
    if content_type.startswith(("text/", "application/json", "application/xml")):
        return False

    # Binary oranı yüksekse medya kabul et.
    sample = data[:2048]
    non_printable = sum(
        1 for b in sample
        if b < 9 or 13 < b < 32 or b > 126
    )
    return non_printable / len(sample) > 0.10


def read_input(path: Path) -> tuple[list[str], int, int]:
    """Boşları/# yorumlarını atlar, birebir duplicate'leri temizler."""
    seen: set[str] = set()
    urls: list[str] = []
    total = 0
    skipped = 0

    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            total += 1

            if not line.lower().startswith(("http://", "https://")):
                skipped += 1
                continue

            if line in seen:
                continue
            seen.add(line)
            urls.append(line)

    return urls, total, skipped


# ---------------------------------------------------------------------------
# Playlist okuma
# ---------------------------------------------------------------------------

def fetch_playlist(url: str) -> tuple[list[str], int | None, str]:
    """Playlist'in sadece ilk MAX_PLAYLIST_LINES satırını indirir."""
    try:
        with requests.get(
            url,
            headers=HEADERS,
            timeout=(CONNECT_TIMEOUT, PLAYLIST_READ_TIMEOUT),
            stream=True,
            allow_redirects=True,
        ) as response:
            code = response.status_code
            content_type = response.headers.get("Content-Type", "").lower()

            if code != 200:
                return [], code, f"HTTP {code}"

            lines: list[str] = []
            total_bytes = 0

            for raw in response.iter_lines(decode_unicode=False):
                if raw is None:
                    continue

                total_bytes += len(raw)
                if total_bytes > MAX_PLAYLIST_BYTES:
                    break

                line = raw.decode("utf-8", errors="replace").strip().lstrip("\ufeff")
                if not line:
                    continue

                lines.append(line)
                if len(lines) >= MAX_PLAYLIST_LINES:
                    break

            if not lines:
                return [], code, "Boş yanıt"

            if lines[0].startswith("#EXTM3U"):
                return lines, code, ""

            if "text/html" in content_type or lines[0].startswith("<"):
                return [], code, "HTML yanıtı (geçerli M3U değil)"

            return [], code, "M3U başlığı yok"

    except requests.Timeout:
        return [], None, "Zaman aşımı"
    except requests.RequestException as exc:
        return [], None, f"Bağlantı hatası ({type(exc).__name__})"
    except Exception as exc:  # beklenmeyen parse hataları
        return [], None, f"Hata ({type(exc).__name__})"


def parse_channels(lines: list[str]) -> list[Channel]:
    channels: list[Channel] = []
    pending: Channel | None = None

    for line in lines:
        if line.startswith("#EXTINF"):
            pending = Channel(name=parse_extinf_name(line), url="")
        elif line.startswith("#"):
            continue
        elif pending is not None:
            pending.url = line
            channels.append(pending)
            pending = None
        elif line.startswith(("http://", "https://")):
            channels.append(Channel(name="", url=line))

    return [c for c in channels if c.url]


def is_hls_source(url: str, lines: list[str]) -> bool:
    if urlparse(url).path.lower().endswith(".m3u8"):
        return True
    return any(line.startswith("#EXT-X-") for line in lines)


def select_channels(channels: list[Channel]) -> list[Channel]:
    """İlk 3 sırayla, kalan 2 ilk 500 satırlık havuzdan rastgele."""
    if len(channels) <= SAMPLE_SIZE:
        return channels

    selected = channels[:3]
    pool = channels[3:]
    selected.extend(random.sample(pool, SAMPLE_SIZE - 3))
    return selected


# ---------------------------------------------------------------------------
# Yayın kontrolü
# ---------------------------------------------------------------------------

def fetch_manifest_text(url: str) -> tuple[str, str]:
    try:
        with requests.get(
            url,
            headers=HEADERS,
            timeout=(CONNECT_TIMEOUT, STREAM_READ_TIMEOUT),
            stream=True,
            allow_redirects=True,
        ) as response:
            if response.status_code != 200:
                return "", f"HTTP {response.status_code}"

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_MANIFEST_BYTES:
                    break

            data = b"".join(chunks)
            if not data:
                return "", "Boş yanıt"

            return data.decode("utf-8", errors="replace"), ""

    except requests.Timeout:
        return "", "Zaman aşımı"
    except requests.RequestException as exc:
        return "", f"Bağlantı hatası ({type(exc).__name__})"


def probe_segment(url: str) -> tuple[bool, str]:
    try:
        with requests.get(
            url,
            headers=HEADERS,
            timeout=(CONNECT_TIMEOUT, STREAM_READ_TIMEOUT),
            stream=True,
            allow_redirects=True,
        ) as response:
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}"

            content_type = response.headers.get("Content-Type", "").lower()
            data = next(response.iter_content(MAX_PROBE_BYTES), b"")

            if not data:
                return False, "Veri gelmedi"
            if is_html_response(data, content_type):
                return False, "HTML yanıtı"
            if not looks_like_media(data, content_type):
                return False, "Medya verisi değil"

            return True, ""

    except requests.Timeout:
        return False, "Zaman aşımı"
    except requests.RequestException as exc:
        return False, f"Bağlantı hatası ({type(exc).__name__})"


def probe_hls_text(manifest: str, base_url: str, depth: int = 0) -> tuple[bool, str]:
    """HLS master/media manifestini doğrular; gerçek segment verisi ister."""
    if depth > 2:
        return False, "Manifest derinliği aşıldı"

    lines = [line.strip() for line in manifest.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        return False, "HLS manifesti geçersiz"

    # Master playlist: variant URL'lerini dene.
    variant_urls: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            for nxt in lines[i + 1:]:
                if not nxt.startswith("#"):
                    variant_urls.append(urljoin(base_url, nxt))
                    break

    if variant_urls:
        last_reason = "Variant açılmadı"
        for variant_url in variant_urls[:2]:
            text, reason = fetch_manifest_text(variant_url)
            if not text:
                last_reason = f"Variant: {reason}"
                continue
            ok, reason = probe_hls_text(text, variant_url, depth + 1)
            if ok:
                return True, ""
            last_reason = reason
        return False, last_reason

    # Media playlist: ilk segmentleri dene.
    segment_urls = [
        urljoin(base_url, line)
        for line in lines
        if not line.startswith("#")
    ]
    if not segment_urls:
        return False, "Manifestte segment yok"

    last_reason = "Segment açılmadı"
    for segment_url in segment_urls[:2]:
        ok, reason = probe_segment(segment_url)
        if ok:
            return True, ""
        last_reason = reason

    return False, last_reason


def probe_channel(url: str) -> tuple[bool, str]:
    try:
        with requests.get(
            url,
            headers=HEADERS,
            timeout=(CONNECT_TIMEOUT, STREAM_READ_TIMEOUT),
            stream=True,
            allow_redirects=True,
        ) as response:
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}"

            content_type = response.headers.get("Content-Type", "").lower()
            data = next(response.iter_content(MAX_PROBE_BYTES), b"")

            if not data:
                return False, "Veri gelmedi"

            if data.lstrip().startswith(b"#EXTM3U"):
                manifest = data.decode("utf-8", errors="replace")
                return probe_hls_text(manifest, url)

            if is_html_response(data, content_type):
                return False, "HTML yanıtı"
            if not looks_like_media(data, content_type):
                return False, "Medya verisi değil"

            return True, ""

    except requests.Timeout:
        return False, "Zaman aşımı"
    except requests.RequestException as exc:
        return False, f"Bağlantı hatası ({type(exc).__name__})"
    except Exception as exc:
        return False, f"Hata ({type(exc).__name__})"


# ---------------------------------------------------------------------------
# Tek kaynak kontrolü
# ---------------------------------------------------------------------------

def check_source(index: int, url: str) -> Result:
    started = time.monotonic()
    lines, code, error = fetch_playlist(url)

    if error:
        return Result(
            index=index,
            url=url,
            status=STATUS_BAD,
            http_code=code,
            reason=error,
            elapsed=time.monotonic() - started,
        )

    # Doğrudan HLS adresi: playlist değil, tek yayın olarak kontrol et.
    if is_hls_source(url, lines):
        ok, reason = probe_hls_text("\n".join(lines), url)
        channel = Channel(name="(doğrudan yayın)", url=url)
        return Result(
            index=index,
            url=url,
            status=STATUS_HEALTHY if ok else STATUS_NONE,
            source_type="single",
            http_code=code,
            available=1,
            checked=1,
            working=1 if ok else 0,
            reason=reason,
            elapsed=time.monotonic() - started,
            playlist_opened=True,
            channels=[ChannelResult(channel=channel, ok=ok, reason=reason)],
        )

    channels = parse_channels(lines)
    available = len(channels)
    if available == 0:
        return Result(
            index=index,
            url=url,
            status=STATUS_BAD,
            http_code=code,
            reason="M3U içinde yayın bulunamadı",
            elapsed=time.monotonic() - started,
            playlist_opened=True,
        )

    selected = select_channels(channels)
    channel_results: list[ChannelResult] = []

    with ThreadPoolExecutor(max_workers=min(SAMPLE_SIZE, len(selected))) as executor:
        futures = {executor.submit(probe_channel, ch.url): ch for ch in selected}
        for future in as_completed(futures):
            channel = futures[future]
            try:
                ok, reason = future.result()
            except Exception as exc:
                ok, reason = False, f"Hata ({type(exc).__name__})"
            channel_results.append(ChannelResult(channel=channel, ok=ok, reason=reason))

    # İlk 3 + rastgele 2 sırasını koru.
    order = {id(ch): i for i, ch in enumerate(selected)}
    channel_results.sort(key=lambda cr: order.get(id(cr.channel), 999))

    working = sum(1 for cr in channel_results if cr.ok)
    if working >= MIN_WORKING:
        status = STATUS_HEALTHY
    elif working == 1:
        status = STATUS_WEAK
    else:
        status = STATUS_NONE

    return Result(
        index=index,
        url=url,
        status=status,
        http_code=code,
        available=available,
        checked=len(channel_results),
        working=working,
        elapsed=time.monotonic() - started,
        playlist_opened=True,
        channels=channel_results,
    )


# ---------------------------------------------------------------------------
# Rapor
# ---------------------------------------------------------------------------

CATEGORY_FILES = {
    STATUS_HEALTHY: "saglikli.txt",
    STATUS_WEAK: "zayif.txt",
    STATUS_NONE: "yayin_yok.txt",
    STATUS_BAD: "playlist_hatali.txt",
}


def build_result_block(result: Result) -> list[str]:
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append(f"[{result.index}] {status_label(result)}")
    lines.append(f"URL: {result.url}")
    lines.append(f"Kaynak: {'Tek yayın' if result.source_type == 'single' else 'M3U playlist'}")
    if result.http_code is not None:
        lines.append(f"HTTP: {result.http_code}")
    if result.source_type == "playlist":
        lines.append(f"Taranan: ilk {MAX_PLAYLIST_LINES} satır")
        lines.append(f"Bulunan yayın: {result.available}")
    lines.append(f"Kontrol edilen: {result.checked}")
    lines.append(f"Çalışan: {result.working}")
    lines.append(f"Süre: {result.elapsed:.1f} sn")
    if result.reason:
        lines.append(f"Neden: {result.reason}")

    if result.channels:
        lines.append("Yayın kontrolleri:")
        for cr in result.channels:
            mark = "OK  " if cr.ok else "FAIL"
            name = cr.channel.name or "(isimsiz)"
            detail = f" - {cr.reason}" if cr.reason else ""
            lines.append(f"  [{mark}] {name}{detail}")
    lines.append("")
    return lines


def build_category_report(
    status: str,
    results: list[Result],
    input_path: Path,
) -> str:
    lines: list[str] = []
    lines.append(f"IPTV CHECKER - {status}")
    lines.append("=" * 70)
    lines.append(f"Girdi dosyası: {input_path}")
    lines.append(f"Adet: {len(results)}")
    lines.append("")
    lines.append("ADRESLER")
    if results:
        for result in results:
            lines.append(result.url)
    else:
        lines.append("(yok)")
    lines.append("")
    lines.append("DETAYLAR")

    for result in results:
        lines.extend(build_result_block(result))

    return "\n".join(lines).rstrip() + "\n"


def build_summary(
    results: list[Result],
    input_path: Path,
    total_lines: int,
    skipped: int,
    duplicates: int,
    elapsed: float,
    output_dir: Path,
) -> str:
    counts = {
        STATUS_HEALTHY: 0,
        STATUS_WEAK: 0,
        STATUS_NONE: 0,
        STATUS_BAD: 0,
    }
    for result in results:
        counts[result.status] += 1

    lines: list[str] = []
    lines.append("IPTV CHECKER - ÖZET")
    lines.append("=" * 70)
    lines.append(f"Girdi dosyası  : {input_path}")
    lines.append(f"Çıktı klasörü  : {output_dir}")
    lines.append(f"Toplam satır   : {total_lines}")
    lines.append(f"Tekrar (atıldı): {duplicates}")
    lines.append(f"Geçersiz satır : {skipped}")
    lines.append(f"Kontrol edilen : {len(results)}")
    lines.append(f"Toplam süre    : {elapsed:.1f} sn")
    lines.append("")
    lines.append("DURUMLAR")
    lines.append(f"SAĞLIKLI       : {counts[STATUS_HEALTHY]}")
    lines.append(f"ZAYIF          : {counts[STATUS_WEAK]}")
    lines.append(f"YAYIN YOK      : {counts[STATUS_NONE]}")
    lines.append(f"PLAYLIST HATALI: {counts[STATUS_BAD]}")
    lines.append("")
    lines.append("DOSYALAR")
    for status, filename in CATEGORY_FILES.items():
        lines.append(f"{filename:<22}: {counts[status]} adet")
    lines.append("")

    return "\n".join(lines)


def write_reports(
    output_dir: Path,
    results: list[Result],
    input_path: Path,
    total_lines: int,
    skipped: int,
    duplicates: int,
    elapsed: float,
) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)

    counts = {
        STATUS_HEALTHY: 0,
        STATUS_WEAK: 0,
        STATUS_NONE: 0,
        STATUS_BAD: 0,
    }
    for result in results:
        counts[result.status] += 1

    for status, filename in CATEGORY_FILES.items():
        group = [r for r in results if r.status == status]
        report = build_category_report(status, group, input_path)
        (output_dir / filename).write_text(report, encoding="utf-8")

    summary = build_summary(
        results,
        input_path,
        total_lines,
        skipped,
        duplicates,
        elapsed,
        output_dir,
    )
    (output_dir / "ozet.txt").write_text(summary, encoding="utf-8")

    return counts


# ---------------------------------------------------------------------------
# Panel / kullanıcı kayıtları
# ---------------------------------------------------------------------------

PANEL_DIR = Path("paneller")
PANEL_FILE = "Paneller.txt"
USERPASS_FILE = "Userpass.txt"
SAME_USERPASS_FILE = "AyniUserpass.txt"
RELATIONS_FILE = "eslesmeler.txt"


def read_file_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return [line.strip() for line in f if line.strip()]


def append_unique(
    path: Path,
    entries: list[str],
    existing: set[str],
) -> tuple[int, int]:
    """Mevcutları koruyarak sadece yeni satırları ekler."""
    unique: list[str] = []
    seen = set(existing)
    skipped = 0

    for entry in entries:
        if entry in seen:
            skipped += 1
            continue
        seen.add(entry)
        unique.append(entry)

    if not unique:
        return 0, skipped

    path.parent.mkdir(parents=True, exist_ok=True)
    needs_newline = False
    if path.is_file() and path.stat().st_size > 0:
        with path.open("rb") as f:
            f.seek(-1, 2)
            needs_newline = f.read(1) != b"\n"

    with path.open("a", encoding="utf-8", newline="\n") as f:
        if needs_newline:
            f.write("\n")
        f.write("\n".join(unique) + "\n")

    return len(unique), skipped


def extract_panel(url: str) -> str | None:
    """http://host:port/ biçiminde panel adresi üretir."""
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https"):
            return None
        if not parts.hostname:
            return None

        host = parts.hostname.lower()
        if ":" in host:  # IPv6
            host = f"[{host}]"

        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme.lower()}://{host}{port}/"
    except ValueError:
        return None


def extract_credentials(url: str) -> str | None:
    """URL'deki username/password değerlerini yazıldığı gibi korur."""
    try:
        query = urlsplit(url).query
    except ValueError:
        return None

    username = None
    password = None
    for part in query.split("&"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.lower()
        if key == "username":
            username = value
        elif key == "password":
            password = value

    if not username or not password:
        return None
    return f"{username}:{password}"


def read_relations(path: Path) -> list[tuple[str, str]]:
    relations: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    if not path.is_file():
        return relations

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if "\t" not in line:
                continue
            panel, credentials = line.split("\t", 1)
            panel = panel.strip()
            credentials = credentials.strip()
            pair = (panel, credentials)
            if panel and credentials and pair not in seen:
                seen.add(pair)
                relations.append(pair)

    return relations


def build_same_userpass(
    relations: list[tuple[str, str]],
) -> tuple[int, str]:
    groups: dict[str, list[str]] = {}
    for panel, credentials in relations:
        panels = groups.setdefault(credentials, [])
        if panel not in panels:
            panels.append(panel)

    lines = [
        "AYNI USERNAME:PASSWORD KULLANAN PANELLER",
        "=" * 60,
        "",
    ]
    group_count = 0
    for credentials, panels in groups.items():
        if len(panels) < 2:
            continue
        group_count += 1
        lines.append(credentials)
        for panel in panels:
            lines.append(f"  {panel}")
        lines.append("")

    if group_count == 0:
        lines.append("(kayıt yok)")
        lines.append("")

    return group_count, "\n".join(lines)


def save_panel_records(
    results: list[Result],
    panel_dir: Path = PANEL_DIR,
) -> dict[str, int]:
    panel_path = panel_dir / PANEL_FILE
    userpass_path = panel_dir / USERPASS_FILE
    same_path = panel_dir / SAME_USERPASS_FILE
    relations_path = panel_dir / RELATIONS_FILE

    existing_panels = set(read_file_lines(panel_path))
    existing_userpass = set(read_file_lines(userpass_path))
    existing_relations = read_relations(relations_path)
    existing_relation_keys = {
        f"{panel}\t{credentials}"
        for panel, credentials in existing_relations
    }

    new_panels: list[str] = []
    new_userpass: list[str] = []
    new_relations: list[tuple[str, str]] = []

    for result in results:
        credentials = extract_credentials(result.url)

        # Kullanıcı bilgisi her durumda kaydedilir (playlist hatalı olsa bile).
        if credentials:
            new_userpass.append(credentials)

        # Panel sadece URL açıldıysa (geçerli M3U döndüyse) kaydedilir.
        if result.playlist_opened:
            panel = extract_panel(result.url)
            if panel:
                new_panels.append(panel)
                if credentials:
                    new_relations.append((panel, credentials))

    added_panels, skipped_panels = append_unique(
        panel_path,
        new_panels,
        existing_panels,
    )
    added_userpass, skipped_userpass = append_unique(
        userpass_path,
        new_userpass,
        existing_userpass,
    )
    append_unique(
        relations_path,
        [f"{panel}\t{credentials}" for panel, credentials in new_relations],
        existing_relation_keys,
    )

    # AyniUserpass.txt her çalıştırmada ilişkilerden yeniden üretilir.
    merged_relations = list(existing_relations)
    seen_relations = set(merged_relations)
    for pair in new_relations:
        if pair not in seen_relations:
            seen_relations.add(pair)
            merged_relations.append(pair)

    group_count, same_text = build_same_userpass(merged_relations)
    panel_dir.mkdir(parents=True, exist_ok=True)
    with same_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(same_text)

    return {
        "panels_added": added_panels,
        "panels_skipped": skipped_panels,
        "userpass_added": added_userpass,
        "userpass_skipped": skipped_userpass,
        "same_groups": group_count,
    }


# ---------------------------------------------------------------------------
# Ana akış
# ---------------------------------------------------------------------------

def ask_parallelism() -> int:
    default = 5
    while True:
        try:
            raw = input(f"Aynı anda kontrol edilecek playlist sayısı (varsayılan {default}): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return default

        if not raw:
            return default

        try:
            value = int(raw)
        except ValueError:
            print("Lütfen bir sayı girin.")
            continue

        if 1 <= value <= 100:
            if value > 20:
                print("Not: Her playlist için 5 yayın da paralel kontrol edilir.")
            return value

        print("1 ile 100 arasında bir sayı girin.")


def main() -> None:
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("input.txt")
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("sonuclar")

    if not input_path.is_file():
        print(f"❌ Girdi dosyası bulunamadı: {input_path}")
        print("   Kullanım: python iptv_checker.py [input.txt] [sonuclar]")
        return

    urls, total_lines, skipped = read_input(input_path)
    duplicates = total_lines - skipped - len(urls)

    if not urls:
        print("❌ input.txt içinde kontrol edilecek http(s) adresi yok.")
        return

    print("🔍 IPTV Checker")
    print(f"📄 Girdi: {input_path}")
    print(f"🔗 Kontrol edilecek adres: {len(urls)}")
    if duplicates:
        print(f"♻️  Tekrar eden adres atıldı: {duplicates}")
    if skipped:
        print(f"⚠️  Geçersiz satır atıldı: {skipped}")
    print()

    parallelism = ask_parallelism()
    print(f"\n🚀 Başlıyor ({parallelism} playlist paralel)...\n")

    started = time.monotonic()
    results: list[Result | None] = [None] * len(urls)
    lock = threading.Lock()
    done = 0

    with ThreadPoolExecutor(max_workers=parallelism) as executor:
        futures = {
            executor.submit(check_source, i, url): i
            for i, url in enumerate(urls)
        }

        for future in as_completed(futures):
            index = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = Result(
                    index=index,
                    url=urls[index],
                    status=STATUS_BAD,
                    reason=f"Beklenmeyen hata ({type(exc).__name__})",
                )
            results[index] = result
            done += 1

            host = urlparse(result.url).netloc or result.url
            with lock:
                print(f"[{done:>4}/{len(urls)}] {status_label(result):<22} {host}")

    elapsed = time.monotonic() - started
    final_results = [r for r in results if r is not None]

    counts = write_reports(
        output_dir,
        final_results,
        input_path,
        total_lines,
        skipped,
        duplicates,
        elapsed,
    )
    try:
        panel_stats = save_panel_records(final_results)
    except OSError as exc:
        print(f"\n⚠️  Panel kayıtları yazılamadı: {exc}")
        panel_stats = {
            "panels_added": 0,
            "panels_skipped": 0,
            "userpass_added": 0,
            "userpass_skipped": 0,
            "same_groups": 0,
        }

    print("\n📊 Sonuç Özeti")
    print("═══════════════════════════════")
    print(f"📡 Kontrol edilen : {len(final_results)}")
    print(f"✅ Sağlıklı       : {counts[STATUS_HEALTHY]}")
    print(f"⚠️  Zayıf          : {counts[STATUS_WEAK]}")
    print(f"❌ Yayın yok      : {counts[STATUS_NONE]}")
    print(f"⛔ Playlist hatalı: {counts[STATUS_BAD]}")
    print(f"⏱️  Toplam süre    : {elapsed:.1f} sn")
    print(f"📁 Klasör         : {output_dir}")
    for status, filename in CATEGORY_FILES.items():
        print(f"   • {filename:<22} {counts[status]} adet")
    print(f"   • {'ozet.txt':<22} özet")
    print(f"📁 Panel klasörü  : {PANEL_DIR}")
    print(f"   • {PANEL_FILE:<22} {panel_stats['panels_added']} yeni, {panel_stats['panels_skipped']} mevcut")
    print(f"   • {USERPASS_FILE:<22} {panel_stats['userpass_added']} yeni, {panel_stats['userpass_skipped']} mevcut")
    print(f"   • {SAME_USERPASS_FILE:<22} {panel_stats['same_groups']} grup")
    print("═══════════════════════════════")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⏹️  Kullanıcı tarafından durduruldu.")
        sys.exit(1)
