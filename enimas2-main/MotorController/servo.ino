#include <Thread.h>

# define EN  8
# define Y_DIR 6
# define Y_STEP 3
# define Y_STOP 10


bool direction;
// 360° : t = 290µs
unsigned int step_delay = 80;
// Anzahl an aufgegebenen Schritten
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
        // n: Anzahl an Schritten, die noch gemacht werden sollen
        motor_step();
        n--; 
    } 
    else if (!n) {
        // Bestätigung für python
        Serial.print("done");
        n = -1;
    }
} 

void motor_step() {
    // selfmade PWM Signal für den Motor mit T = 2*step_delay, duty 50%
    // 1 step = 1,8°/8 = 0,225°
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

void reference() {
    // Aktive Aufträge sollen gestoppt werden
    stop();

    digitalWrite(EN, LOW);

    long previous_step_delay = step_delay;
    step_delay = 100;
    set_direction(HIGH);
    // fährt schnell hoch bis er den schalter erreicht
    for (int i = 0; i < 100; i++) {
        // erstmal 100 langsame schritte
        if (digitalRead(Y_STOP)) motor_step();
    }
    
    step_delay = 40;
    while (digitalRead(Y_STOP)) {
        motor_step();
    }
    delayMicroseconds(1000);


    step_delay = 400;
    set_direction(LOW);
    // fährt langsam wieder runter bis er den schalter nicht mehr berührt
    while (digitalRead(Y_STOP) == LOW) {
        motor_step();
    }
  
    //Bestätigung für python
    Serial.print("referenced");

    step_delay = previous_step_delay;
}

void read_input() {
    while (Serial.available() > 0) {
        message = Serial.readString(); 
        
        if (message.startsWith(String('l'))) {
            // LEFT  zB:message = "l1600"
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
            // REFERENZIEREN
            reference();
        }
        else if (message.startsWith(String("arduino"))) {
            delay(20);
            Serial.print("yes");
        }
    } 
}

void loop() {
    // Motor macht ein schritt wenn nötig
    motor.run();
    
    // Abfrage des Serial Ports für Befehle von Python
    read.run();  
}