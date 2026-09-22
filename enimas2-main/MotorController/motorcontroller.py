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

    def __init__(self, arduino: Serial):
        
        self.arduino = arduino
        self.motor = Motor(self.arduino)
        
        self.z = 0.0
        self._referenced = False
        self.lower_limit = constants.AXIS_LENGHT

        self.queue = []
    
    def tracking_thread(self):
        # hört auf die Antwort vom Arduino nach einem Befehl wie move_up()
        while True:
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

    def move_for(self, dz :float, wait=False):
        # logger.debug(f"request move for {dz}, {self.queue=}")
        if not self.check_limit(dz):
            return False

        if not self.queue:
            # erstes request
            self.queue = [dz]
        else:
            # es sind gerade noch andere prozesse in der warteschlange
            self.queue.append(dz)
            return 
        
        self._move_for(dz, wait)

    def _move_for(self, dz, wait):
        logger.debug(f"move for {dz}, {self.queue=}")

        if not self.check_limit(dz):
            self.queue.clear()
            return False
        
        self.z += dz
        self.motor.turn_left(int(-self.DIST_STEPS * dz)) if dz < 0 else self.motor.turn_right(int(self.DIST_STEPS * dz))

        self.thread = Thread(target=self.tracking_thread)
        self.thread.start()
        if wait:
            self.thread.join()

    def move_to(self, z, **kw):
        dz = z - self.z
        if dz:
            self.move_for(dz, **kw)
   
    def check_limit(self, requested_lenght):
        if self._referenced and (0 <= (self.z + requested_lenght) <= (self.lower_limit-20)):
            return True
        logger.debug(f"Request out of limit: {self.z + requested_lenght}")
        return False
    
    def set_current_limit(self, limit):
        logger.debug(f"new lower limit: {limit}")
        self.lower_limit = limit

    def stop(self):
        self.motor.stop()
    
    def set_speed(self, speed: float):
        self.motor.set_speed(speed)
    
    def axis_reference(self, timeout=30):
        """Send reference command and wait for confirmation.

        Returns True on success, False on timeout or missing Arduino.
        """
        if self.queue:
            self.stop()
            sleep(.2)
        if self.arduino is None:
            logger.error("Arduino not connected — cannot reference axis.")
            return False

        self.arduino.write(b'n')

        deadline = time() + timeout
        while time() < deadline:
            response = self.arduino.readline().decode("ascii")
            if response.endswith("eferenced"):
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
