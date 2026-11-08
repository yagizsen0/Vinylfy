"""
Vinylfy — Desktop Spotify Vinyl "Now Playing" Visualizer (PyQt6)
================================================================
Displays the currently playing Spotify track on a realistic spinning vinyl
record with dominant-color dynamic crossfading backgrounds, blurred cover art,
first-run local API credential setup, and fullscreen mode.
"""

from __future__ import annotations

import colorsys
import json
import os
import sys
import threading
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from typing import TypedDict

import requests
from PIL import Image, ImageEnhance, ImageFilter
from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QThread,
    QTimer,
    QUrl,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QConicalGradient,
    QCursor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QKeyEvent,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QResizeEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "Vinylfy"
APP_VERSION = "1.0.0"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"
SPOTIFY_DASHBOARD_URL = "https://developer.spotify.com/dashboard"
SPOTIFY_SCOPE = "user-read-currently-playing user-read-playback-state"
POLL_INTERVAL_SECONDS = 2.0

RGBColor = tuple[int, int, int]


# ============================================================================
# PATHS & PERSISTENT LOCAL CONFIGURATION
# ============================================================================
def resource_path(relative_path: str) -> Path:
    """
    Resolve bundled asset paths both when running from source (`python main.py`)
    and when frozen into an executable with PyInstaller (`sys._MEIPASS`).
    """
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_dir / relative_path


