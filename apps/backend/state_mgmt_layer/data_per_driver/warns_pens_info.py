# MIT License
#
# Copyright (c) [2024] [Ashwin Natarajan]
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

from enum import Enum, auto
from typing import Any, Dict, List, Optional

from lib.f1_types import LapData

# -------------------------------------- GLOBALS -----------------------------------------------------------------------

# -------------------------------------- CLASS DEFINITIONS -------------------------------------------------------------

class WarningPenaltyEntry:
    """
    Class that captures one warning/penalty entry
    """
    class EntryType(Enum):
        """
        Enum representing the type of warning/penalty entry
        """

        CORNER_CUTTING_WARNING = auto()
        DT_PENALTY = auto()
        SG_PENALTY = auto()
        OTHER_WARNING = auto()
        TIME_PENALTY = auto()

        @staticmethod
        def isValid(entry_type_code: int) -> bool:
            """
            Check if the entry type is valid

            Args:
                entry_type_code (int): The entry type code to check

            Returns:
                bool: True if the entry type is valid
            """

            if isinstance(entry_type_code, WarningPenaltyEntry.EntryType):
                return True  # It's already an instance of EntryType
            return any(entry_type_code == member.value for member in  WarningPenaltyEntry.EntryType)

        def __str__(self) -> str:
            """
            Get the name of the entry type

            Returns:
                str: The name of the entry type
            """
            return self.name.replace('_', ' ').title()

    def __init__(self,
                    entry_type: EntryType,
                    old_value: int,
                    new_value: int,
                    lap_num: int,
                    sector_number: int,
                    distance_from_start: float,
                    lap_progress_percent: float) -> None:

        """
        Init the warning/penalty entry

        Args:
            entry_type (EntryType): The entry type
            old_value (int): The old value of the entry
            new_value (int): The new value of the entry
            lap_number (int): The lap number
            sector_number (int): The sector number
            distance_from_start (float): The distance from start
            lap_progress_percent (float): The lap progress percent
        """

        assert old_value != new_value
        self.m_entry_type: WarningPenaltyEntry.EntryType = entry_type
        self.m_old_value: int = old_value
        self.m_new_value: int = new_value
        self.m_lap_number: int = lap_num
        self.m_sector_number: int = sector_number
        self.m_distance_from_start: float = distance_from_start
        self.m_lap_progress_percent: float = lap_progress_percent

    def toJSON(self) -> Dict[str, Any]:
        """
        Dump this object into JSON

        Returns:
            Dict[str, Any]: The JSON dump
        """
        return {
            "entry-type" : str(self.m_entry_type),
            "old-value" : self.m_old_value,
            "new-value" : self.m_new_value,
            "lap-number" : self.m_lap_number,
            "sector-number" : str(self.m_sector_number),
            "distance-from-start" : self.m_distance_from_start,
            "lap-progress-percent" : self.m_lap_progress_percent
        }

    def __repr__(self) -> str:
        """Return the string representation of this object

        Returns:
            str: string representation of this object
        """
        return  f"entry_type: {self.m_entry_type} " \
                f"new_value: {self.m_new_value} " \
                f"lap: {self.m_lap_number} " \
                f"sector: {self.m_sector_number}"

    def __str__(self) -> str:
        """Return the string representation of this object

        Returns:
            str: string representation of this object
        """
        return self.__repr__()

