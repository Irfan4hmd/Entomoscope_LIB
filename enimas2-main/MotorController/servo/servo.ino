#include <StaticThreadController.h>
#include <Thread.h>
#include <ThreadController.h>



# define EN  8
# define Y_DIR 6
# define Y_STEP 3
# define Y_STOP 10

// Homing profile.
// The motor cannot jump from standstill straight to the seek speed:
// if it is commanded to anyway, the rotor loses step and the motor only
// buzzes in place. The seek run therefore accelerates over
// REF_RAMP_STEPS up to REF_SEEK_DELAY.
//
// REF_SEEK_DELAY must stay slower than MAX_SPEED from constants.py
// (51 µs = 9800 steps/s), because homing lifts the camera against
// gravity - the direction with the highest torque demand.
// 80 µs = 6250 steps/s is below that, and also below DEFAULT_SPEED.
// If the motor still buzzes at the end of the ramp, increase this value
// (e.g. 120 or 154 like LOW_SPEED).
# define REF_START_DELAY 800     // µs half period at the start of the ramp
# define REF_SEEK_DELAY 80       // µs half period at full seek speed
# define REF_RAMP_STEPS 4000L    // steps for the acceleration
# define REF_BACKOFF_DELAY 400   // µs half period while backing off the switch
// Travel budget of the seek run. Must be chosen so that the run ends before
// the 30 s timeout in axis_reference(), otherwise the motor keeps running
// after Python has already given up. 130000 steps = approx. 24 s.
# define REF_MAX_STEPS 130000L
# define REF_MAX_BACKOFF_STEPS 20000L

// MacroStation turntable, on the X socket of the CNC shield.
// The home switch sits on the X endstop header and marks 0 deg, reached by
// turning clockwise. The end switch sits on the Z endstop header and marks
// 270 deg, reached by turning counter-clockwise. Both switches are wired like
// Y_STOP: normally open to GND, read LOW when pressed.
// On some CNC shield revisions the Z endstop header is routed to D12 instead
// of D11 (GRBL 0.9 spindle swap) - check with a multimeter.
# define ROT_STEP 2
# define ROT_DIR 5
# define ROT_HOME_SW 9
# define ROT_END_SW 11
// DIR level that turns the disk clockwise (seen from the camera). If homing
// turns the disk away from the home switch, swap this to LOW.
# define ROT_CW_LEVEL HIGH
// The firmware counts the turntable position in steps from the home switch,
// counter-clockwise positive. Python derives steps per degree from the count
// between the two switches, so no disk radius or gear ratio is needed here.
# define ROT_START_DELAY 1200    // µs half period at the start of a ramp
# define ROT_MOVE_DELAY 300      // µs half period when cruising to a target
# define ROT_SEEK_DELAY 400      // µs half period when searching for a switch
# define ROT_SLOW_DELAY 1200     // µs half period for the precise switch approach
# define ROT_RAMP_STEPS 800L
// Travel budget of one switch search. Must end before ROTATION_REPLY_TIMEOUT
// in constants.py: 100000 steps at 800 µs per step = 80 s.
# define ROT_MAX_STEPS 100000L
# define ROT_MAX_BACKOFF_STEPS 20000L

// steps from the home switch release point, counter-clockwise positive
long rot_pos = 0;
bool rot_homed = false;


bool direction;
// 360° : t = 290µs
unsigned int step_delay = 80;
// number of steps that have been queued
long n = -1;
String message;

Thread motor = Thread();
Thread read = Thread();

void setup() {
    Serial.begin(115200);
    Serial.setTimeout(5);

    pinMode(EN, OUTPUT);
    pinMode(Y_DIR, OUTPUT);
    pinMode(Y_STEP, OUTPUT);
    pinMode(Y_STOP, INPUT_PULLUP);
    pinMode(ROT_STEP, OUTPUT);
    pinMode(ROT_DIR, OUTPUT);
    pinMode(ROT_HOME_SW, INPUT_PULLUP);
    pinMode(ROT_END_SW, INPUT_PULLUP);

    digitalWrite(EN, HIGH);
    // via set_direction so that the direction flag matches the pin from the
    // start: stop() signs the remaining step count by that flag, so a stop
    // arriving before the first move command would otherwise report it
    // backwards and the host would correct its position the wrong way.
    set_direction(HIGH);

    motor.onRun(move_motor);
    read.onRun(read_input);
}

void move_motor() {
    if (n > 0){
        // n: number of steps that still have to be made
        motor_step();
        n--;
    }
    else if (!n) {
        // confirmation for python
        Serial.print("done");
        n = -1;
    }
}