def get_user_data_dir() -> Path:
    """
    Return a writable per-user data directory (`%APPDATA%/Vinylfy` on Windows).
    Ensures config and OAuth tokens work reliably even when installed in Program Files.
    """
    if sys.platform == "win32":
        root = Path(os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config"))

    data_dir = root / APP_NAME
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


CONFIG_FILE = get_user_data_dir() / "config.json"
TOKEN_CACHE_FILE = get_user_data_dir() / ".spotify_token_cache"


@dataclass
class AppConfig:
    """Stores Spotify API credentials persisted locally on the user's machine."""

    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = DEFAULT_REDIRECT_URI

    def is_valid(self) -> bool:
        return bool(self.client_id.strip() and self.client_secret.strip())

    @classmethod
    def load(cls) -> AppConfig:
        if CONFIG_FILE.exists():
            try:
                raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                return cls(
                    client_id=str(raw.get("client_id", "")).strip(),
                    client_secret=str(raw.get("client_secret", "")).strip(),
                    redirect_uri=str(raw.get("redirect_uri") or DEFAULT_REDIRECT_URI).strip(),
                )
            except (OSError, json.JSONDecodeError):
                pass

        # Fallback to environment variables if present
        return cls(
            client_id=os.getenv("SPOTIPY_CLIENT_ID", "").strip(),
            client_secret=os.getenv("SPOTIPY_CLIENT_SECRET", "").strip(),
            redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip(),
        )

    def save(self) -> None:
        self.client_id = self.client_id.strip()
        self.client_secret = self.client_secret.strip()
        self.redirect_uri = self.redirect_uri.strip() or DEFAULT_REDIRECT_URI
        CONFIG_FILE.write_text(
            json.dumps(asdict(self), indent=2),
            encoding="utf-8",
        )


def load_app_icon() -> QIcon:
    """Load Vinylfy's application icon (.ico preferred on Windows, .png fallback)."""
    ico_path = resource_path("assets/icon.ico")
    png_path = resource_path("assets/logo.png")
    if ico_path.exists():
        return QIcon(str(ico_path))
    if png_path.exists():
        return QIcon(str(png_path))
    return QIcon()


class TrackInfo(TypedDict, total=False):
    """Payload emitted from SpotifyWorker to the UI thread."""

    is_active: bool
    is_playing: bool
    track_id: str
    title: str
    artist: str
    album: str
    cover_url: str
    progress_ms: int
    duration_ms: int


# ============================================================================
# IMAGE PROCESSING & DOMINANT COLOR EXTRACTION (RUNS IN WORKER THREAD)
# ============================================================================
def _clamp_hsv_color(
    rgb: RGBColor,
    min_v: float,
    max_v: float,
    sat_boost: float = 1.0,
) -> RGBColor:
    """Adjust saturation and brightness in HSV space for readable UI contrast."""
    h, s, v = colorsys.rgb_to_hsv(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
    s = min(1.0, s * sat_boost)
    v = max(min_v, min(max_v, v))
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def _color_distance(c1: RGBColor, c2: RGBColor) -> float:
    return ((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2) ** 0.5


def _pil_to_qimage(pil_image: Image.Image) -> QImage:
    """
    Convert a PIL Image into a standalone thread-safe QImage.
    Note: QPixmap must only be created on the main GUI thread in Qt6, so the
    worker thread emits QImage objects with `.copy()` owning their buffer.
    """
    rgba = pil_image.convert("RGBA")
    width, height = rgba.size
    raw_bytes = rgba.tobytes("raw", "RGBA")
    return QImage(raw_bytes, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()


def analyze_artwork(image_bytes: bytes) -> tuple[QImage, QImage, list[RGBColor]]:
    """
    Process downloaded cover artwork into:
    1) High-res square-cropped cover QImage for the spinning vinyl label
    2) Gaussian-blurred atmospheric backdrop QImage
    3) 3 dominant palette colors [bg_top, bg_bottom, accent] via Median Cut
    """
    img = Image.open(BytesIO(image_bytes)).convert("RGB")

    # Center-crop non-square thumbnails (e.g., podcast or video episodes)
    w, h = img.size
    if w != h:
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))

    # 1. Crisp cover image for the vinyl mask
    cover_pil = img.resize((640, 640), Image.Resampling.LANCZOS)
    cover_qimage = _pil_to_qimage(cover_pil)

    # 2. Blurred, color-boosted background texture
    blur_base = img.resize((420, 420), Image.Resampling.BILINEAR)
    blur_base = ImageEnhance.Color(blur_base).enhance(1.30)
    blur_base = ImageEnhance.Brightness(blur_base).enhance(0.58)
    blurred_pil = blur_base.filter(ImageFilter.GaussianBlur(radius=34))
    blurred_qimage = _pil_to_qimage(blurred_pil)

    # 3. Dominant palette extraction using Median-Cut quantization
    sample = img.resize((100, 100), Image.Resampling.BILINEAR)
    quantized = sample.quantize(colors=8, method=Image.Quantize.MEDIANCUT).convert("RGB")
    raw_clusters = quantized.getcolors(maxcolors=256) or [(10000, (24, 42, 32))]

    scored_colors: list[tuple[float, RGBColor]] = []
    for count, rgb in raw_clusters:
        _, s, v = colorsys.rgb_to_hsv(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
        saturation_weight = 0.35 + 1.5 * s
        midtone_weight = 0.35 + 0.85 * (1.0 - abs(v - 0.55))
        score = count * saturation_weight * midtone_weight
        scored_colors.append((score, rgb))

    scored_colors.sort(key=lambda item: item[0], reverse=True)
    primary_raw = scored_colors[0][1]

    secondary_raw = primary_raw
    for _, candidate in scored_colors[1:]:
        if _color_distance(primary_raw, candidate) > 48:
            secondary_raw = candidate
            break
    else:
        h, s, v = colorsys.rgb_to_hsv(
            primary_raw[0] / 255.0,
            primary_raw[1] / 255.0,
            primary_raw[2] / 255.0,
        )
        r, g, b = colorsys.hsv_to_rgb((h + 0.08) % 1.0, s * 0.85, max(0.12, v * 0.65))
        secondary_raw = (int(r * 255), int(g * 255), int(b * 255))

    vibrant_raw = max(
        (rgb for _, rgb in scored_colors),
        key=lambda c: colorsys.rgb_to_hsv(c[0] / 255.0, c[1] / 255.0, c[2] / 255.0)[1]
        * 0.7
        + colorsys.rgb_to_hsv(c[0] / 255.0, c[1] / 255.0, c[2] / 255.0)[2] * 0.3,
    )

    bg_top = _clamp_hsv_color(primary_raw, min_v=0.20, max_v=0.46, sat_boost=1.15)
    bg_bottom = _clamp_hsv_color(secondary_raw, min_v=0.10, max_v=0.28, sat_boost=1.10)
    accent = _clamp_hsv_color(vibrant_raw, min_v=0.78, max_v=0.96, sat_boost=1.20)

    return cover_qimage, blurred_qimage, [bg_top, bg_bottom, accent]


# ============================================================================
# FIRST-RUN & API CREDENTIALS SETTINGS DIALOG
# ============================================================================
class SettingsDialog(QDialog):
    """
    Modern dark-themed onboarding & settings dialog where users enter and save
    their Spotify Client ID, Client Secret, and Redirect URI locally.
    """

    def __init__(
        self,
        config: AppConfig,
        first_run: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._first_run = first_run

        self.setWindowTitle(f"{APP_NAME} — Setup" if first_run else f"{APP_NAME} — Settings")
        self.setWindowIcon(load_app_icon())
        self.setModal(True)
        self.setFixedWidth(490)
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            """
            QDialog {
                background-color: #101316;
                color: #F3F5F7;
            }
            QFrame#Card {
                background-color: #171B20;
                border: 1px solid #262D35;
                border-radius: 12px;
            }
            QLabel {
                color: #E8ECEF;
                background: transparent;
            }
            QLineEdit {
                background-color: #0E1114;
                color: #FFFFFF;
                border: 1px solid #2B333C;
                border-radius: 8px;
                padding: 9px 12px;
                font-size: 13px;
                selection-background-color: #1ED760;
                selection-color: #000000;
            }
            QLineEdit:focus {
                border: 1px solid #1ED760;
            }
            QPushButton#PrimaryBtn {
                background-color: #1ED760;
                color: #0A0D0E;
                border: none;
                border-radius: 9px;
                padding: 11px 20px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#PrimaryBtn:hover {
                background-color: #3BE377;
            }
            QPushButton#SecondaryBtn {
                background-color: #1E242B;
                color: #DCE2E8;
                border: 1px solid #2E3742;
                border-radius: 8px;
                padding: 8px 13px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton#SecondaryBtn:hover {
                background-color: #28303A;
                border-color: #3D4957;
            }
            QPushButton#LinkBtn {
                background: transparent;
                color: #1ED760;
                border: none;
                font-size: 12px;
                font-weight: 600;
                text-align: left;
                padding: 0px;
            }
            QPushButton#LinkBtn:hover {
                color: #52EB88;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(14)

        # Logo & Brand Header
        logo_path = resource_path("assets/logo.png")
        if logo_path.exists():
            logo_lbl = QLabel()
            pix = QPixmap(str(logo_path)).scaled(
                72,
                72,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            logo_lbl.setPixmap(pix)
            logo_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(logo_lbl)

        heading = QLabel(f"Welcome to {APP_NAME}" if self._first_run else f"{APP_NAME} API Settings")
        heading.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)

        subtitle = QLabel(
            "Enter your Spotify Developer credentials below. They are saved locally on "
            "this computer so you only need to set this up once."
        )
        subtitle.setFont(QFont("Segoe UI", 9))
        subtitle.setStyleSheet("color: #9AA5B1;")
        subtitle.setWordWrap(True)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        # Quick Guide Card
        guide_card = QFrame()
        guide_card.setObjectName("Card")
        guide_layout = QVBoxLayout(guide_card)
        guide_layout.setContentsMargins(14, 12, 14, 12)
        guide_layout.setSpacing(6)

        steps_lbl = QLabel(
            "1. Open Spotify Developer Dashboard and click <b>Create app</b>.<br>"
            "2. Enable <b>Web API</b> and add <code>http://127.0.0.1:8888/callback</code> as Redirect URI.<br>"
            "3. Copy your <b>Client ID</b> and <b>Client Secret</b> from App Settings."
        )
        steps_lbl.setFont(QFont("Segoe UI", 9))
        steps_lbl.setStyleSheet("color: #C3CCD5;")
        steps_lbl.setWordWrap(True)
        steps_lbl.setMinimumHeight(62)
        guide_layout.addWidget(steps_lbl)

        dash_btn = QPushButton("Open Spotify Developer Dashboard ↗")
        dash_btn.setObjectName("LinkBtn")
        dash_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        dash_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(SPOTIFY_DASHBOARD_URL)))
        guide_layout.addWidget(dash_btn)

        layout.addWidget(guide_card)

        # Client ID
        cid_lbl = QLabel("Spotify Client ID")
        cid_lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        layout.addWidget(cid_lbl)

        self._client_id_input = QLineEdit(self._config.client_id)
        self._client_id_input.setPlaceholderText("e.g. 4f8b2c9e1a0d47c89b12e34f56a78b90")
        layout.addWidget(self._client_id_input)

        # Client Secret + Show/Hide Toggle
        sec_lbl = QLabel("Spotify Client Secret")
        sec_lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        layout.addWidget(sec_lbl)

        sec_row = QHBoxLayout()
        sec_row.setSpacing(8)
        self._client_secret_input = QLineEdit(self._config.client_secret)
        self._client_secret_input.setPlaceholderText("Paste your Client Secret")
        self._client_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        sec_row.addWidget(self._client_secret_input, stretch=1)

        self._toggle_secret_btn = QPushButton("Show")
        self._toggle_secret_btn.setObjectName("SecondaryBtn")
        self._toggle_secret_btn.setFixedWidth(64)
        self._toggle_secret_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._toggle_secret_btn.clicked.connect(self._toggle_secret_visibility)
        sec_row.addWidget(self._toggle_secret_btn)
        layout.addLayout(sec_row)

        # Redirect URI + Copy Button
        uri_lbl = QLabel("Redirect URI")
        uri_lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        layout.addWidget(uri_lbl)

        uri_row = QHBoxLayout()
        uri_row.setSpacing(8)
        self._redirect_input = QLineEdit(self._config.redirect_uri or DEFAULT_REDIRECT_URI)
        uri_row.addWidget(self._redirect_input, stretch=1)

        self._copy_uri_btn = QPushButton("Copy")
        self._copy_uri_btn.setObjectName("SecondaryBtn")
        self._copy_uri_btn.setFixedWidth(64)
        self._copy_uri_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._copy_uri_btn.clicked.connect(self._copy_redirect_uri)
        uri_row.addWidget(self._copy_uri_btn)
        layout.addLayout(uri_row)

        # Validation Error Label
        self._error_lbl = QLabel("")
        self._error_lbl.setFont(QFont("Segoe UI", 9, QFont.Weight.Medium))
        self._error_lbl.setStyleSheet("color: #FF6B6B;")
        self._error_lbl.setWordWrap(True)
        self._error_lbl.hide()
        layout.addWidget(self._error_lbl)

        layout.addSpacing(4)

        # Action Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        if not self._first_run:
            cancel_btn = QPushButton("Cancel")
            cancel_btn.setObjectName("SecondaryBtn")
            cancel_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            cancel_btn.clicked.connect(self.reject)
            btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("Save && Launch Vinylfy" if self._first_run else "Save && Reconnect")
        save_btn.setObjectName("PrimaryBtn")
        save_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn, stretch=1)

        layout.addLayout(btn_row)

    def _toggle_secret_visibility(self) -> None:
        if self._client_secret_input.echoMode() == QLineEdit.EchoMode.Password:
            self._client_secret_input.setEchoMode(QLineEdit.EchoMode.Normal)
            self._toggle_secret_btn.setText("Hide")
        else:
            self._client_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
            self._toggle_secret_btn.setText("Show")

    def _copy_redirect_uri(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._redirect_input.text().strip() or DEFAULT_REDIRECT_URI)
        self._copy_uri_btn.setText("Copied!")
        QTimer.singleShot(1400, lambda: self._copy_uri_btn.setText("Copy"))

    def _on_save(self) -> None:
        client_id = self._client_id_input.text().strip()
        client_secret = self._client_secret_input.text().strip()
        redirect_uri = self._redirect_input.text().strip() or DEFAULT_REDIRECT_URI

        if not client_id or not client_secret:
            self._error_lbl.setText("Please enter both your Spotify Client ID and Client Secret.")
            self._error_lbl.show()
            return

        if len(client_id) < 16 or len(client_secret) < 16:
            self._error_lbl.setText(
                "The Client ID or Client Secret looks too short. Please verify your keys."
            )
            self._error_lbl.show()
            return

        # Clear stale token cache if credentials changed
        credentials_changed = (
            client_id != self._config.client_id
            or client_secret != self._config.client_secret
            or redirect_uri != self._config.redirect_uri
        )
        if credentials_changed and TOKEN_CACHE_FILE.exists():
            try:
                TOKEN_CACHE_FILE.unlink()
            except OSError:
                pass

        self._config.client_id = client_id
        self._config.client_secret = client_secret
        self._config.redirect_uri = redirect_uri
        self._config.save()
        self.accept()

    def get_config(self) -> AppConfig:
        return self._config


# ============================================================================
# ASYNCHRONOUS SPOTIFY WORKER THREAD
# ============================================================================
class SpotifyWorker(QThread):
    """
    Polls active Spotify playback in a background thread so the GUI never freezes,
    and downloads/analyzes cover art whenever the track artwork changes.
    """

    track_updated = pyqtSignal(dict)
    artwork_ready = pyqtSignal(QImage, QImage, list)
    status_message = pyqtSignal(str)

    def __init__(
        self,
        config: AppConfig,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        super().__init__()
        self._config = config
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._last_cover_url: str | None = None
        self._http = requests.Session()

    def stop(self) -> None:
        """Signal the worker loop to exit immediately and wait cleanly."""
        self._stop_event.set()
        self.wait(3000)

    def run(self) -> None:
        if not self._config.is_valid():
            self.status_message.emit("API credentials missing • Click Settings in the top-right corner")
            return

        try:
            import spotipy
            from spotipy.oauth2 import SpotifyOAuth
        except ImportError:
            self.status_message.emit(
                "Missing dependency • Run 'pip install -r requirements.txt'"
            )
            return

        try:
            self.status_message.emit("Authenticating with Spotify...")
            auth_manager = SpotifyOAuth(
                client_id=self._config.client_id,
                client_secret=self._config.client_secret,
                redirect_uri=self._config.redirect_uri,
                scope=SPOTIFY_SCOPE,
                cache_path=str(TOKEN_CACHE_FILE),
                open_browser=True,
            )
            sp = spotipy.Spotify(auth_manager=auth_manager, requests_timeout=8)
        except Exception as exc:
            self.status_message.emit(f"Spotify authentication error: {exc}")
            return

        while not self._stop_event.is_set():
            try:
                playback = sp.current_playback(additional_types="track,episode")
                if playback and playback.get("item"):
                    item = playback["item"]
                    is_playing = bool(playback.get("is_playing", False))
                    title = item.get("name") or "Unknown Track"

                    if "artists" in item:
                        artist = ", ".join(a["name"] for a in item.get("artists", []))
                        album_obj = item.get("album") or {}
                        album = album_obj.get("name", "")
                        images = album_obj.get("images") or []
                    else:
                        show_obj = item.get("show") or {}
                        artist = show_obj.get("publisher") or show_obj.get("name", "Podcast")
                        album = show_obj.get("name", "")
                        images = item.get("images") or show_obj.get("images") or []

                    cover_url = images[0]["url"] if images else ""

                    info: TrackInfo = {
                        "is_active": True,
                        "is_playing": is_playing,
                        "track_id": item.get("id") or title,
                        "title": title,
                        "artist": artist or "Unknown Artist",
                        "album": album,
                        "cover_url": cover_url,
                        "progress_ms": int(playback.get("progress_ms") or 0),
                        "duration_ms": int(item.get("duration_ms") or 0),
                    }
                    self.track_updated.emit(dict(info))

                    if cover_url and cover_url != self._last_cover_url:
                        resp = self._http.get(cover_url, timeout=8)
                        resp.raise_for_status()
                        cover_img, blurred_img, palette = analyze_artwork(resp.content)
                        self._last_cover_url = cover_url
                        self.artwork_ready.emit(cover_img, blurred_img, palette)
                else:
                    self.track_updated.emit({"is_active": False, "is_playing": False})

            except requests.RequestException:
                self.status_message.emit("Reconnecting to Spotify...")
            except Exception as exc:
                self.status_message.emit(f"Spotify API notice • Check Settings ({exc})")

            self._stop_event.wait(self._poll_interval)


# ============================================================================
# CUSTOM UI WIDGETS (STATUS BADGE, ELIDED LABEL, PROGRESS BAR, VINYL DISC)
# ============================================================================
class StatusBadge(QWidget):
    """Antialiased capsule pill showing live playback or connection status."""

    def __init__(self, text: str = "CONNECTING", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self._dot_color = QColor(30, 215, 96)
        font = QFont("Segoe UI", 9, QFont.Weight.DemiBold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.1)
        self.setFont(font)
        self.setFixedHeight(28)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._recalc_width()

    def _recalc_width(self) -> None:
        fm = QFontMetrics(self.font())
        self.setFixedWidth(fm.horizontalAdvance(self._text) + 42)

    def set_status(self, text: str, color: QColor) -> None:
        self._text = text
        self._dot_color = color
        self._recalc_width()
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        rect = QRectF(0.75, 0.75, self.width() - 1.5, self.height() - 1.5)
        radius = rect.height() / 2.0

        painter.setBrush(QColor(8, 10, 16, 110))
        border_color = QColor(
            self._dot_color.red(),
            self._dot_color.green(),
            self._dot_color.blue(),
            95,
        )
        painter.setPen(QPen(border_color, 1.1))
        painter.drawRoundedRect(rect, radius, radius)

        dot_center = QPointF(15.0, self.height() / 2.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._dot_color)
        painter.drawEllipse(dot_center, 3.8, 3.8)

        painter.setFont(self.font())
        painter.setPen(QColor(255, 255, 255, 232))
        text_rect = QRectF(25.0, 0.0, self.width() - 36.0, float(self.height()))
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self._text,
        )


class ElidedLabel(QWidget):
    """Single-line centered label that gracefully elides long titles with '...'."""

    def __init__(
        self,
        text: str = "",
        font: QFont | None = None,
        color: QColor | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._text = text
        self._color = color or QColor(255, 255, 255)
        if font is not None:
            self.setFont(font)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        fm = QFontMetrics(self.font())
        self.setFixedHeight(fm.height() + 6)

    def setText(self, text: str) -> None:
        if self._text != text:
            self._text = text
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setFont(self.font())
        painter.setPen(self._color)
        fm = QFontMetrics(self.font())
        elided = fm.elidedText(self._text, Qt.TextElideMode.ElideRight, max(40, self.width() - 16))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, elided)


class TrackProgressBar(QWidget):
    """Minimalist progress bar tinted dynamically with the album's accent color."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._progress_ms = 0
        self._duration_ms = 0
        self._accent = QColor(30, 215, 96)
        self.setFixedHeight(26)
        self.setMaximumWidth(520)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_accent_color(self, rgb: RGBColor) -> None:
        self._accent = QColor(*rgb)
        self.update()

    def set_progress(self, progress_ms: int, duration_ms: int) -> None:
        self._progress_ms = max(0, min(progress_ms, duration_ms))
        self._duration_ms = max(0, duration_ms)
        self.update()

    @staticmethod
    def _fmt_time(ms: int) -> str:
        total_sec = max(0, ms // 1000)
        minutes, seconds = divmod(total_sec, 60)
        return f"{minutes:02d}:{seconds:02d}"

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        font = QFont("Segoe UI", 9, QFont.Weight.Medium)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255, 165))

        left_txt = self._fmt_time(self._progress_ms)
        right_txt = self._fmt_time(self._duration_ms)
        label_w = 52
        h = self.height()

        painter.drawText(
            QRectF(0, 0, label_w, h),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            left_txt,
        )
        painter.drawText(
            QRectF(self.width() - label_w, 0, label_w, h),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            right_txt,
        )

        bar_x = float(label_w + 8)
        bar_w = max(20.0, float(self.width() - 2 * (label_w + 8)))
        bar_h = 4.0
        bar_y = (h - bar_h) / 2.0

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 45))
        painter.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2.0, 2.0)

        ratio = (self._progress_ms / self._duration_ms) if self._duration_ms > 0 else 0.0
        fill_w = bar_w * max(0.0, min(1.0, ratio))
        if fill_w > 1.0:
            painter.setBrush(self._accent)
            painter.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2.0, 2.0)


