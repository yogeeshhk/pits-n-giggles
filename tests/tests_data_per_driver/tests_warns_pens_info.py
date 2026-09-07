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

import logging
from unittest.mock import MagicMock

from apps.backend.state_mgmt_layer.data_per_driver import DataPerDriver
from apps.backend.state_mgmt_layer.intf.readers.helpers.drivers_list_rsp import DriversListRsp
from apps.backend.state_mgmt_layer.intf.readers.stream_overlay import StreamOverlayData
from lib.f1_types import LapData

from .tests_data_per_driver_base import F1DataPerDriverTest


class TestWarningPenaltyHistory(F1DataPerDriverTest):

    @staticmethod
    def _lap_data(
        corner_cutting_warnings: int,
        total_warnings: int,
        penalties: int,
        num_dt: int = 0,
        num_sg: int = 0,
    ) -> LapData:
        return LapData.from_values(
            last_lap_time_ms=0,
            current_lap_time_ms=1000,
            sector1_time_ms=0,
            sector1_time_minutes=0,
            sector2_time_ms=0,
            sector2_time_minutes=0,
            delta_to_front_ms=0,
            delta_to_front_minutes=0,
            delta_to_leader_ms=0,
            delta_to_leader_minutes=0,
            lap_distance=100.0,
            total_distance=100.0,
            safety_car_delta=0.0,
            car_position=1,
            current_lap_num=1,
            pit_status=0,
            num_pit_stops=0,
            sector=0,
            current_lap_invalid=0,
            penalties=penalties,
            total_warnings=total_warnings,
            corner_cutting_warnings=corner_cutting_warnings,
            num_unserved_drive_through_pens=num_dt,
            num_unserved_stop_go_pens=num_sg,
            grid_position=1,
            driver_status=4,
            result_status=2,
            pit_lane_timer_active=0,
            pit_lane_time_ms=0,
            pit_stop_timer_ms=0,
            pit_stop_should_serve_pen=0,
            speed_trap_fastest_speed=0.0,
            speed_trap_fastest_lap=0,
        )

    def setUp(self):
        super().setUp()
        self.driver = DataPerDriver(
            index=0,
            logger=logging.getLogger("test"),
            total_laps=50,
            state_ref=MagicMock(),
            weather_aware_prediction=False,
            tyre_wear_window_size=None,
            harvest_power_window_size=5,
        )
        # Exercise the warning readers without unrelated telemetry setup.
        self.dashboard = DriversListRsp.__new__(DriversListRsp)
        self.overlay = StreamOverlayData.__new__(StreamOverlayData)
        self.overlay.m_ref_obj = self.driver

    def _assert_counts(self, corner, total, penalties, num_dt=0, num_sg=0):
        self.assertEqual(
            {
                "corner-cutting-warnings": corner,
                "other-warnings": total - corner,
                "total-warnings": total,
                "time-penalties": penalties,
                "num-dt": num_dt,
                "num-sg": num_sg,
            },
            self.dashboard._getWarningsPenaltiesJSON(self.driver),
        )
        self.overlay._StreamOverlayData__initPenalties()
        self.assertEqual(corner, self.overlay.m_corner_cutting_warnings)
        self.assertEqual(total, self.overlay.m_total_warnings)
        self.assertEqual(penalties, self.overlay.m_penalties)
        self.assertEqual(num_dt, self.overlay.m_num_dt)
        self.assertEqual(num_sg, self.overlay.m_num_sg)

    def test_flashback_removes_warnings_and_retry_counts_once(self):
        for counts in [(1, 2, 3, 1, 1), (0, 0, 0, 0, 0), (1, 2, 3, 1, 1)]:
            self.driver.updateLapDataPacketCopy(self._lap_data(*counts), 1000)
            self._assert_counts(*counts)

    def test_partial_flashback_preserves_earlier_warnings(self):
        for counts in [(3, 5, 6, 2, 1), (1, 2, 3, 1, 0)]:
            self.driver.updateLapDataPacketCopy(self._lap_data(*counts), 1000)
            self._assert_counts(*counts)
        entries = self.driver.m_warning_penalty_history.getEntries()
        self.assertTrue(any(entry.m_new_value < entry.m_old_value for entry in entries))

    def test_served_penalties_and_duplicate_packets(self):
        for counts in [(1, 2, 3, 1, 1), (1, 2, 0, 0, 0), (1, 2, 0, 0, 0)]:
            self.driver.updateLapDataPacketCopy(self._lap_data(*counts), 1000)
            self._assert_counts(*counts)

    def test_missing_lap_data(self):
        self.assertTrue(all(
            value is None
            for value in self.dashboard._getWarningsPenaltiesJSON(self.driver).values()
        ))
        self.overlay._StreamOverlayData__initPenalties()
        self.assertEqual(0, self.overlay.m_corner_cutting_warnings)