void motor_step() {
    // selfmade PWM signal for the motor with T = 2*step_delay, duty 50%
    // 1 step = 1.8°/8 = 0.225°
    digitalWrite(Y_STEP, HIGH);
    delayMicroseconds(step_delay);
    digitalWrite(Y_STEP, LOW);
    delayMicroseconds(step_delay);
}

void stop() {
    if (n > 0) {
        if (direction)
            Serial.print(n);
        else
            Serial.print(-1*n);
        n = -1;
    }
}

void set_direction(bool state) {
    direction = state;
    digitalWrite(Y_DIR, state);
}

bool seek_endstop() {
    // Moves towards the switch using an acceleration ramp.
    // Returns false if the switch was not reached within the travel budget,
    // so that the motor does not keep running endlessly.
    for (long i = 0; i < REF_MAX_STEPS; i++) {
        if (!digitalRead(Y_STOP)) return true;

        if (i < REF_RAMP_STEPS) {
            step_delay = REF_START_DELAY - (unsigned int)(
                ((unsigned long)(REF_START_DELAY - REF_SEEK_DELAY) * i) / REF_RAMP_STEPS
            );
        } else {
            step_delay = REF_SEEK_DELAY;
        }
        motor_step();
    }
    return false;
}

bool release_endstop() {
    // Moves back slowly until the switch is no longer touched.
    step_delay = REF_BACKOFF_DELAY;
    for (long i = 0; i < REF_MAX_BACKOFF_STEPS; i++) {
        if (digitalRead(Y_STOP)) return true;
        motor_step();
    }
    return false;
}

void reference() {
    // active jobs are supposed to be stopped
    stop();

    digitalWrite(EN, LOW);

    unsigned int previous_step_delay = step_delay;

    set_direction(HIGH);
    // moves up until it reaches the switch
    bool found = seek_endstop();

    if (!found) {
        step_delay = previous_step_delay;
        // switch not found - Python should be able to abort immediately
        Serial.print("ref_failed");
        return;
    }

    delayMicroseconds(1000);

    set_direction(LOW);
    // moves slowly back down until it no longer touches the switch
    bool released = release_endstop();

    step_delay = previous_step_delay;

    if (!released) {
        Serial.print("ref_failed");
        return;
    }

    //confirmation for python
    Serial.print("referenced");
}

// ---------------------------------------------------------------------------
// MacroStation turntable
//
// The turntable commands block until they finish, like reference(). Python only
// sends them while the Y axis is idle. Every reply is a line starting with
// "rot_", and the ones that end on a position carry it after a colon.
// ---------------------------------------------------------------------------

bool rot_pressed(int pin) {
    return !digitalRead(pin);
}

void rot_set_direction(bool ccw) {
    digitalWrite(ROT_DIR, ccw ? !ROT_CW_LEVEL : ROT_CW_LEVEL);
    delayMicroseconds(5);
}

void rot_step(bool ccw, unsigned int half_period) {
    digitalWrite(ROT_STEP, HIGH);
    delayMicroseconds(half_period);
    digitalWrite(ROT_STEP, LOW);
    delayMicroseconds(half_period);
    rot_pos += ccw ? 1 : -1;
}

unsigned int rot_ramp_delay(long done, long remaining, unsigned int cruise) {
    // accelerates at the start and brakes towards the end of a run
    long k = min(done, remaining);
    if (k >= ROT_RAMP_STEPS) return cruise;
    return ROT_START_DELAY - (unsigned int)(
        ((unsigned long)(ROT_START_DELAY - cruise) * k) / ROT_RAMP_STEPS
    );
}

bool rot_abort_requested() {
    // a "stop" from Python interrupts a running turntable command
    if (Serial.available() > 0 && Serial.peek() == 's') {
        Serial.readString();
        return true;
    }
    return false;
}

// Turns until the switch on `pin` reaches the wanted state.
// Returns 1 when it did, 0 when the travel budget ran out, -1 on a stop.
int rot_run_until(bool ccw, int pin, bool want_pressed, long budget, unsigned int cruise) {
    rot_set_direction(ccw);
    for (long i = 0; i < budget; i++) {
        if (rot_pressed(pin) == want_pressed) return 1;
        if (rot_abort_requested()) return -1;
        rot_step(ccw, rot_ramp_delay(i, budget - i, cruise));
    }
    return rot_pressed(pin) == want_pressed ? 1 : 0;
}

