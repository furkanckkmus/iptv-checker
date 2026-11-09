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

import os
import random
import shutil
import sys
import threading
import time
from collections import Counter, deque
from concurrent.futures import (
    FIRST_COMPLETED,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlsplit

import requests

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        Progress,
        TextColumn,
        TimeElapsedColumn,
    )
    from rich.table import Table

    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# Windows konsolunda emoji/Türkçe karakter sorununu önle.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

CONNECT_TIMEOUT = 20
PLAYLIST_READ_TIMEOUT = 20
STREAM_READ_TIMEOUT = 20

DEFAULT_PARALLELISM = 20
HOST_TASK_LIMIT = 4
HOST_HTTP_LIMIT = 6

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


class HostLimiter:
    """Aynı host:port için eşzamanlı HTTP isteği sayısını sınırlar."""

    def __init__(self, limit: int = HOST_HTTP_LIMIT) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}

    def _semaphore(self, host: str) -> threading.BoundedSemaphore:
        with self._lock:
            semaphore = self._semaphores.get(host)
            if semaphore is None:
                semaphore = threading.BoundedSemaphore(self._limit)
                self._semaphores[host] = semaphore
            return semaphore

    @contextmanager
    def limit(self, url: str):
        host = urlsplit(url).netloc.lower()
        semaphore = self._semaphore(host)
        with semaphore:
            yield


HOST_LIMITER = HostLimiter(HOST_HTTP_LIMIT)

_SHARED_ADAPTER = requests.adapters.HTTPAdapter(
    pool_connections=20,
    pool_maxsize=50,
    max_retries=0,
)
_SESSION_LOCAL = threading.local()


