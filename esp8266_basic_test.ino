/*
  ESP8266 → MD10C Motor Test
  - No WiFi, no UDP, no noise
  - Just tests DIR + PWM output using analogWrite()
*/

const int PWM_PIN = D2;   // choose a PWM-capable pin (D1, D2, D5, D6, D7)
const int DIR_PIN = D3;   // any digital pin

void setup() {
  Serial.begin(115200);
  delay(200);

  pinMode(DIR_PIN, OUTPUT);
  digitalWrite(DIR_PIN, LOW);   // single direction

  pinMode(PWM_PIN, OUTPUT);
  analogWriteRange(1023);       // ESP8266 default, but we set it explicitly
  analogWriteFreq(1000);        // set PWM frequency (1 kHz is safe for testing)

  Serial.println("ESP8266 Motor test starting...");
}

void loop() {
  Serial.println("Speed up...");
  // ramp up 0 → 1023
  for (int p = 0; p <= 255; p++) {
    int duty = map(p, 0, 255, 0, 1023);
    analogWrite(PWM_PIN, duty);
    delay(10);
  }

  delay(500);

  Serial.println("Speed down...");
  // ramp down 1023 → 0
  for (int p = 255; p >= 0; p--) {
    int duty = map(p, 0, 255, 0, 1023);
    analogWrite(PWM_PIN, duty);
    delay(10);
  }

  delay(1000);
}
