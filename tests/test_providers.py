from providers.base import Trip, DIRECTION_MG_MNSK, DIRECTION_MNSK_MG, DIRECTION_LABELS


def test_trip_fields():
    t = Trip(
        trip_id="abc",
        provider="test",
        route="Могилёв -> Минск",
        date="2026-05-24",
        departure_time="14:20",
        free_seats=2,
        price=20.0,
        currency="руб.",
    )
    assert t.trip_id == "abc"
    assert t.free_seats == 2
    assert t.departure_time == "14:20"


def test_direction_constants():
    assert DIRECTION_MG_MNSK == "mg_mnsk"
    assert DIRECTION_MNSK_MG == "mnsk_mg"
    assert "Могилёв" in DIRECTION_LABELS[DIRECTION_MG_MNSK]
    assert "Минск" in DIRECTION_LABELS[DIRECTION_MNSK_MG]
