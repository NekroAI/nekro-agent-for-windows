import os
import re
import sys

from PyQt6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QParallelAnimationGroup,
    Qt,
    QTimer,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PyQt6.QtWidgets import QGraphicsOpacityEffect, QWidget


def _resource_path(relative):
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


_IMG_W, _IMG_H = 680, 372
_STATUS_H = 36
_BAR_H = 4
_RADIUS = 18
_ACCENT = QColor("#e88478")
_ACCENT_LIGHT = QColor("#f0a89e")
_BG = QColor("#fff7f6")

_PHASE_BRAND = "brand"
_PHASE_DEPLOY = "deploy"
_PHASE_DONE = "done"

_BRAND_DURATION_MS = 1500


class SplashScreen(QWidget):
    finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.SplashScreen
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        total_h = _IMG_H + _STATUS_H + _BAR_H
        self.setFixedSize(_IMG_W + 40, total_h + 40)

        self._pixmap = QPixmap(_resource_path(os.path.join("assets", "splash.png")))
        self._phase = _PHASE_BRAND

        self._scale = 0.95
        self._shimmer_x = -0.15
        self._text_opacity = 0.0
        self._progress = 0.0
        self._target_progress = 0.0
        self._status_text = "正在准备..."
        self._bar_visible = False

        self._deploy_stage = "init"
        self._pull_total = 0
        self._pull_done = 0

        self._opacity_fx = QGraphicsOpacityEffect(self)
        self._opacity_fx.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity_fx)

        self._build_brand_animations()
        self._build_fade_out()

        self._repaint_timer = QTimer(self)
        self._repaint_timer.setInterval(16)
        self._repaint_timer.timeout.connect(self._tick)

    # -- Qt properties for QPropertyAnimation ----------------------------------

    def _get_scale(self):
        return self._scale

    def _set_scale(self, v):
        self._scale = v

    q_scale = pyqtProperty(float, _get_scale, _set_scale)

    def _get_shimmer_x(self):
        return self._shimmer_x

    def _set_shimmer_x(self, v):
        self._shimmer_x = v

    q_shimmer_x = pyqtProperty(float, _get_shimmer_x, _set_shimmer_x)

    def _get_text_opacity(self):
        return self._text_opacity

    def _set_text_opacity(self, v):
        self._text_opacity = v

    q_text_opacity = pyqtProperty(float, _get_text_opacity, _set_text_opacity)

    # -- Animations ------------------------------------------------------------

    def _build_brand_animations(self):
        self._fade_in = QPropertyAnimation(self._opacity_fx, b"opacity", self)
        self._fade_in.setDuration(500)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._scale_anim = QPropertyAnimation(self, b"q_scale", self)
        self._scale_anim.setDuration(_BRAND_DURATION_MS)
        self._scale_anim.setStartValue(0.95)
        self._scale_anim.setEndValue(1.0)
        self._scale_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._shimmer_anim = QPropertyAnimation(self, b"q_shimmer_x", self)
        self._shimmer_anim.setDuration(1000)
        self._shimmer_anim.setStartValue(-0.15)
        self._shimmer_anim.setEndValue(1.15)
        self._shimmer_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)

        self._text_fade = QPropertyAnimation(self, b"q_text_opacity", self)
        self._text_fade.setDuration(600)
        self._text_fade.setStartValue(0.0)
        self._text_fade.setEndValue(1.0)
        self._text_fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._brand_group = QParallelAnimationGroup(self)
        self._brand_group.addAnimation(self._fade_in)
        self._brand_group.addAnimation(self._scale_anim)

    def _build_fade_out(self):
        self._fade_out = QPropertyAnimation(self._opacity_fx, b"opacity", self)
        self._fade_out.setDuration(400)
        self._fade_out.setStartValue(1.0)
        self._fade_out.setEndValue(0.0)
        self._fade_out.setEasingCurve(QEasingCurve.Type.InCubic)
        self._fade_out.finished.connect(self._on_fade_out_done)

    # -- Public API ------------------------------------------------------------

    def start(self):
        self._phase = _PHASE_BRAND
        self._center_on_screen()
        self.show()
        self._brand_group.start()
        QTimer.singleShot(300, self._shimmer_anim.start)
        QTimer.singleShot(400, self._text_fade.start)
        self._repaint_timer.start()

    def enter_deploy_phase(self):
        self._phase = _PHASE_DEPLOY
        self._bar_visible = True
        self._status_text = "正在启动服务..."
        self._progress = 0.0
        self._target_progress = 0.05
        self._deploy_stage = "starting"

    def finish(self):
        if self._phase == _PHASE_DONE:
            return
        self._phase = _PHASE_DONE
        self._progress = 1.0
        self._target_progress = 1.0
        self._status_text = "服务已就绪"
        self.update()
        QTimer.singleShot(500, self._begin_fade_out)

    def finish_with_error(self, message="启动失败"):
        if self._phase == _PHASE_DONE:
            return
        self._phase = _PHASE_DONE
        self._status_text = message
        self.update()
        QTimer.singleShot(1800, self._begin_fade_out)

    def finish_for_wizard(self):
        if self._phase == _PHASE_DONE:
            return
        self._phase = _PHASE_DONE
        self._repaint_timer.stop()
        self.update()
        QTimer.singleShot(200, self._begin_fade_out)

    def on_status_changed(self, status):
        if self._phase != _PHASE_DEPLOY:
            return

        if status == "启动中...":
            self._status_text = "正在启动服务..."
            self._deploy_stage = "starting"
            self._set_progress(0.10)
            return

        if status == "运行中":
            self._status_text = "服务已就绪"
            self._target_progress = 1.0
            self._progress = max(self._progress, 0.85)
            QTimer.singleShot(800, self.finish)
            return

        if status in {"启动失败", "启动超时"}:
            self.finish_with_error(status)
            return

    def on_progress_updated(self, text):
        if self._phase != _PHASE_DEPLOY:
            return

        if not text.startswith("__pull_progress__|"):
            return

        parts = text.split("|", 2)
        if len(parts) < 3:
            return

        _, phase, message = parts

        if phase == "start":
            self._deploy_stage = "pulling"
            self._status_text = "正在拉取镜像..."
            self._pull_total = 0
            self._pull_done = 0
            self._set_progress(0.20)

        elif phase == "update":
            layer_match = re.match(r"^([a-f0-9]{6,64}):\s*(.+)$", message, re.IGNORECASE)
            if layer_match:
                _layer_id, layer_status = layer_match.groups()
                if layer_status.startswith(("Pulling fs layer", "Waiting", "Downloading")):
                    self._pull_total = max(self._pull_total, 1)
                if layer_status.startswith(("Pull complete", "Already exists", "Download complete")):
                    self._pull_done += 1
                    self._pull_total = max(self._pull_total, self._pull_done)

                if self._pull_total > 0:
                    ratio = min(self._pull_done / self._pull_total, 1.0)
                    self._set_progress(0.20 + ratio * 0.60)
                    self._status_text = f"拉取镜像层 {self._pull_done}/{self._pull_total}..."

        elif phase == "stage":
            self._status_text = message or "正在拉取镜像..."
            self._pull_total = 0
            self._pull_done = 0

        elif phase == "done":
            self._deploy_stage = "waiting"
            self._set_progress(0.85)
            self._status_text = "等待服务就绪..."

        elif phase == "error":
            self._status_text = message or "镜像拉取出错"

    # -- Internal --------------------------------------------------------------

    def _set_progress(self, value):
        self._target_progress = min(max(value, 0.0), 1.0)

    def _tick(self):
        if self._phase == _PHASE_DEPLOY and self._progress < self._target_progress:
            gap = self._target_progress - self._progress
            step = max(gap * 0.08, 0.003)
            self._progress = min(self._progress + step, self._target_progress)
        self.update()

    def _begin_fade_out(self):
        self._repaint_timer.stop()
        self._fade_out.start()

    def _on_fade_out_done(self):
        self.hide()
        self.finished.emit()

    def _center_on_screen(self):
        from PyQt6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = geo.x() + (geo.width() - self.width()) // 2
            y = geo.y() + (geo.height() - self.height()) // 2
            self.move(x, y)

    # -- Painting --------------------------------------------------------------

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        ox, oy = 20, 16
        w, h = _IMG_W, _IMG_H
        total_h = h + _STATUS_H + _BAR_H

        clip = QPainterPath()
        clip.addRoundedRect(float(ox), float(oy), float(w), float(total_h), _RADIUS, _RADIUS)
        p.setClipPath(clip)

        p.fillPath(clip, _BG)

        self._draw_image(p, ox, oy, w, h)

        if self._phase == _PHASE_BRAND and self._shimmer_x < 1.15:
            self._draw_shimmer(p, ox, oy, w, h)

        self._draw_status_bar(p, ox, oy + h, w)
        self._draw_progress_bar(p, ox, oy + h + _STATUS_H, w)

        p.end()

    def _draw_image(self, p, ox, oy, w, h):
        if self._pixmap.isNull():
            return

        scale = self._scale
        sw = int(w / scale)
        sh = int(h / scale)
        scaled = self._pixmap.scaled(
            sw, sh,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )

        crop_x = (scaled.width() - w) // 2
        crop_y = (scaled.height() - h) // 2
        p.drawPixmap(ox, oy, scaled, crop_x, crop_y, w, h)

    def _draw_shimmer(self, p, ox, oy, w, h):
        shimmer_w = int(w * 0.25)
        cx = int(ox + w * self._shimmer_x)

        grad = QLinearGradient(cx - shimmer_w // 2, oy, cx + shimmer_w // 2, oy)
        grad.setColorAt(0.0, QColor(255, 255, 255, 0))
        grad.setColorAt(0.45, QColor(255, 255, 255, 70))
        grad.setColorAt(0.55, QColor(255, 255, 255, 70))
        grad.setColorAt(1.0, QColor(255, 255, 255, 0))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawRect(cx - shimmer_w // 2, oy, shimmer_w, h)

    def _draw_status_bar(self, p, ox, y, w):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 252, 251, 245))
        p.drawRect(ox, y, w, _STATUS_H)

        alpha = int(self._text_opacity * 255)
        if alpha <= 0:
            return

        text_color = QColor(38, 64, 87, alpha)
        p.setPen(text_color)
        font = QFont("Microsoft YaHei UI", 10)
        font.setWeight(QFont.Weight.Medium)
        p.setFont(font)

        from PyQt6.QtCore import QRectF
        text_rect = QRectF(ox + 16, y, w - 32, _STATUS_H)
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._status_text)

        if self._phase == _PHASE_DEPLOY and self._progress > 0:
            pct_text = f"{int(self._progress * 100)}%"
            p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, pct_text)

    def _draw_progress_bar(self, p, ox, y, w):
        p.setPen(Qt.PenStyle.NoPen)

        if self._phase == _PHASE_BRAND:
            p.setBrush(_ACCENT_LIGHT.lighter(115))
            p.drawRect(ox, y, w, _BAR_H)
            return

        track = QColor(230, 220, 218)
        p.setBrush(track)
        p.drawRect(ox, y, w, _BAR_H)

        bar_w = int(w * min(self._progress, 1.0))
        if bar_w > 0:
            grad = QLinearGradient(ox, y, ox + bar_w, y)
            grad.setColorAt(0, _ACCENT)
            grad.setColorAt(1, _ACCENT_LIGHT)
            p.setBrush(grad)
            p.drawRect(ox, y, bar_w, _BAR_H)
