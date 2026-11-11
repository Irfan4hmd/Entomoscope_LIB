import unittest

import constants
from MotorController.motorcontroller import RotaryAxis, RotationError, RotationStopped


class FakeTurntable:
    """Answers the turntable commands the way servo.ino does."""

    def __init__(self, end_steps=4321, home_reply="rot_home"):
        self.end_steps = end_steps
        self.home_reply = home_reply
        self.pos = None
        self.written = []
        self.pending = b""
        self.is_open = True

    def reset_input_buffer(self):
        self.pending = b""

    def write(self, data):
        command = data.decode("ascii")
        self.written.append(command)
        if command == "H":
            self.pos = 0
            reply = self.home_reply
        elif command == "E":
            self.pos = self.end_steps
            reply = f"rot_span:{self.pos}"
        elif command.startswith("G"):
            self.pos = int(command[1:])
            reply = f"rot_at:{self.pos}"
        else:
            return
        # split across reads, as the firmware's replies can be
        self.pending += reply.encode("ascii") + b"\r\n"

    def readline(self):
        chunk, self.pending = self.pending[:5], self.pending[5:]
        return chunk


class RotaryAxisTests(unittest.TestCase):
    def test_views_are_derived_from_the_switch_count(self):
        fake = FakeTurntable(end_steps=5400)
        rotary = RotaryAxis(fake)

        rotary.home()
        rotary.seek_end()
        self.assertAlmostEqual(rotary.steps_per_degree, 5400 / constants.MACRO_END_SWITCH_ANGLE)

        rotary.move_to_angle(180)
        self.assertEqual(rotary.position, 3600)
        rotary.move_to_angle(90)
        self.assertEqual(rotary.position, 1800)
        self.assertAlmostEqual(rotary.angle, 90)

    def test_clockwise_moves_overshoot_and_finish_counter_clockwise(self):
        fake = FakeTurntable(end_steps=2700)
        rotary = RotaryAxis(fake)
        rotary.home()
        rotary.seek_end()
        fake.written.clear()

        rotary.move_to_angle(180, overshoot_deg=5)
        self.assertEqual(fake.written, ["G1750", "G1800"])

        fake.written.clear()
        rotary.move_to_angle(200, overshoot_deg=5)
        self.assertEqual(fake.written, ["G2000"])

    def test_overshoot_never_goes_past_home(self):
        fake = FakeTurntable(end_steps=2700)
        rotary = RotaryAxis(fake)
        rotary.home()
        rotary.seek_end()
        fake.written.clear()

        rotary.move_to_angle(2, overshoot_deg=5)
        self.assertEqual(fake.written, ["G0", "G20"])

    def test_calibrate_returns_to_zero(self):
        fake = FakeTurntable(end_steps=2700)
        rotary = RotaryAxis(fake)
        rotary.calibrate()
        self.assertEqual(fake.written[:2], ["H", "E"])
        self.assertEqual(rotary.position, 0)
        self.assertTrue(rotary.calibrated)

    def test_jog_is_relative_and_stays_inside_the_switches(self):
        fake = FakeTurntable(end_steps=2700)
        rotary = RotaryAxis(fake)
        rotary.calibrate()

        rotary.jog(30)
        self.assertAlmostEqual(rotary.angle, 30)
        rotary.jog(-100)
        self.assertEqual(rotary.position, 0)
        rotary.jog(500)
        # short of the end switch, which only seek_end drives onto
        self.assertLess(rotary.position, 2700)
        self.assertAlmostEqual(rotary.angle, 269.5)

    def test_jog_needs_calibration(self):
        rotary = RotaryAxis(FakeTurntable())
        with self.assertRaises(RotationError):
            rotary.jog(10)

    def test_moves_are_refused_before_calibration(self):
        rotary = RotaryAxis(FakeTurntable())
        rotary.home()
        with self.assertRaises(RotationError):
            rotary.move_to_angle(90)

    def test_failed_home_leaves_the_turntable_unhomed(self):
        rotary = RotaryAxis(FakeTurntable(home_reply="rot_failed"))
        with self.assertRaises(RotationError):
            rotary.home()
        self.assertFalse(rotary.homed)

    def test_stop_reply_raises_stopped(self):
        rotary = RotaryAxis(FakeTurntable(home_reply="rot_stopped:-120"))
        with self.assertRaises(RotationStopped):
            rotary.home()

    def test_end_switch_without_movement_is_an_error(self):
        rotary = RotaryAxis(FakeTurntable(end_steps=0))
        rotary.home()
        with self.assertRaises(RotationError):
            rotary.seek_end()

    def test_refuses_while_the_focus_stage_is_moving(self):
        class BusyAxis:
            queue = [1.0]

        fake = FakeTurntable()
        rotary = RotaryAxis(fake, BusyAxis())
        with self.assertRaises(RotationError):
            rotary.home()
        self.assertEqual(fake.written, [])


if __name__ == "__main__":
    unittest.main()
