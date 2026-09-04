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

from apps.backend.state_mgmt_layer.data_per_driver.warns_pens_info import WarningPenaltyHistory
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

    def test_observed_totals_keep_erased_rewind_counts(self):
        history = WarningPenaltyHistory()
        full_lap_distance = 1000

        before_rewind = self._lap_data(
            corner_cutting_warnings=1,
            total_warnings=1,
            penalties=3,
            num_dt=1,
        )
        after_rewind = self._lap_data(
            corner_cutting_warnings=0,
            total_warnings=0,
            penalties=0,
            num_dt=0,
        )
        after_retry = self._lap_data(
            corner_cutting_warnings=1,
            total_warnings=2,
            penalties=6,
            num_dt=1,
            num_sg=1,
        )

        history.update(before_rewind, full_lap_distance)
        history.update(after_rewind, full_lap_distance, before_rewind)
        history.update(after_retry, full_lap_distance, after_rewind)

        self.assertEqual(
            {
                "corner-cutting-warnings": 2,
                "other-warnings": 1,
                "total-warnings": 3,
                "time-penalties": 9,
                "num-dt": 2,
                "num-sg": 1,
            },
            history.getObservedTotalsJSON(),
        )
