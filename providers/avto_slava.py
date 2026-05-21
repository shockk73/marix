from .timetable_base import TimetableBaseProvider


class AvtoSlavaProvider(TimetableBaseProvider):
    name = "avto_slava"
    display_name = "Автослава"
    url = "https://avto-slava.by/timetable/trips/"
    directions = {
        "mg_mnsk": ("2", "1"),
        "mnsk_mg": ("1", "2"),
    }
