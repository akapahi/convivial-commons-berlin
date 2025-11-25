/*
  ESP32 → MD10C Motor Test with Perlin Noise
  Using ONLY the pin-based LEDC API:
      ledcAttach(pin, freq, resolution)
      ledcWrite(pin, duty)
  No WiFi, no networking, no extras.
*/

#include <FastLED.h>   // for inoise8()

// Motor pins
const int PWM_PIN = 18;    // MD10C PWM input
const int DIR_PIN = 19;    // MD10C DIR input

// PWM settings
const int PWM_FREQ = 10000;   // 10 kHz
const int PWM_RES  = 8;       // 0–255

// Noise parameters
const int SAMPLE_HZ = 40;                       // updates per second
const int SAMPLE_INTERVAL_MS = 1000 / SAMPLE_HZ;
const int DEFAULT_AMPLITUDE = 200;              // 0–255 max power
const uint32_t NOISE_STEP = 3000;               // higher = faster movement
const float SMOOTH_ALPHA = 0.20f;               // smoothing (0 = none)

// State
unsigned long lastSample = 0;
uint32_t noisePos = 0;
int currentPwm = 0;

// Write PWM using pin-based LEDC API
void writePwm(int val) {
  val = constrain(val, 0, 255);
  currentPwm = val;
  ledcWrite(PWM_PIN, val);
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n=== ESP32 Motor Noise Test (pin-based LEDC API) ===");

  pinMode(DIR_PIN, OUTPUT);
  digitalWrite(DIR_PIN, LOW);       // single direction

  // YOUR ESP32 LEDC API:
  ledcAttach(PWM_PIN, PWM_FREQ, PWM_RES);
  writePwm(0);

  noisePos = 0;
  lastSample = millis();

  Serial.println("Motor ready. Running noise modulation...");
}

void loop() {
  unsigned long now = millis();
  if (now - lastSample >= SAMPLE_INTERVAL_MS) {
    lastSample = now;

    // Move noise position
    noisePos += NOISE_STEP;

    // Perlin noise (0–255)
    uint8_t n = inoise8(noisePos);

    // Scale noise to motor amplitude
    int target = (int)((n / 255.0f) * DEFAULT_AMPLITUDE);

    // Smooth movement
    int smoothed = (int)(SMOOTH_ALPHA * target + (1 - SMOOTH_ALPHA) * currentPwm);

    // Apply PWM
    writePwm(smoothed);

    // Debug every 1 second
    static unsigned long lastPrint = 0;
    if (now - lastPrint >= 1000) {
      lastPrint = now;
      Serial.printf("Noise=%3u  Target=%3d  PWM=%3d\n", n, target, smoothed);
    }
  }

  delay(1);
}
