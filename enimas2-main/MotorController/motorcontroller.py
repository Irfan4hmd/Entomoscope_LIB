import serial
from time import sleep, time
from serial import Serial
from threading import Thread
from logging import getLogger, DEBUG, INFO

import constants

logger = getLogger("MotorController")
logger.setLevel(INFO)


class Motor:
    def __init__(self, arduino):
        self.arduino = arduino

    def turn_left(self, n: int):
        self.arduino.write(bytes(f"l{n}", "ascii"))

    def turn_right(self, n: int):
        self.arduino.write(bytes(f"r{n}", "ascii"))

    def stop(self):
        self.arduino.write(b"stop")

    def set_speed(self, speed: float):
        if speed > constants.MAX_SPEED:
            logger.error(f"speed exceeds limit: {speed}")
            return
        logger.debug(f"new speed: {speed}")
        
        sleep(.01)
        self.arduino.write(bytes(f"d{int(360 * speed)}", "ascii"))
        sleep(.01) # short delay to separate the commands to the arduino


class Axis:
    # umrechnungsfaktor distanz->steps  n = 360°/0,225° * z mm / 0,8mm
    DIST_STEPS = 360 * constants.MICROSTEPS / 1.8 / constants.ROTATION_HEIGHT
    STEPS_DIST = 1 / DIST_STEPS

    # Kept as a class attribute so a test or a caller can override it per
    # instance; the value itself lives with the other tunables in constants.py.
    MOVE_REPLY_TIMEOUT = constants.AXIS_MOVE_REPLY_TIMEOUT

    def __init__(self, arduino: Serial):
        
        self.arduino = arduino
        self.motor = Motor(self.arduino)
        
        self.z = 0.0
        self._referenced = False

        # Both start unset on purpose. Defaulting them to the largest possible
        # travel fails OPEN: on a PI2AI the real travel is 200mm, so a default
        # of AXIS_LENGHT (343) would permit 108mm past the mechanical end. Until
        # the device profile and the lens have set these, the axis may retract
        # but not descend.
        self.lower_limit = None      # from the lens length, via set_current_limit
        self.travel_limit = None     # the device's physical travel, via set_travel_limit

        self.queue = []

    @property
    def max_z(self):
        """Deepest z the stage may ever reach, safety margin included.

        ``lower_limit`` is derived from the lens length and can land beyond the
        physical travel when the lens is short; ``travel_limit`` is the device's
        own mechanical range. The tighter of the two wins, and both are applied
        here rather than at the call sites, so no caller can widen the boundary.
        Nothing configured means no descent at all.
        """
        limits = [float(v) for v in (self.lower_limit, self.travel_limit) if v is not None]
        if not limits:
            return 0.0
        return max(0.0, min(limits) - constants.AXIS_SAFETY_MARGIN)

    def clamp_z(self, z):
        """Clamp an absolute position into the permitted travel."""
        return min(max(0.0, float(z)), self.max_z)
    
    def tracking_thread(self):
        # hört auf die Antwort vom Arduino nach einem Befehl wie move_up()
        #
        # The loop is bounded because an empty read satisfies neither exit
        # below: "" is not "done", and ""[1:].isdigit() is False. If the
        # Arduino stops answering -- a reset, a USB re-enumeration, a brownout
        # while the motor pulls current -- an unbounded loop would spin here
        # forever, and _move_for's join() would never return, freezing whatever
        # called it with wait=True.
        deadline = time() + self.MOVE_REPLY_TIMEOUT
        while time() < deadline:
            try:
                response = self.arduino.readline().decode("ascii").strip()
            except serial.SerialException as e:
                print(f"[tracking_thread] SerialException: {e}")
                break
            except UnicodeDecodeError as e:
                print(f"[tracking_thread] Decode error: {e}")
                break
            except Exception as e:
                print(f"[tracking_thread] Unexpected error: {e}")
                break

            if response == "done":
                # Der Befehl wurde korrekt und bis zum Ende ausgeführt
                self.queue.pop(0)
                if self.queue:
                    # es wird der nächste prozess in der warteschlange ausgeführt
                    self._move_for(self.queue[0], False)
                break

            if response[1:].isdigit():
                # Der Motor wurde gestoppt, bevor alle Schritte gemacht wurden
                # in dem Fall gibt der Arduino die Anzahl an übrigen Schritte zurück, die hier zurückgerechnet werden
                self.queue.clear()
                self.z += int(response) * self.STEPS_DIST
                break
        else:
            # Fell out on the deadline rather than via a break, so no reply was
            # understood. Clear the queue: leaving the finished move at its head
            # would make every later move_for() append behind it and never run.
            logger.error(
                f"No reply from the motor controller within {self.MOVE_REPLY_TIMEOUT}s; "
                "giving up on this move. The stage position may no longer be exact."
            )
            self.queue.clear()

    def move_for(self, dz :float, wait=False):
        # logger.debug(f"request move for {dz}, {self.queue=}")
        if not self.check_limit(dz):
            return False

        if self.queue:
            if wait:
                # A blocking caller (autofocus, stacking) must never be told the
                # stage arrived while the move is still sitting in the queue --
                # it would measure, and then step further down, against a
                # position the stage has not reached yet.
                if not self._wait_for_idle():
                    logger.error("Stage still busy; refusing the blocking move.")
                    return False
                # The queue drained while we waited, so re-check against the
                # position the stage actually ended up at.
                if not self.check_limit(dz):
                    return False
            else:
                # other moves are still in the queue
                self.queue.append(dz)
                return True

        # first request
        self.queue = [dz]
        return self._move_for(dz, wait)

    def _wait_for_idle(self, timeout=15):
        """Block until the queue has drained. Returns False on timeout."""
        deadline = time() + timeout
        while self.queue and time() < deadline:
            sleep(0.01)
        return not self.queue

    def _move_for(self, dz, wait):
        logger.debug(f"move for {dz}, {self.queue=}")

        if not self.check_limit(dz):
            self.queue.clear()
            return False

        # Last line of defence before steps reach the motor. Whatever the
        # caller worked out, the stage is never commanded past the boundary;
        # retraction is left untouched so it can always come back up.
        if dz > 0:
            dz = min(dz, max(0.0, self.max_z - self.z))
        else:
            dz = -min(-dz, max(0.0, self.z))

        steps = int(round(self.DIST_STEPS * abs(dz)))
        if steps <= 0:
            logger.debug("Move is shorter than one step; nothing to do.")
            if self.queue:
                self.queue.pop(0)
            if self.queue:
                return self._move_for(self.queue[0], wait)
            return True

        # Book the distance actually commanded rather than the requested one,
        # so rounding cannot accumulate into a z that reads higher (further
        # retracted) than the stage really is -- which would hand out travel
        # that does not exist and drive the lens onto the specimen.
        travelled = steps * self.STEPS_DIST
        self.z += travelled if dz > 0 else -travelled

        self.motor.turn_left(steps) if dz < 0 else self.motor.turn_right(steps)

        self.thread = Thread(target=self.tracking_thread)
        self.thread.start()
        if wait:
            self.thread.join()
        return True

    def move_to(self, z, **kw):
        dz = z - self.z
        if dz:
            return self.move_for(dz, **kw)
        return True
   
    def check_limit(self, requested_lenght):
        if not self._referenced:
            logger.warning("Movement refused: the axis is not referenced, so "
                           "its position is unknown.")
            return False

        target = self.z + requested_lenght

        # Retracting is always permitted as long as it stays above the endstop.
        # If the stage is ever left below the boundary -- a shorter lens was
        # selected, steps were lost, the limit changed underneath it -- it must
        # still be able to move away from the specimen. Refusing that would
        # strand the lens at its lowest point with no way back.
        if requested_lenght < 0:
            if target < -1e-9:
                logger.warning(f"Movement refused: {target:.3f}mm is above the endstop.")
                return False
            return True

        if target > self.max_z + 1e-9:
            logger.warning(
                f"Movement refused: {target:.3f}mm is past the safety boundary "
                f"of {self.max_z:.3f}mm (lower limit {self.lower_limit}mm, "
                f"margin {constants.AXIS_SAFETY_MARGIN}mm)."
            )
            return False
        return True
    
    def set_current_limit(self, limit):
        self.lower_limit = limit
        logger.debug(f"new lower limit: {limit}mm -> max_z {self.max_z:.3f}mm")

    def set_rotation_height(self, mm_per_revolution):
        """Set the lead-screw pitch, which fixes the steps-per-mm scaling.

        This must be applied for every device profile, not only the ones that
        differ from the default. Leaving a previous device's value in place
        makes every later move travel by the wrong amount, and booking fewer mm
        than the stage actually covers is exactly how it reaches the base plate
        while the software still believes it is inside the limit: an 8mm screw
        scaled as a 10mm one overshoots by 25%.
        """
        self.DIST_STEPS = 360 * constants.MICROSTEPS / 1.8 / float(mm_per_revolution)
        self.STEPS_DIST = 1 / self.DIST_STEPS
        logger.debug(f"rotation height {mm_per_revolution}mm -> {self.DIST_STEPS:.1f} steps/mm")

    def set_travel_limit(self, travel):
        """Record the device's physical travel, in mm from the endstop.

        Set from the device profile, since the travel differs per model and is
        not derivable from the lens. Without it the lens-derived limit is the
        only guard, and that one can exceed the mechanical range.
        """
        self.travel_limit = travel
        logger.debug(f"new travel limit: {travel}mm -> max_z {self.max_z:.3f}mm")

    def stop(self):
        self.motor.stop()
    
    def set_speed(self, speed: float):
        self.motor.set_speed(speed)
    
    def axis_reference(self, timeout=30):
        """Send reference command and wait for confirmation.

        Returns True on success, False on a firmware abort, timeout or
        missing Arduino.
        """
        if self.queue:
            self.stop()
            sleep(.2)
        if self.arduino is None:
            logger.error("Arduino not connected — cannot reference axis.")
            return False

        self.arduino.write(b'n')

        # The firmware answers with Serial.print and no newline, so a reply can
        # arrive split over several reads. Accumulate instead of testing each
        # read on its own, otherwise a split token wastes the whole timeout.
        buffer = ""
        deadline = time() + timeout
        while time() < deadline:
            try:
                buffer += self.arduino.readline().decode("ascii", "ignore")
            except serial.SerialException as e:
                logger.error(f"Serial error while referencing: {e}")
                return False
            buffer = buffer[-64:]

            if "ref_failed" in buffer:
                logger.error("Axis reference aborted by the controller — the "
                             "endstop switch was never reached. Check the "
                             "endstop switch and the motor wiring.")
                return False

            if "eferenced" in buffer:
                self.z = 0.0
                self._referenced = True
                self.motor.set_speed(constants.LOW_SPEED)
                logger.debug("Axis referenced.")
                return True

        logger.error(f"Axis reference timed out after {timeout}s — "
                     "check endstop switch and motor wiring.")
        return False
    
    def __del__(self):
        # Serial Verbindung schließen
        
        if self.arduino and self.arduino.is_open:
            logger.info("Closed Arduino connection.")
            # self.stop() 
            self.arduino.close()
