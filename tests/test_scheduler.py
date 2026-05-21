from providers.base import Trip

from scheduler import filter_trips_in_window, compute_newly_available


def _trip(trip_id: str, departure_time: str, free_seats: int) -> Trip:
    return Trip(
        trip_id=trip_id, provider="test", route="test",
        date="2026-05-24", departure_time=departure_time,
        free_seats=free_seats, price=20.0, currency="руб.",
    )


def test_filter_trips_in_window():
    trips = [
        _trip("a", "10:00", 2),
        _trip("b", "14:00", 1),
        _trip("c", "23:30", 0),
    ]
    result = filter_trips_in_window(trips, "11:00", "23:00")
    assert len(result) == 1
    assert result[0].trip_id == "b"


def test_filter_includes_boundary():
    trips = [
        _trip("a", "11:00", 2),
        _trip("b", "23:00", 1),
    ]
    result = filter_trips_in_window(trips, "11:00", "23:00")
    assert len(result) == 2


def test_newly_available_detects_new():
    trips = [
        _trip("a", "14:00", 2),
        _trip("b", "15:00", 1),
    ]
    notified = {"a"}
    newly, updated_notified = compute_newly_available(trips, notified)
    assert len(newly) == 1
    assert newly[0].trip_id == "b"
    assert "a" in updated_notified
    assert "b" in updated_notified


def test_newly_available_removes_gone():
    trips = [
        _trip("a", "14:00", 0),
    ]
    notified = {"a"}
    newly, updated_notified = compute_newly_available(trips, notified)
    assert len(newly) == 0
    assert "a" not in updated_notified


def test_newly_available_no_double_notify():
    trips = [_trip("a", "14:00", 2)]
    notified = {"a"}
    newly, updated_notified = compute_newly_available(trips, notified)
    assert len(newly) == 0
    assert "a" in updated_notified