class WarningPenaltyHistory:
    """Warns/Pens histroy tracker class
    """
    def __init__(self) -> None:
        """Init the history tracker
        """
        self.m_history: List[WarningPenaltyEntry] = []
        self.m_observed_corner_cutting_warnings: int = 0
        self.m_observed_other_warnings: int = 0
        self.m_observed_time_penalties: int = 0
        self.m_observed_dt_penalties: int = 0
        self.m_observed_sg_penalties: int = 0

    def _track_observed_delta(self, entry_type: WarningPenaltyEntry.EntryType, old_value: int, new_value: int) -> None:
        """Track positive counter deltas even if the game rewinds them later."""
        delta = new_value - old_value
        if delta <= 0:
            return

        match entry_type:
            case WarningPenaltyEntry.EntryType.CORNER_CUTTING_WARNING:
                self.m_observed_corner_cutting_warnings += delta
            case WarningPenaltyEntry.EntryType.OTHER_WARNING:
                self.m_observed_other_warnings += delta
            case WarningPenaltyEntry.EntryType.TIME_PENALTY:
                self.m_observed_time_penalties += delta
            case WarningPenaltyEntry.EntryType.DT_PENALTY:
                self.m_observed_dt_penalties += delta
            case WarningPenaltyEntry.EntryType.SG_PENALTY:
                self.m_observed_sg_penalties += delta

    def _append_entry(self,
                      entry_type: WarningPenaltyEntry.EntryType,
                      old_value: int,
                      new_value: int,
                      lap_num: int,
                      sector_number: int,
                      distance_from_start: float,
                      lap_progress_percent: float) -> None:
        """Append a history entry and update observed live counters."""
        self._track_observed_delta(entry_type, old_value, new_value)
        self.m_history.append(WarningPenaltyEntry(
            entry_type=entry_type,
            old_value=old_value,
            new_value=new_value,
            lap_num=lap_num,
            sector_number=sector_number,
            distance_from_start=distance_from_start,
            lap_progress_percent=lap_progress_percent
        ))

    def update(self, curr_packet: LapData, full_lap_distance: int, old_packet: Optional[LapData] = None) -> None:
        """Update the history tracker with the new warns/pens

        Args:
            curr_packet (LapData): Current lap data packet
            full_lap_distance (int): _description_
            old_packet (Optional[LapData], optional): Previously stored lap data packet. Defaults to None.
        """
        curr_other_warnings = curr_packet.m_totalWarnings - curr_packet.m_cornerCuttingWarnings
        lap_progress_percent=(curr_packet.m_lapDistance/float(full_lap_distance))*100.0
        if not old_packet:
            # If any penalties/warnings exist, set it
            if curr_packet.m_cornerCuttingWarnings > 0:
                # Add the corner cutting warning
                self._append_entry(
                    entry_type=WarningPenaltyEntry.EntryType.CORNER_CUTTING_WARNING,
                    old_value=0,
                    new_value=curr_packet.m_cornerCuttingWarnings,
                    lap_num=curr_packet.m_currentLapNum,
                    sector_number=curr_packet.m_sector,
                    distance_from_start=curr_packet.m_lapDistance,
                    lap_progress_percent=lap_progress_percent
                    )
            if curr_other_warnings > 0:
                # Add other warnings
                self._append_entry(
                    entry_type=WarningPenaltyEntry.EntryType.OTHER_WARNING,
                    old_value=0,
                    new_value=curr_other_warnings,
                    lap_num=curr_packet.m_currentLapNum,
                    sector_number=curr_packet.m_sector,
                    distance_from_start=curr_packet.m_lapDistance,
                    lap_progress_percent=lap_progress_percent
                    )
            if curr_packet.m_numUnservedDriveThroughPens > 0:
                # Add the drive through penalty
                self._append_entry(
                    entry_type=WarningPenaltyEntry.EntryType.DT_PENALTY,
                    old_value=0,
                    new_value=curr_packet.m_numUnservedDriveThroughPens,
                    lap_num=curr_packet.m_currentLapNum,
                    sector_number=curr_packet.m_sector,
                    distance_from_start=curr_packet.m_lapDistance,
                    lap_progress_percent=lap_progress_percent
                    )
            if curr_packet.m_numUnservedStopGoPens > 0:
                # Add the stop go penalty
                self._append_entry(
                    entry_type=WarningPenaltyEntry.EntryType.SG_PENALTY,
                    old_value=0,
                    new_value=curr_packet.m_numUnservedStopGoPens,
                    lap_num=curr_packet.m_currentLapNum,
                    sector_number=curr_packet.m_sector,
                    distance_from_start=curr_packet.m_lapDistance,
                    lap_progress_percent=lap_progress_percent
                    )
            if curr_packet.m_penalties > 0:
                # Add the time penalty
                self._append_entry(
                    entry_type=WarningPenaltyEntry.EntryType.TIME_PENALTY,
                    old_value=0,
                    new_value=curr_packet.m_penalties,
                    lap_num=curr_packet.m_currentLapNum,
                    sector_number=curr_packet.m_sector,
                    distance_from_start=curr_packet.m_lapDistance,
                    lap_progress_percent=lap_progress_percent
                    )
        else:
            old_other_warnings  = old_packet.m_totalWarnings - \
                old_packet.m_cornerCuttingWarnings
            # If there is a diff in corner cutting warnings, add it
            if curr_packet.m_cornerCuttingWarnings != old_packet.m_cornerCuttingWarnings:
                self._append_entry(
                    entry_type=WarningPenaltyEntry.EntryType.CORNER_CUTTING_WARNING,
                    old_value=old_packet.m_cornerCuttingWarnings,
                    new_value=curr_packet.m_cornerCuttingWarnings,
                    lap_num=curr_packet.m_currentLapNum,
                    sector_number=curr_packet.m_sector,
                    distance_from_start=curr_packet.m_lapDistance,
                    lap_progress_percent=lap_progress_percent
                    )
            # If there is a diff in other warnings, add it
            if curr_other_warnings != old_other_warnings:
                self._append_entry(
                    entry_type=WarningPenaltyEntry.EntryType.OTHER_WARNING,
                    old_value=old_other_warnings,
                    new_value=curr_other_warnings,
                    lap_num=curr_packet.m_currentLapNum,
                    sector_number=curr_packet.m_sector,
                    distance_from_start=curr_packet.m_lapDistance,
                    lap_progress_percent=lap_progress_percent
                    )
            # If there is a diff in drive through penalties, add it
            if curr_packet.m_numUnservedDriveThroughPens != old_packet.m_numUnservedDriveThroughPens:
                self._append_entry(
                    entry_type=WarningPenaltyEntry.EntryType.DT_PENALTY,
                    old_value=old_packet.m_numUnservedDriveThroughPens,
                    new_value=curr_packet.m_numUnservedDriveThroughPens,
                    lap_num=curr_packet.m_currentLapNum,
                    sector_number=curr_packet.m_sector,
                    distance_from_start=curr_packet.m_lapDistance,
                    lap_progress_percent=lap_progress_percent
                    )
            # If there is a diff in stop go penalties, add it
            if curr_packet.m_numUnservedStopGoPens != old_packet.m_numUnservedStopGoPens:
                self._append_entry(
                    entry_type=WarningPenaltyEntry.EntryType.SG_PENALTY,
                    old_value=old_packet.m_numUnservedStopGoPens,
                    new_value=curr_packet.m_numUnservedStopGoPens,
                    lap_num=curr_packet.m_currentLapNum,
                    sector_number=curr_packet.m_sector,
                    distance_from_start=curr_packet.m_lapDistance,
                    lap_progress_percent=lap_progress_percent
                    )
            # If there is a diff in time penalties, add it
            if curr_packet.m_penalties != old_packet.m_penalties:
                self._append_entry(
                    entry_type=WarningPenaltyEntry.EntryType.TIME_PENALTY,
                    old_value=old_packet.m_penalties,
                    new_value=curr_packet.m_penalties,
                    lap_num=curr_packet.m_currentLapNum,
                    sector_number=curr_packet.m_sector,
                    distance_from_start=curr_packet.m_lapDistance,
                    lap_progress_percent=lap_progress_percent
                    )

    def getEntries(self) -> List[WarningPenaltyEntry]:
        """Get the warnings penalties history entries

        Returns:
            List[WarningPenaltyEntry]: History entries
        """
        return self.m_history

    def getObservedTotalsJSON(self) -> Dict[str, int]:
        """Get live counters that include penalties/warnings erased by flashback."""
        return {
            "corner-cutting-warnings": self.m_observed_corner_cutting_warnings,
            "other-warnings": self.m_observed_other_warnings,
            "total-warnings": self.m_observed_corner_cutting_warnings + self.m_observed_other_warnings,
            "time-penalties": self.m_observed_time_penalties,
            "num-dt": self.m_observed_dt_penalties,
            "num-sg": self.m_observed_sg_penalties,
        }
