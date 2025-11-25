/*
  ESP32 → MD10C Motor Test
  - No WiFi, no UDP, no noise
  - Just tests DIR + PWM output
*/

const int PWM_PIN = 18;   // PWM → MD10C PWM
const int DIR_PIN = 19;   // DIR → MD10C DIR

const int PWM_FREQ = 10000;  // 10 kHz
const int PWM_RES  = 8;      // 0–255

void setup() {
  Serial.begin(115200);
  delay(200);

  pinMode(DIR_PIN, OUTPUT);
  digitalWrite(DIR_PIN, LOW);   // single direction

  // Attach LEDC PWM
  ledcAttach(PWM_PIN, PWM_FREQ, PWM_RES);
  ledcWrite(PWM_PIN, 0);

  Serial.println("Motor test starting...");
}

void loop() {
  Serial.println("Speed up...");
  // ramp up
  for (int p = 0; p <= 255; p++) {
    ledcWrite(PWM_PIN, p);
    delay(10);
  }

  delay(500);

  Serial.println("Speed down...");
  // ramp down
  for (int p = 255; p >= 0; p--) {
    ledcWrite(PWM_PIN, p);
    delay(10);
  }

  delay(1000); // pause before repeating
}