bool rot_check(int result) {
    if (result == 1) return true;
    if (result < 0) {
        Serial.print("rot_stopped:");
        Serial.println(rot_pos);
    } else {
        Serial.println("rot_failed");
    }
    return false;
}

void rot_home() {
    // Defines 0 deg: turns clockwise onto the home switch, then slowly back
    // counter-clockwise until it releases. The release point is the zero, so it
    // is always approached counter-clockwise and slowly, whatever the seek did.
    rot_homed = false;
    digitalWrite(EN, LOW);

    if (rot_pressed(ROT_HOME_SW)) {
        if (!rot_check(rot_run_until(true, ROT_HOME_SW, false, ROT_MAX_BACKOFF_STEPS, ROT_SLOW_DELAY))) return;
    }
    if (!rot_check(rot_run_until(false, ROT_HOME_SW, true, ROT_MAX_STEPS, ROT_SEEK_DELAY))) return;
    delay(50);
    if (!rot_check(rot_run_until(true, ROT_HOME_SW, false, ROT_MAX_BACKOFF_STEPS, ROT_SLOW_DELAY))) return;

    rot_pos = 0;
    rot_homed = true;
    Serial.println("rot_home");
}

void rot_seek_end() {
    // Turns counter-clockwise onto the 270 deg switch and reports how many
    // steps that took. The final approach is slow and counter-clockwise, like
    // the zero, so the backlash of the drive cancels out of the count.
    if (!rot_homed) {
        Serial.println("rot_nohome");
        return;
    }
    digitalWrite(EN, LOW);

    if (!rot_check(rot_run_until(true, ROT_END_SW, true, ROT_MAX_STEPS, ROT_SEEK_DELAY))) return;
    delay(50);
    if (!rot_check(rot_run_until(false, ROT_END_SW, false, ROT_MAX_BACKOFF_STEPS, ROT_SLOW_DELAY))) return;
    delay(50);
    if (!rot_check(rot_run_until(true, ROT_END_SW, true, ROT_MAX_BACKOFF_STEPS, ROT_SLOW_DELAY))) return;

    Serial.print("rot_span:");
    Serial.println(rot_pos);
}

void rot_goto(long target) {
    // Absolute move in steps from home. The switches are the hard ends, so a
    // move never drives further into one, whatever target it was given.
    if (!rot_homed) {
        Serial.println("rot_nohome");
        return;
    }
    digitalWrite(EN, LOW);

    bool ccw = target > rot_pos;
    long steps = labs(target - rot_pos);
    rot_set_direction(ccw);
    for (long i = 0; i < steps; i++) {
        if (rot_pressed(ccw ? ROT_END_SW : ROT_HOME_SW)) {
            Serial.print("rot_limit:");
            Serial.println(rot_pos);
            return;
        }
        if (rot_abort_requested()) {
            Serial.print("rot_stopped:");
            Serial.println(rot_pos);
            return;
        }
        rot_step(ccw, rot_ramp_delay(i, steps - i, ROT_MOVE_DELAY));
    }
    Serial.print("rot_at:");
    Serial.println(rot_pos);
}

void read_input() {
    while (Serial.available() > 0) {
        message = Serial.readString();

        if (message.startsWith(String('l'))) {
            // LEFT  e.g.: message = "l1600"
            n = message.substring(1).toInt();

            set_direction(HIGH);
        }
        else if (message.startsWith(String('r'))) {
            // RIGHT
            n = message.substring(1).toInt();

            set_direction(LOW);
        }
        else if (message.startsWith(String("stop"))) {
            // STOP
            stop();
        }
        else if (message.startsWith(String('d'))) {
            // SPEED
            step_delay = long(111500/message.substring(1).toInt());
        }
        else if (message[0] == 'n') {
            // REFERENCE
            reference();
        }
        else if (message[0] == 'H' || message[0] == 'E' || message[0] == 'G') {
            // TURNTABLE: H = home, E = seek the end switch, G<steps> = go to
            // Upper case, so they cannot collide with the Y axis commands.
            if (n > 0) {
                // the turntable commands block, which would freeze a Y move
                Serial.println("rot_busy");
            }
            else if (message[0] == 'H') rot_home();
            else if (message[0] == 'E') rot_seek_end();
            else rot_goto(message.substring(1).toInt());
        }
        else if (message.startsWith(String("arduino"))) {
            delay(20);
            Serial.print("yes");
        }
    }
}

void loop() {
    // motor makes a step if necessary
    motor.run();

    // polling the serial port for commands from Python
    read.run();
}
