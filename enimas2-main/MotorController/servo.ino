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

    digitalWrite(EN, HIGH);
    digitalWrite(Y_DIR, HIGH);

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