class VinylWidget(QWidget):
    """
    Renders the album artwork masked onto a grooved vinyl record and rotates it
    smoothly with QPropertyAnimation.
    - When paused, the animation pauses in place without snapping back to 0°.
    - Before a track plays, Vinylfy's emblem is shown on the turntable platter.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._angle: float = 0.0
        self._cover_image: QImage | None = None
        self._standby_logo: QImage | None = None
        logo_file = resource_path("assets/logo.png")
        if logo_file.exists():
            self._standby_logo = QImage(str(logo_file))

        self._accent_color = QColor(30, 215, 96)
        self._disc_cache: QPixmap | None = None
        self._cached_diameter: int = 0

        self.setMinimumSize(240, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._spin_anim = QPropertyAnimation(self, b"angle", self)
        self._spin_anim.setStartValue(0.0)
        self._spin_anim.setEndValue(360.0)
        self._spin_anim.setDuration(14000)  # 14 seconds per full revolution
        self._spin_anim.setLoopCount(-1)    # Infinite loop
        self._spin_anim.setEasingCurve(QEasingCurve.Type.Linear)

    def _get_angle(self) -> float:
        return self._angle

    def _set_angle(self, value: float) -> None:
        self._angle = value
        self.update()

    angle = pyqtProperty(float, fget=_get_angle, fset=_set_angle)

    def set_playing(self, is_playing: bool) -> None:
        state = self._spin_anim.state()
        if is_playing:
            if state == QAbstractAnimation.State.Paused:
                self._spin_anim.resume()
            elif state == QAbstractAnimation.State.Stopped:
                self._spin_anim.start()
        else:
            if state == QAbstractAnimation.State.Running:
                self._spin_anim.pause()

    def set_artwork(self, cover_image: QImage, accent_rgb: RGBColor) -> None:
        self._cover_image = cover_image
        self._accent_color = QColor(*accent_rgb)
        self._disc_cache = None
        self.update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        new_diameter = self._current_diameter()
        if abs(new_diameter - self._cached_diameter) >= 2:
            self._disc_cache = None

    def _current_diameter(self) -> int:
        raw = min(self.width(), self.height()) - 36
        return max(180, min(620, raw))

    def _build_disc_pixmap(self, diameter: int) -> QPixmap:
        dpr = self.devicePixelRatioF()
        px_size = max(1, int(round(diameter * dpr)))
        pixmap = QPixmap(px_size, px_size)
        pixmap.fill(Qt.GlobalColor.transparent)
        pixmap.setDevicePixelRatio(dpr)

        center = QPointF(diameter / 2.0, diameter / 2.0)
        outer_r = diameter / 2.0 - 1.5
        cover_r = outer_r * 0.72
        collar_r = outer_r * 0.095
        hole_r = outer_r * 0.048

        painter = QPainter(pixmap)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform,
            True,
        )

        # Outer disc minus center spindle hole (transparent center hole)
        vinyl_ring = QPainterPath()
        vinyl_ring.setFillRule(Qt.FillRule.OddEvenFill)
        vinyl_ring.addEllipse(center, outer_r, outer_r)
        vinyl_ring.addEllipse(center, hole_r, hole_r)
        painter.setClipPath(vinyl_ring)

        if self._cover_image is None and self._standby_logo is not None and not self._standby_logo.isNull():
            # Render Vinylfy's signature green vinyl logo when in standby mode
            disc_rect = QRectF(
                center.x() - outer_r,
                center.y() - outer_r,
                outer_r * 2.0,
                outer_r * 2.0,
            )
            painter.drawImage(disc_rect, self._standby_logo)
            painter.end()
            return pixmap

        # 1. Matte charcoal vinyl body
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(17, 18, 23))
        painter.drawEllipse(center, outer_r, outer_r)

        # 2. Concentric vinyl grooves
        groove_steps = 18
        for i in range(groove_steps):
            t = i / max(1, groove_steps - 1)
            r = cover_r + (outer_r * 0.975 - cover_r) * t
            alpha = 18 if i % 2 == 0 else 9
            color = QColor(255, 255, 255, alpha) if i % 3 != 0 else QColor(0, 0, 0, 120)
            painter.setPen(QPen(color, 0.9))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, r, r)

        # 3. Circular masked album cover
        cover_clip = QPainterPath()
        cover_clip.setFillRule(Qt.FillRule.OddEvenFill)
        cover_clip.addEllipse(center, cover_r, cover_r)
        cover_clip.addEllipse(center, hole_r, hole_r)

        painter.save()
        painter.setClipPath(cover_clip)
        target_rect = QRectF(
            center.x() - cover_r,
            center.y() - cover_r,
            cover_r * 2.0,
            cover_r * 2.0,
        )
        if self._cover_image is not None and not self._cover_image.isNull():
            painter.drawImage(target_rect, self._cover_image)
        else:
            grad = QRadialGradient(center, cover_r)
            grad.setColorAt(0.0, QColor(28, 62, 42))
            grad.setColorAt(1.0, QColor(14, 22, 18))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(grad)
            painter.drawEllipse(center, cover_r, cover_r)
        painter.restore()

        # 4. Label rim & center hub collar
        painter.setClipPath(vinyl_ring)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(0, 0, 0, 160), 2.0))
        painter.drawEllipse(center, cover_r, cover_r)
        painter.setPen(QPen(QColor(255, 255, 255, 32), 1.0))
        painter.drawEllipse(center, cover_r - 1.0, cover_r - 1.0)

        painter.setPen(QPen(QColor(255, 255, 255, 48), 1.1))
        painter.setBrush(QColor(18, 19, 24, 225))
        painter.drawEllipse(center, collar_r, collar_r)

        painter.setPen(QPen(QColor(255, 255, 255, 70), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, hole_r + 0.6, hole_r + 0.6)

        painter.end()
        return pixmap

    def paintEvent(self, _event) -> None:
        diameter = self._current_diameter()
        if self._disc_cache is None or self._cached_diameter != diameter:
            self._disc_cache = self._build_disc_pixmap(diameter)
            self._cached_diameter = diameter

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        center = QPointF(cx, cy)
        outer_r = diameter / 2.0 - 1.5
        hole_r = outer_r * 0.048

        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform,
            True,
        )

        # 1. Stationary ambient drop shadow
        shadow_center = QPointF(cx, cy + diameter * 0.035)
        shadow_r = outer_r * 1.08
        shadow_grad = QRadialGradient(shadow_center, shadow_r)
        shadow_grad.setColorAt(0.0, QColor(0, 0, 0, 165))
        shadow_grad.setColorAt(0.78, QColor(0, 0, 0, 115))
        shadow_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow_grad)
        painter.drawEllipse(shadow_center, shadow_r, shadow_r)

        # 2. Rotating vinyl + cover layer
        painter.save()
        painter.translate(center)
        painter.rotate(self._angle)
        painter.drawPixmap(QPointF(-diameter / 2.0, -diameter / 2.0), self._disc_cache)
        painter.restore()

        # 3. Stationary conical specular reflection
        sheen_clip = QPainterPath()
        sheen_clip.setFillRule(Qt.FillRule.OddEvenFill)
        sheen_clip.addEllipse(center, outer_r, outer_r)
        sheen_clip.addEllipse(center, hole_r, hole_r)

        painter.save()
        painter.setClipPath(sheen_clip)
        sheen = QConicalGradient(center, 38.0)
        sheen.setColorAt(0.00, QColor(255, 255, 255, 26))
        sheen.setColorAt(0.12, QColor(255, 255, 255, 0))
        sheen.setColorAt(0.38, QColor(255, 255, 255, 0))
        sheen.setColorAt(0.50, QColor(255, 255, 255, 24))
        sheen.setColorAt(0.62, QColor(255, 255, 255, 0))
        sheen.setColorAt(0.88, QColor(255, 255, 255, 0))
        sheen.setColorAt(1.00, QColor(255, 255, 255, 26))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(sheen)
        painter.drawEllipse(center, outer_r, outer_r)
        painter.restore()

        # 4. Subtle outer rim highlight
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 40), 1.2))
        painter.drawEllipse(center, outer_r, outer_r)


# ============================================================================
# MAIN PLAYER WINDOW: DYNAMIC CROSSFADING BACKGROUND & FULLSCREEN CONTROL
# ============================================================================
class NowPlayingWindow(QWidget):
    """
    Main Vinylfy player window:
    - Blends Gaussian-blurred cover art with dominant-color diagonal gradients.
    - Smoothly crossfades backgrounds whenever the album cover changes.
    - Toggles fullscreen via F11 or double-clicking anywhere on the window.
    """

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(load_app_icon())
        self.resize(920, 730)
        self.setMinimumSize(480, 560)

        # Default emerald-dark Vinylfy palette until a track loads
        self._palette: list[RGBColor] = [(20, 44, 30), (10, 16, 14), (30, 215, 96)]
        self._blurred_bg: QImage | None = None
        self._prev_bg_pixmap: QPixmap | None = None
        self._curr_bg_pixmap: QPixmap | None = None
        self._transition_progress: float = 1.0

        self._bg_anim = QPropertyAnimation(self, b"transitionProgress", self)
        self._bg_anim.setDuration(950)
        self._bg_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

        self._is_playing = False
        self._progress_ms = 0
        self._duration_ms = 0
        self._ui_tick = QTimer(self)
        self._ui_tick.setInterval(250)
        self._ui_tick.timeout.connect(self._advance_local_progress)
        self._ui_tick.start()

        self._worker: SpotifyWorker | None = None

        self._setup_ui()
        self._rebuild_current_background()
        self._start_worker()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(36, 24, 36, 22)
        root.setSpacing(10)

        # Top Bar: Brand mark (left) | Status capsule (center) | Settings button (right)
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)

        brand_wrap = QWidget(self)
        brand_wrap.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        brand_wrap.setFixedWidth(120)
        brand_layout = QHBoxLayout(brand_wrap)
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(8)

        logo_path = resource_path("assets/logo.png")
        if logo_path.exists():
            brand_icon = QLabel()
            brand_icon.setPixmap(
                QPixmap(str(logo_path)).scaled(
                    24,
                    24,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            brand_layout.addWidget(brand_icon)

        brand_lbl = QLabel(APP_NAME)
        brand_lbl.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        brand_lbl.setStyleSheet("color: rgba(255, 255, 255, 225);")
        brand_layout.addWidget(brand_lbl)
        brand_layout.addStretch(1)

        top_row.addWidget(brand_wrap)
        top_row.addStretch(1)

        self._status_badge = StatusBadge("CONNECTING", self)
        top_row.addWidget(self._status_badge)
        top_row.addStretch(1)

        right_wrap = QWidget(self)
        right_wrap.setFixedWidth(120)
        right_layout = QHBoxLayout(right_wrap)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addStretch(1)

        self._settings_btn = QPushButton("⚙")
        self._settings_btn.setToolTip("Spotify API Settings")
        self._settings_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._settings_btn.setFixedSize(32, 32)
        self._settings_btn.setFont(QFont("Segoe UI Symbol", 13))
        self._settings_btn.setStyleSheet(
            """
            QPushButton {
                color: rgba(255, 255, 255, 185);
                background-color: rgba(0, 0, 0, 75);
                border: 1px solid rgba(255, 255, 255, 35);
                border-radius: 16px;
            }
            QPushButton:hover {
                color: #FFFFFF;
                background-color: rgba(255, 255, 255, 32);
                border-color: rgba(255, 255, 255, 90);
            }
            """
        )
        self._settings_btn.clicked.connect(self.open_settings)
        right_layout.addWidget(self._settings_btn)

        top_row.addWidget(right_wrap)
        root.addLayout(top_row)

        root.addSpacing(4)

        # Center Spinning Vinyl
        self._vinyl = VinylWidget(self)
        root.addWidget(self._vinyl, stretch=1)

        root.addSpacing(8)

        # Track Metadata Labels
        title_font = QFont("Segoe UI", 22, QFont.Weight.Bold)
        artist_font = QFont("Segoe UI", 14, QFont.Weight.Medium)
        album_font = QFont("Segoe UI", 10, QFont.Weight.Normal)

        self._title_label = ElidedLabel(
            "Play Something on Spotify",
            font=title_font,
            color=QColor(255, 255, 255, 245),
            parent=self,
        )
        self._artist_label = ElidedLabel(
            "Waiting for active playback...",
            font=artist_font,
            color=QColor(255, 255, 255, 190),
            parent=self,
        )
        self._album_label = ElidedLabel(
            "",
            font=album_font,
            color=QColor(255, 255, 255, 130),
            parent=self,
        )

        root.addWidget(self._title_label)
        root.addWidget(self._artist_label)
        root.addWidget(self._album_label)

        # Centered Progress Bar
        progress_row = QHBoxLayout()
        progress_row.addStretch(1)
        self._progress_bar = TrackProgressBar(self)
        progress_row.addWidget(self._progress_bar, stretch=4)
        progress_row.addStretch(1)
        root.addLayout(progress_row)

        # Fullscreen Hint
        self._hint_label = QLabel("Fullscreen: Press F11 or double-click anywhere")
        self._hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint_label.setFont(QFont("Segoe UI", 8))
        self._hint_label.setStyleSheet("color: rgba(255, 255, 255, 85);")
        self._hint_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        root.addWidget(self._hint_label)

    def _start_worker(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._worker.deleteLater()

        self._worker = SpotifyWorker(config=self._config)
        self._worker.track_updated.connect(self._on_track_updated)
        self._worker.artwork_ready.connect(self._on_artwork_ready)
        self._worker.status_message.connect(self._on_status_message)
        self._worker.start()

    def open_settings(self) -> None:
        """Open the API credentials dialog and reconnect if saved."""
        dialog = SettingsDialog(config=self._config, first_run=False, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._config = dialog.get_config()
            self._update_badge("RECONNECTING", QColor(30, 215, 96))
            self._artist_label.setText("Reconnecting to Spotify...")
            self._start_worker()

    # --- Dynamic Background Composition & Crossfade ---
    def _get_transition_progress(self) -> float:
        return self._transition_progress

    def _set_transition_progress(self, value: float) -> None:
        self._transition_progress = value
        self.update()

    transitionProgress = pyqtProperty(
        float,
        fget=_get_transition_progress,
        fset=_set_transition_progress,
    )

    def _compose_background_pixmap(
        self,
        blurred_img: QImage | None,
        palette: list[RGBColor],
    ) -> QPixmap:
        dpr = self.devicePixelRatioF()
        w = max(1, self.width())
        h = max(1, self.height())
        canvas = QPixmap(max(1, int(round(w * dpr))), max(1, int(round(h * dpr))))
        canvas.setDevicePixelRatio(dpr)

        painter = QPainter(canvas)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform,
            True,
        )
        rect = QRectF(0, 0, w, h)

        top_rgb, bottom_rgb, accent_rgb = palette[0], palette[1], palette[2]

        base_grad = QLinearGradient(0, 0, w, h)
        base_grad.setColorAt(0.0, QColor(*top_rgb))
        base_grad.setColorAt(0.55, QColor(*bottom_rgb))
        base_grad.setColorAt(
            1.0,
            QColor(
                max(8, bottom_rgb[0] // 2),
                max(10, bottom_rgb[1] // 2),
                max(14, bottom_rgb[2] // 2),
            ),
        )
        painter.fillRect(rect, base_grad)

        if blurred_img is not None and not blurred_img.isNull():
            painter.save()
            painter.setOpacity(0.58)
            img_w = blurred_img.width()
            img_h = blurred_img.height()
            scale = max(w / img_w, h / img_h) * 1.08
            draw_w = img_w * scale
            draw_h = img_h * scale
            target = QRectF((w - draw_w) / 2.0, (h - draw_h) / 2.0, draw_w, draw_h)
            painter.drawImage(target, blurred_img)
            painter.restore()

            tint_grad = QLinearGradient(0, 0, w, h)
            tint_grad.setColorAt(0.0, QColor(top_rgb[0], top_rgb[1], top_rgb[2], 125))
            tint_grad.setColorAt(1.0, QColor(bottom_rgb[0], bottom_rgb[1], bottom_rgb[2], 175))
            painter.fillRect(rect, tint_grad)

        glow_center = QPointF(w / 2.0, h * 0.42)
        glow_radius = min(w, h) * 0.52
        glow = QRadialGradient(glow_center, glow_radius)
        glow.setColorAt(0.0, QColor(accent_rgb[0], accent_rgb[1], accent_rgb[2], 42))
        glow.setColorAt(1.0, QColor(accent_rgb[0], accent_rgb[1], accent_rgb[2], 0))
        painter.fillRect(rect, glow)

        vignette = QRadialGradient(QPointF(w / 2.0, h * 0.43), max(w, h) * 0.72)
        vignette.setColorAt(0.0, QColor(0, 0, 0, 0))
        vignette.setColorAt(0.70, QColor(5, 7, 12, 70))
        vignette.setColorAt(1.0, QColor(5, 7, 12, 165))
        painter.fillRect(rect, vignette)

        painter.end()
        return canvas

    def _snapshot_blended_background(self) -> QPixmap:
        if (
            self._prev_bg_pixmap is None
            or self._curr_bg_pixmap is None
            or self._transition_progress >= 0.999
        ):
            return self._curr_bg_pixmap or self._compose_background_pixmap(
                self._blurred_bg, self._palette
            )

        dpr = self.devicePixelRatioF()
        blended = QPixmap(
            max(1, int(round(self.width() * dpr))),
            max(1, int(round(self.height() * dpr))),
        )
        blended.setDevicePixelRatio(dpr)
        painter = QPainter(blended)
        painter.drawPixmap(0, 0, self._prev_bg_pixmap)
        painter.setOpacity(self._transition_progress)
        painter.drawPixmap(0, 0, self._curr_bg_pixmap)
        painter.end()
        return blended

    def _rebuild_current_background(self) -> None:
        self._curr_bg_pixmap = self._compose_background_pixmap(self._blurred_bg, self._palette)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        if self._curr_bg_pixmap is None:
            self._rebuild_current_background()

        if self._transition_progress < 0.999 and self._prev_bg_pixmap is not None:
            painter.drawPixmap(0, 0, self._prev_bg_pixmap)
            painter.setOpacity(self._transition_progress)
            painter.drawPixmap(0, 0, self._curr_bg_pixmap)
        elif self._curr_bg_pixmap is not None:
            painter.drawPixmap(0, 0, self._curr_bg_pixmap)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._curr_bg_pixmap = self._compose_background_pixmap(self._blurred_bg, self._palette)
        if self._prev_bg_pixmap is not None and self._transition_progress < 0.999:
            self._prev_bg_pixmap = self._prev_bg_pixmap.scaled(
                self._curr_bg_pixmap.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._prev_bg_pixmap.setDevicePixelRatio(self.devicePixelRatioF())

    # --- Worker Signal Handlers ---
    def _on_track_updated(self, info: dict) -> None:
        if not info.get("is_active", False):
            self._is_playing = False
            self._vinyl.set_playing(False)
            self._title_label.setText("Nothing Playing Right Now")
            self._artist_label.setText("Start playing a track on Spotify to spin the vinyl")
            self._album_label.setText("")
            self._progress_ms = 0
            self._duration_ms = 0
            self._progress_bar.set_progress(0, 0)
            self._update_badge("STANDBY", QColor(180, 186, 200))
            return

        self._is_playing = bool(info.get("is_playing", False))
        self._vinyl.set_playing(self._is_playing)

        self._title_label.setText(info.get("title", ""))
        self._artist_label.setText(info.get("artist", ""))
        self._album_label.setText(info.get("album", ""))

        self._progress_ms = int(info.get("progress_ms", 0))
        self._duration_ms = int(info.get("duration_ms", 0))
        self._progress_bar.set_progress(self._progress_ms, self._duration_ms)

        accent = QColor(*self._palette[2])
        if self._is_playing:
            self._update_badge("NOW PLAYING", accent)
        else:
            self._update_badge("PAUSED", QColor(235, 195, 95))

    def _on_artwork_ready(
        self,
        cover_img: QImage,
        blurred_img: QImage,
        palette: list[RGBColor],
    ) -> None:
        self._prev_bg_pixmap = self._snapshot_blended_background()
        self._blurred_bg = blurred_img
        self._palette = palette
        self._rebuild_current_background()

        accent_rgb = palette[2]
        self._vinyl.set_artwork(cover_img, accent_rgb)
        self._progress_bar.set_accent_color(accent_rgb)
        if self._is_playing:
            self._update_badge("NOW PLAYING", QColor(*accent_rgb))

        self._bg_anim.stop()
        self._bg_anim.setStartValue(0.0)
        self._bg_anim.setEndValue(1.0)
        self._bg_anim.start()

    def _on_status_message(self, message: str) -> None:
        self._artist_label.setText(message)
        self._update_badge("STATUS", QColor(240, 175, 90))

    def _update_badge(self, text: str, dot_color: QColor) -> None:
        self._status_badge.set_status(text, dot_color)

    def _advance_local_progress(self) -> None:
        if self._is_playing and self._duration_ms > 0:
            self._progress_ms = min(self._duration_ms, self._progress_ms + 250)
            self._progress_bar.set_progress(self._progress_ms, self._duration_ms)

    # --- Fullscreen Controls (F11, Double-Click & ESC) ---
    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_F11:
            self.toggle_fullscreen()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.showNormal()
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_fullscreen()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def closeEvent(self, event) -> None:
        self._ui_tick.stop()
        if self._worker is not None:
            self._worker.stop()
        super().closeEvent(event)


def main() -> None:
    # Ensure Windows taskbar displays Vinylfy's custom icon instead of python.exe's icon
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Vinylfy.Desktop.Player.1"
            )
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(load_app_icon())

    # Load locally saved credentials; show onboarding setup dialog on first launch
    config = AppConfig.load()
    if not config.is_valid():
        setup_dialog = SettingsDialog(config=config, first_run=True)
        if setup_dialog.exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)
        config = setup_dialog.get_config()

    window = NowPlayingWindow(config=config)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
