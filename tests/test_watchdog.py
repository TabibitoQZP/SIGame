from gpa.execution.watchdog import PressureGuard, host_pressure


def test_transient_pressure_pauses_and_sustained_pressure_aborts_without_host_stress():
    guard = PressureGuard()
    moderate = {"some_avg10": 12, "full_avg10": 1, "swap_out_pages": 0}
    assert guard.update(moderate, 0)["elevated"]
    severe = {**moderate, "full_avg10": 15}
    assert not guard.update(severe, 1)["stop"]
    assert not guard.update(severe, 5)["stop"]
    assert guard.update(severe, 6)["stop"]
    recovered = guard.update({"some_avg10": 0, "full_avg10": 0, "swap_out_pages": 0}, 7)
    assert not recovered["elevated"] and not recovered["stop"]
    assert not guard.update(severe, 8)["stop"]


def test_swap_out_rate_and_counter_reset_do_not_depend_on_free_ram():
    guard = PressureGuard()
    sample = {"some_avg10": 25, "full_avg10": 0, "swap_out_pages": 0}
    guard.update(sample, 0)
    import os

    pages = 64 * 1024**2 // os.sysconf("SC_PAGE_SIZE")
    for second in range(1, 7):
        result = guard.update({**sample, "swap_out_pages": pages * second}, second)
        assert result["swap_out_bytes_per_second"] == 64 * 1024**2
    assert result["stop"]
    result = guard.update({**sample, "swap_out_pages": 0}, 7)
    assert result["swap_out_bytes_per_second"] == 0 and not result["stop"]


def test_actual_pressure_sampling_is_read_only_and_has_declared_fields():
    sample = host_pressure()
    assert set(sample) == {"some_avg10", "full_avg10", "swap_out_pages"}
    assert all(value is None or value >= 0 for value in sample.values())
