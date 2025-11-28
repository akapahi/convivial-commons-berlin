/*
  ESP32 Art-Net Vibration Motor Controller (debounced OFF)

  Protocol:
    - Listens on Art-Net universe 0 via ArtnetWifi
    - Uses ONE DMX channel (MOTOR_CHANNEL) per ESP32

    DMX on MOTOR_CHANNEL:
      0   -> request OFF, but only if seen 5 frames in a row
      1–255 -> ON, amplitude scales with the value

  Behavior:
    - If value > 0: motor runs, vibration amplitude from DMX
    - If value == 0:
        - Count how many consecutive 0 frames we see
        - After 5 consecutive zeros -> motor OFF
        - Occasional zeros are ignored (no flicker)
    - If DMX dies completely: DMX_TIMEOUT_MS kills the motor as a failsafe

  Motion:
    - Uses Perlin noise (inoise8) to generate a vibrating PWM pattern
*/

#include <WiFi.h>
#include <ArtnetWifi.h>
#include <FastLED.h>   // for inoise8()

// ---------------- CONFIG ----------------
const char* WIFI_SSID = "Internet";
const char* WIFI_PASS = "password";

// Art-Net / DMX config
ArtnetWifi artnet;
const uint16_t ARTNET_UNIVERSE = 0;      // matches server universe 0

// This ESP's motor channel in the DMX frame (1..512).
// Set this to match the dramatization server's ACTOR_CONFIG motor_channel.
// e.g. lake motor_channel=2 -> MOTOR_CHANNEL=2
const uint16_t MOTOR_CHANNEL = 2;

// Motor driver pins
const int PWM_PIN = 18;
const int DIR_PIN = 19;

// PWM settings
const int PWM_FREQ = 10000;
const int PWM_RES  = 8;  // 0..255

// Noise & runtime
const int SAMPLE_HZ = 40;
const int SAMPLE_INTERVAL_MS = 1000 / SAMPLE_HZ;
const int DEFAULT_AMPLITUDE = 230;   // maximum vibration amplitude
const uint32_t NOISE_STEP_DEFAULT = 20000;
const float SMOOTH_ALPHA = 0.28f;

// Safety: global Art-Net timeout (if DMX dies completely)
const unsigned long DMX_TIMEOUT_MS = 30000UL;

// Debounce OFF: how many consecutive zero frames we need to see
const uint8_t ZERO_FRAMES_TO_OFF = 5;

// ----------------------------------------

// State
bool motorRunning       = false;
int currentPwm          = 0;
unsigned long lastDmxMillis    = 0;
unsigned long lastSampleMillis = 0;

// DMX-driven amplitude: 0..255 from DMX when >0
uint8_t motorLevel      = 0;

// Noise state
uint32_t noisePos  = 0;
uint32_t noiseStep = NOISE_STEP_DEFAULT;

// Zero-frame debounce
uint8_t zeroFrameCount = 0;


// Helpers
void writePwm(int val) {
  val = constrain(val, 0, 255);
  currentPwm = val;
  ledcWrite(PWM_PIN, currentPwm);
}

void setMotorOff() {
  motorRunning = false;
  motorLevel = 0;
  writePwm(0);
  digitalWrite(DIR_PIN, LOW);
  Serial.println("Motor OFF");
}

void startMotor() {
  if (!motorRunning) {
    Serial.println("Motor START -> local noise generator");
  }
  motorRunning = true;
  noisePos = random();
  lastSampleMillis = millis();
}

// ---------- Art-Net callback ----------
// Called on ArtDMX frames
void onDmxFrame(uint16_t universe, uint16_t length, uint8_t sequence, uint8_t* data) {
  if (universe != ARTNET_UNIVERSE) return;

  // Need the motor channel to be within DMX data length
  if (MOTOR_CHANNEL < 1 || MOTOR_CHANNEL > length) return;

  uint8_t val = data[MOTOR_CHANNEL - 1];   // 0..255
  lastDmxMillis = millis();

  if (val == 0) {
    // Debounced OFF: only stop if we get N consecutive zero frames
    zeroFrameCount++;
    if (zeroFrameCount >= ZERO_FRAMES_TO_OFF) {
      if (motorRunning) {
        Serial.printf("DMX: MOTOR_CHANNEL=%u -> 0 for %u frames, turning OFF\n",
                      MOTOR_CHANNEL, ZERO_FRAMES_TO_OFF);
        setMotorOff();
      } else {
        // already off, nothing to do
        // Serial.printf("DMX: MOTOR_CHANNEL=%u -> 0 (motor already off)\n", MOTOR_CHANNEL);
      }
      zeroFrameCount = 0;  // reset the counter once we've acted
    } else {
      // Optional debug if you want to see ignored zeros:
      // Serial.printf("DMX: MOTOR_CHANNEL=%u -> 0 (ignored, count=%u)\n",
      //               MOTOR_CHANNEL, zeroFrameCount);
    }
    return;
  }

  // Non-zero → ON, amplitude scales with val
  zeroFrameCount = 0;  // reset off debounce
  motorLevel = val;
  Serial.printf("DMX: MOTOR_CHANNEL=%u=%u (motorLevel)\n", MOTOR_CHANNEL, motorLevel);

  if (!motorRunning) {
    startMotor();
  }
}

bool connectWifi() {
  Serial.printf("Connecting to WiFi '%s' ...\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 50) {
    delay(200);
    Serial.print(".");
    tries++;
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("WiFi connected. IP: ");
    Serial.println(WiFi.localIP());
    WiFi.setSleep(false);   // disable power-save so we don't miss packets
    return true;
  } else {
    Serial.println("WiFi NOT connected.");
    return false;
  }
}

void setup() {
  Serial.begin(115200);
  delay(100);
  Serial.println("\n=== ESP32 Art-Net Debounced Vibration Controller ===");

  pinMode(DIR_PIN, OUTPUT);
  digitalWrite(DIR_PIN, LOW);

  // If ledcAttach(...) gives issues, switch to ledcSetup/ledcAttachPin pattern.
  ledcAttach(PWM_PIN, PWM_FREQ, PWM_RES);
  ledcWrite(PWM_PIN, 0);

  setMotorOff();

  connectWifi();

  artnet.begin();              // listen on UDP 6454
  artnet.setArtDmxCallback(onDmxFrame);

  randomSeed(analogRead(0));
  Serial.printf("Listening on universe %u, MOTOR_CHANNEL=%u\n",
                ARTNET_UNIVERSE, MOTOR_CHANNEL);
}

void loop() {
  // Process incoming Art-Net packets
  artnet.read();

  unsigned long now = millis();

  // Global DMX timeout failsafe
  if (motorRunning && (now - lastDmxMillis >= DMX_TIMEOUT_MS)) {
    Serial.println("DMX timeout -> stopping motor");
    setMotorOff();
    zeroFrameCount = 0;
  }

  // Noise / PWM update
  if (motorRunning && (now - lastSampleMillis >= (unsigned long)SAMPLE_INTERVAL_MS)) {
    noisePos += noiseStep;
    uint8_t noiseVal = inoise8(noisePos); // 0..255

    // Scale amplitude by DMX level:
    uint8_t effectiveAmp = map(motorLevel, 0, 255, 0, DEFAULT_AMPLITUDE);
    int target = (int)((noiseVal * (uint16_t)effectiveAmp) / 255);

    int smoothed = (int)round(SMOOTH_ALPHA * (float)target +
                              (1.0f - SMOOTH_ALPHA) * (float)currentPwm);

    digitalWrite(DIR_PIN, LOW);
    writePwm(smoothed);

    lastSampleMillis = now;
  }

  delay(1);
}

