# MIT License
#
# Copyright (c) [2026] [Ashwin Natarajan]
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# -------------------------------------- IMPORTS -----------------------------------------------------------------------

from pathlib import Path
from typing import final

from lib.config import OverlayId, PngSettings
from lib.logger import PngLogger

from ...hf_types import HudOverlayData
from ..base.base_overlay import BaseOverlay

# -------------------------------------- CLASSES -----------------------------------------------------------------------

class WarningsTelemetryOverlay(BaseOverlay):
    """Transparent warning counter overlay."""

    QML_FILE = Path(__file__).parent / "warnings_telemetry.qml"
    OVERLAY_ID = OverlayId.WARNINGS_TELEMETRY

    ANIMATION_DRIVEN = True

    def __init__(self, settings: PngSettings, logger: PngLogger) -> None:
        super().__init__(settings, logger)
        self.subscribe_hf(HudOverlayData)

    @final
    def render_frame(self):
        data = self.get_latest_hf_data(HudOverlayData)
        if not data:
            return

        self.set_qml_property("cornerCuttingWarnings", data.corner_cutting_warnings)
        self.set_qml_property("trackLimitsWarnings", data.track_limits_warnings)
