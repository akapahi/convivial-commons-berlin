/*
  ESP32 Local FastLED Noise Vibration Controller
  - Uses FastLED's inoise8() for 1D noise
  - Start: send single byte '1' (0x31) or 0x01
  - Stop:  send single byte '0' (0x30) or 0x00
  - Auto-stop after TIMEOUT_MS (30s)
  - LEDC API used: ledcAttach(pin,freq,res) and ledcWrite(pin,duty)
  - Requires FastLED library
*/

#include <WiFi.h>
#include <WiFiUdp.h>
#include <FastLED.h>   // for inoise8()

// ---------------- CONFIG ----------------
const char* WIFI_SSID = "Internet";
const char* WIFI_PASS = "password";

const unsigned int UDP_PORT = 6454;      // command port

// Motor driver pins (MD10C)
const int PWM_PIN = 18;     // PWM -> MD10C PWM input
const int DIR_PIN = 19;     // DIR -> MD10C DIR input (keeps LOW for single-direction)

// PWM settings (MD10C supports up to 20kHz)
const int PWM_FREQ = 10000;
const int PWM_RES  = 8;       // 0..255

// Noise & runtime settings
const int SAMPLE_HZ = 40;           // updates per second
const int SAMPLE_INTERVAL_MS = 1000 / SAMPLE_HZ;
const int DEFAULT_AMPLITUDE = 200;  // 0..255 amplitude for final PWM
const uint32_t NOISE_STEP_DEFAULT = 2000; // how much the noise 'position' advances each sample (higher = faster)
const float SMOOTH_ALPHA = 0.18f;   // exponential smoothing factor (0..1)
const unsigned long TIMEOUT_MS = 30000UL; // 30 seconds auto-stop

// ----------------------------------------

WiFiUDP Udp;
uint8_t udpBuf[256];

bool motorRunning = false;
int currentPwm = 0;
unsigned long lastCmdMillis = 0;
unsigned long lastSampleMillis = 0;

// FastLED noise position (32-bit)
uint32_t noisePos = 0;
uint32_t noiseStep = NOISE_STEP_DEFAULT; // tweak to control speed

// Helper: write PWM via pin-based LEDC API
void writePwm(int val) {
  val = constrain(val, 0, 255);
  currentPwm = val;
  ledcWrite(PWM_PIN, currentPwm);
}

void setMotorOff() {
  motorRunning = false;
  writePwm(0);
  digitalWrite(DIR_PIN, LOW);
  Serial.println("Motor OFF");
}

void startMotor() {
  if (!motorRunning) {
    Serial.println("Motor START requested -> starting local noise generator");
  }
  motorRunning = true;
  noisePos = random(); // small random start offset for variety across devices
  lastSampleMillis = millis();
  lastCmdMillis = millis();
}

void handleSimpleCmd(uint8_t *buf, int len, IPAddress from, uint16_t port) {
  if (len < 1) return;
  uint8_t b = buf[0];
  if (b == '1' || b == 1) {
    Serial.printf("CMD from %s:%u -> START\n", from.toString().c_str(), port);
    startMotor();
  } else if (b == '0' || b == 0) {
    Serial.printf("CMD from %s:%u -> STOP\n", from.toString().c_str(), port);
    setMotorOff();
  } else {
    Serial.printf("Unknown CMD (len=%d first=0x%02X) from %s\n", len, b, from.toString().c_str());
  }
}

void setup() {
  Serial.begin(115200);
  delay(100);
  Serial.println("\n=== ESP32 FastLED Noise Vibration Controller ===");

  // pins
  pinMode(DIR_PIN, OUTPUT);
  digitalWrite(DIR_PIN, LOW);

  // attach PWM using pin-based API
  ledcAttach(PWM_PIN, PWM_FREQ, PWM_RES);
  ledcWrite(PWM_PIN, 0);

  // motor off initially
  setMotorOff();

  // WiFi
  Serial.printf("Connecting to WiFi '%s' ...\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    Serial.print(".");
    delay(300);
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nWiFi connected. IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\nWiFi not connected (continuing; UDP will only work if you later join same LAN).");
  }

  Udp.begin(UDP_PORT);
  Serial.printf("Listening for simple UDP commands on port %u\n", UDP_PORT);
  Serial.println("Send single byte '1' to start, '0' to stop. Broadcast OK.");
  randomSeed(analogRead(0));
}

void loop() {
  // handle UDP commands
  int packetSize = Udp.parsePacket();
  if (packetSize > 0) {
    if (packetSize > (int)sizeof(udpBuf)) packetSize = sizeof(udpBuf);
    IPAddress remote = Udp.remoteIP();
    uint16_t rport = Udp.remotePort();
    Udp.read(udpBuf, packetSize);
    if (packetSize == 1) {
      handleSimpleCmd(udpBuf, packetSize, remote, rport);
      lastCmdMillis = millis();
    } else {
      Serial.printf("Ignored UDP packet len=%d from %s\n", packetSize, remote.toString().c_str());
    } 
  }

  unsigned long now = millis();

  // watchdog timeout
  if (motorRunning && (now - lastCmdMillis >= TIMEOUT_MS)) {
    Serial.println("Watchdog timeout reached -> stopping motor");
    setMotorOff();
  }

  // sample noise if running
  if (motorRunning && (now - lastSampleMillis >= (unsigned long)SAMPLE_INTERVAL_MS)) {

    // advance noise position
    noisePos += noiseStep; // 32-bit wrap is fine

    // FastLED inoise8 returns 0..255
    uint8_t noiseVal = inoise8(noisePos);

    // scale noiseVal (0..255) to amplitude 0..DEFAULT_AMPLITUDE
    int target = (int)((noiseVal / 255.0f) * (float)DEFAULT_AMPLITUDE);

    // optional smoothing (exponential)
    int smoothed = (int)round(SMOOTH_ALPHA * (float)target + (1.0f - SMOOTH_ALPHA) * (float)currentPwm);

    // single-direction — keep DIR low
    digitalWrite(DIR_PIN, LOW);

    writePwm(smoothed);

    lastSampleMillis = now;
  }

  delay(1);
}