def get_session() -> requests.Session:
    session = getattr(_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        session.mount("http://", _SHARED_ADAPTER)
        session.mount("https://", _SHARED_ADAPTER)
        _SESSION_LOCAL.session = session
    return session


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
    timed_out: bool = False
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
        with HOST_LIMITER.limit(url):
            with get_session().get(
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
        with HOST_LIMITER.limit(url):
            with get_session().get(
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
        with HOST_LIMITER.limit(url):
            with get_session().get(
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
    manifest_text: str | None = None

    try:
        with HOST_LIMITER.limit(url):
            with get_session().get(
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
                    manifest_text = data.decode("utf-8", errors="replace")
                else:
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

    # HLS zinciri kendi isteklerini yapar; host kilidi burada bırakılmış olmalı.
    if manifest_text is not None:
        return probe_hls_text(manifest_text, url)

    return False, "Bilinmeyen yayın yanıtı"


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
            timed_out="Zaman aşımı" in error,
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
            timed_out="Zaman aşımı" in reason,
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
        timed_out=any("Zaman aşımı" in cr.reason for cr in channel_results),
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
    run_stamp: str,
) -> str:
    lines: list[str] = []
    lines.append(f"IPTV CHECKER - {status}")
    lines.append("=" * 70)
    lines.append(f"Tarama       : {run_stamp}")
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
    run_dir: Path,
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
    lines.append(f"Tarama         : {run_dir.name}")
    lines.append(f"Girdi dosyası  : {input_path}")
    lines.append(f"Çıktı klasörü  : {run_dir}")
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
        lines.append(f"{filename:<40}: {counts[status]} adet")
    lines.append(f"{'ozet.txt':<40}: özet")
    lines.append("")

    return "\n".join(lines)


def make_run_folder(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    base = datetime.now().strftime("%Y-%m-%d-%H-%M")
    folder = output_dir / base
    counter = 2
    while folder.exists():
        folder = output_dir / f"{base}_{counter}"
        counter += 1
    folder.mkdir(parents=True)
    return folder


def write_reports(
    output_dir: Path,
    results: list[Result],
    input_path: Path,
    total_lines: int,
    skipped: int,
    duplicates: int,
    elapsed: float,
) -> tuple[dict[str, int], Path]:
    run_dir = make_run_folder(output_dir)
    run_stamp = run_dir.name

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
        report = build_category_report(status, group, input_path, run_stamp)
        (run_dir / filename).write_text(report, encoding="utf-8")

    summary = build_summary(
        results,
        input_path,
        total_lines,
        skipped,
        duplicates,
        elapsed,
        run_dir,
    )
    (run_dir / "ozet.txt").write_text(summary, encoding="utf-8")

    return counts, run_dir


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
    default = DEFAULT_PARALLELISM
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


def enable_ansi() -> None:
    """Windows konsolunda ANSI kaçış dizilerini etkinleştirir."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def compute_speed_eta(
    done: int,
    total: int,
    elapsed: float,
    p90_done: int | None,
    p90_time: float | None,
    now: float,
) -> tuple[float, str, str]:
    """%90 sonrası gerçek hızı, ondan önce ortalama hızı kullanır."""
    remaining = max(0, total - done)

    if (
        p90_done is not None
        and p90_time is not None
        and done > p90_done
        and now > p90_time
    ):
        speed = (done - p90_done) / (now - p90_time)
        speed_label = "90%+"
    else:
        speed = done / elapsed if elapsed > 0 else 0.0
        speed_label = "ort."

    if remaining == 0:
        eta_text = "00:00:00"
    elif speed > 0:
        eta_text = format_duration(remaining / speed)
    else:
        eta_text = "--:--:--"

    return speed, eta_text, speed_label


def display_host(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or url


class RichDashboard:
    """rich ile canlı ilerleme, sayaçlar ve son çalışanlar paneli."""

    def __init__(self, total: int, parallelism: int, host_limit: int) -> None:
        self.total = total
        self.parallelism = parallelism
        self.host_limit = host_limit
        self.console = Console()
        self.progress = Progress(
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=None),
            TextColumn("[progress.percentage]{task.percentage:>5.1f}%"),
            TextColumn("({task.completed}/{task.total})"),
            TimeElapsedColumn(),
            console=self.console,
            expand=True,
        )
        self.task = self.progress.add_task("İlerleme", total=total)
        self.layout = Layout()
        self.layout.split_column(
            Layout(name="header", size=3),
            Layout(name="info", size=5),
            Layout(name="progress", size=3),
            Layout(name="stats", size=3),
            Layout(name="working", ratio=1),
        )
        self.layout["header"].update(
            Panel.fit("[bold cyan]📺 IPTV CHECKER[/]", border_style="cyan")
        )
        self.live = Live(
            self.layout,
            console=self.console,
            refresh_per_second=2,
            transient=False,
        )
        self.live.start(refresh=True)

    @staticmethod
    def _indices_text(indices: list[int], limit: int = 8) -> str:
        parts = [f"#{index + 1}" for index in indices[:limit]]
        if len(indices) > limit:
            parts.append(f"+{len(indices) - limit}")
        return ", ".join(parts) if parts else "-"

    def update(
        self,
        done: int,
        status_counts: Counter[str],
        timed_out_count: int,
        recent_working: deque[Result],
        current: list[int],
        elapsed: float,
        p90_done: int | None = None,
        p90_time: float | None = None,
        now: float | None = None,
    ) -> None:
        self.progress.update(self.task, completed=done)

        remaining = max(0, self.total - done)
        percent = (done / self.total * 100) if self.total else 0.0
        speed, eta_text, speed_label = compute_speed_eta(
            done,
            self.total,
            elapsed,
            p90_done,
            p90_time,
            now if now is not None else time.monotonic(),
        )

        info = Table.grid(expand=True)
        for _ in range(3):
            info.add_column(justify="left", ratio=1)
        info.add_row(
            f"📡 Toplam: [bold]{self.total}[/]",
            f"✅ Tamamlanan: [bold]{done}[/] (%{percent:.1f})",
            f"⏳ Kalan: [bold]{remaining}[/]",
        )
        info.add_row(
            f"🔄 Paralel: [bold]{self.parallelism}[/]",
            f"🏠 Host limiti: [bold]{self.host_limit}[/]",
            f"⏱️  Süre: [bold]{format_duration(elapsed)}[/]",
        )
        info.add_row(
            f"🚀 Hız ({speed_label}): [bold]{speed:.2f}[/] işlem/sn",
            f"🕒 Tahmini: [bold]{eta_text}[/]",
            f"🎯 Şu an: {self._indices_text(current)}",
        )

        stats = Table.grid(expand=True)
        for _ in range(3):
            stats.add_column(justify="center")
        stats.add_row(
            f"[green]✅ Sağlıklı: {status_counts[STATUS_HEALTHY]}[/]",
            f"[yellow]⚠️  Zayıf: {status_counts[STATUS_WEAK]}[/]",
            f"[red]❌ Yayın yok: {status_counts[STATUS_NONE]}[/]",
        )
        stats.add_row(
            f"[bold red]⛔ Hatalı: {status_counts[STATUS_BAD]}[/]",
            f"[magenta]⏳ Zaman aşımı: {timed_out_count}[/]",
            "",
        )

        working = Table(
            title="Son çalışanlar (10)",
            expand=True,
            border_style="green",
            show_lines=False,
        )
        working.add_column("Durum", width=6, justify="center")
        working.add_column("Host")
        working.add_column("Yayın", width=8, justify="right")

        if recent_working:
            for result in reversed(recent_working):
                mark = "✅" if result.status == STATUS_HEALTHY else "⚠️"
                working.add_row(
                    mark,
                    display_host(result.url),
                    f"{result.working}/{result.checked}",
                )
        else:
            working.add_row("", "(henüz çalışan yok)", "")

        self.layout["info"].update(info)
        self.layout["progress"].update(self.progress)
        self.layout["stats"].update(stats)
        self.layout["working"].update(working)
        self.live.update(self.layout, refresh=True)

    def stop(self) -> None:
        self.live.stop()


def build_progress_frame(
    total: int,
    completed: set[int],
    started: set[int],
    urls: list[str],
    order: list[int],
    parallelism: int,
    host_limit: int,
    elapsed: float,
    p90_done: int | None = None,
    p90_time: float | None = None,
    now: float | None = None,
) -> str:
    done = len(completed)
    remaining = max(0, total - done)
    percent = (done / total * 100) if total else 0.0

    current = sorted(started - completed)
    pending = [index for index in order if index not in started]

    bar_width = 32
    filled = int(bar_width * done / total) if total else bar_width
    bar = "█" * filled + "░" * (bar_width - filled)

    speed, eta_text, speed_label = compute_speed_eta(
        done,
        total,
        elapsed,
        p90_done,
        p90_time,
        now if now is not None else time.monotonic(),
    )

    def indices_text(indices: list[int], limit: int = 8) -> str:
        parts = [f"#{index + 1}" for index in indices[:limit]]
        if len(indices) > limit:
            parts.append(f"+{len(indices) - limit}")
        return ", ".join(parts) if parts else "-"

    try:
        width = max(60, min(100, shutil.get_terminal_size((80, 24)).columns))
    except OSError:
        width = 78

    lines = [
        "═" * width,
        "📺 IPTV CHECKER",
        "═" * width,
        f"📡 Toplam       : {total}",
        f"✅ Tamamlanan   : {done}  (%{percent:.1f})",
        f"⏳ Kalan        : {remaining}",
        f"🔄 Paralel      : {parallelism}",
        f"🏠 Host limiti  : {host_limit}",
        f"🎯 Şu an        : {indices_text(current)}",
        f"⏱️  Geçen süre   : {format_duration(elapsed)}",
        f"🚀 Hız ({speed_label}) : {speed:.2f} işlem/sn",
        f"🕒 Tahmini süre : {eta_text}",
        f"[{bar}] %{percent:.1f}",
        "─" * width,
        "Sıradaki 10:",
    ]

    if pending:
        for index in pending[:10]:
            lines.append(
                f"  [{index + 1:>5}/{total}] {display_host(urls[index])}"
            )
    else:
        lines.append("  (sırada bekleyen yok)")

    lines.append("─" * width)
    return "\n".join(lines)


def draw_progress(frame: str) -> None:
    sys.stdout.write("\033[H" + frame + "\033[J")
    sys.stdout.flush()


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
    live = sys.stdout.isatty()
    use_rich = live and HAS_RICH

    if live and not use_rich:
        enable_ansi()
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    elif not live:
        print(f"\n🚀 Başlıyor ({parallelism} playlist paralel)...\n")

    started = time.monotonic()
    results: list[Result | None] = [None] * len(urls)
    state_lock = threading.Lock()
    started_indices: set[int] = set()
    completed_indices: set[int] = set()
    render_stop = threading.Event()
    done = 0
    status_counts: Counter[str] = Counter()
    timed_out_count = 0
    recent_working: deque[Result] = deque(maxlen=10)
    dashboard: RichDashboard | None = None
    p90_done: int | None = None
    p90_time: float | None = None

    order = list(range(len(urls)))
    random.shuffle(order)
    host_of = {
        index: (urlparse(urls[index]).netloc.lower() or urls[index])
        for index in range(len(urls))
    }

    def check_one(index: int) -> Result:
        with state_lock:
            started_indices.add(index)
        return check_source(index, urls[index])

    def render_loop() -> None:
        local_p90_done: int | None = None
        local_p90_time: float | None = None
        while not render_stop.wait(0.5):
            with state_lock:
                started_snapshot = set(started_indices)
                completed_snapshot = set(completed_indices)
            now = time.monotonic()
            done_snapshot = len(completed_snapshot)
            if (
                local_p90_time is None
                and len(urls)
                and done_snapshot / len(urls) >= 0.90
            ):
                local_p90_time = now
                local_p90_done = done_snapshot
            frame = build_progress_frame(
                len(urls),
                completed_snapshot,
                started_snapshot,
                urls,
                order,
                parallelism,
                HOST_TASK_LIMIT,
                now - started,
                local_p90_done,
                local_p90_time,
                now,
            )
            draw_progress(frame)

    render_thread = threading.Thread(target=render_loop, daemon=True)

    try:
        if use_rich:
            dashboard = RichDashboard(len(urls), parallelism, HOST_TASK_LIMIT)
            dashboard.update(
                0,
                status_counts,
                0,
                recent_working,
                [],
                0.0,
                None,
                None,
                time.monotonic(),
            )
        elif live:
            render_thread.start()
            draw_progress(
                build_progress_frame(
                    len(urls),
                    set(),
                    set(),
                    urls,
                    order,
                    parallelism,
                    HOST_TASK_LIMIT,
                    0.0,
                    None,
                    None,
                    time.monotonic(),
                )
            )

        with ThreadPoolExecutor(max_workers=parallelism) as executor:
            pending = deque(order)
            running = {}
            host_counts: Counter[str] = Counter()

            def fill_slots() -> int:
                submitted = 0
                examined = 0
                initial_len = len(pending)
                blocked = deque()

                while (
                    pending
                    and len(running) < parallelism
                    and examined < initial_len
                ):
                    index = pending.popleft()
                    examined += 1
                    host = host_of[index]

                    if host_counts[host] >= HOST_TASK_LIMIT:
                        blocked.append(index)
                        continue

                    future = executor.submit(check_one, index)
                    running[future] = index
                    host_counts[host] += 1
                    submitted += 1

                pending.extend(blocked)
                return submitted

            while pending or running:
                fill_slots()
                if not running:
                    break

                finished, _ = wait(
                    running,
                    timeout=0.5,
                    return_when=FIRST_COMPLETED,
                )
                for future in finished:
                    index = running.pop(future)
                    host = host_of[index]
                    host_counts[host] -= 1

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
                    status_counts[result.status] += 1
                    if result.timed_out:
                        timed_out_count += 1
                    if result.status in (STATUS_HEALTHY, STATUS_WEAK):
                        recent_working.append(result)

                    with state_lock:
                        completed_indices.add(index)

                    if not live:
                        host_name = display_host(result.url)
                        print(
                            f"[{done:>4}/{len(urls)}] "
                            f"{status_label(result):<22} {host_name}"
                        )

                if dashboard is not None:
                    with state_lock:
                        current_snapshot = sorted(
                            started_indices - completed_indices
                        )
                    now = time.monotonic()
                    if (
                        p90_time is None
                        and len(urls)
                        and done / len(urls) >= 0.90
                    ):
                        p90_time = now
                        p90_done = done
                    dashboard.update(
                        done,
                        status_counts,
                        timed_out_count,
                        recent_working,
                        current_snapshot,
                        now - started,
                        p90_done,
                        p90_time,
                        now,
                    )
    finally:
        render_stop.set()
        if live and not use_rich:
            render_thread.join(timeout=2)
        if dashboard is not None:
            with state_lock:
                current_snapshot = sorted(
                    started_indices - completed_indices
                )
            now = time.monotonic()
            if (
                p90_time is None
                and len(urls)
                and done / len(urls) >= 0.90
            ):
                p90_time = now
                p90_done = done
            dashboard.update(
                done,
                status_counts,
                timed_out_count,
                recent_working,
                current_snapshot,
                now - started,
                p90_done,
                p90_time,
                now,
            )
            dashboard.stop()

    if live and not use_rich:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()

    elapsed = time.monotonic() - started
    final_results = [r for r in results if r is not None]

    counts, run_dir = write_reports(
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
    print(f"📁 Klasör         : {run_dir}")
    print(f"🕒 Tarama         : {run_dir.name}")
    for status, filename in CATEGORY_FILES.items():
        print(f"   • {filename:<22} {counts[status]} adet")
    print(f"   • {'ozet.txt':<22} özet")
    print(f"📁 Panel klasörü  : {PANEL_DIR}")
    print(f"   • {PANEL_FILE:<22} {panel_stats['panels_added']} yeni, {panel_stats['panels_skipped']} tekrar")
    print(f"   • {USERPASS_FILE:<22} {panel_stats['userpass_added']} yeni, {panel_stats['userpass_skipped']} tekrar")
    print(f"   • {SAME_USERPASS_FILE:<22} {panel_stats['same_groups']} grup")
    print("═══════════════════════════════")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⏹️  Kullanıcı tarafından durduruldu.")
        sys.exit(1)
