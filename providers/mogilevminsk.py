from .timetable_base import TimetableBaseProvider


class MogilevMinskProvider(TimetableBaseProvider):
    name = "mogilevminsk"
    display_name = "Минск Экспресс"
    url = "https://mogilevminsk.by/timetable/trips/"
    directions = {
        "mg_mnsk": ("2", "1"),
        "mnsk_mg": ("1", "2"),
    }
