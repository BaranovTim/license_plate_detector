/*
 * XIAO ESP32S3 Sense as a USB camera for the Jetson Nano.
 *
 * The board is plugged into the Jetson with a USB-C cable (power + data) and shows up as
 * /dev/ttyACM0. No Wi-Fi is used. The Jetson sends text commands; the board answers:
 *
 *   PING            -> PONG
 *   INFO            -> INFO sensor=OV2640 size=XGA quality=12 psram=8388608
 *   SNAP            -> JPEG <length>\n<length bytes of JPEG>
 *   SIZE <name>     -> OK | ERR ...     (QVGA VGA SVGA XGA HD SXGA UXGA)
 *   QUALITY <0-63>  -> OK               (lower = better quality, bigger files)
 *
 * Any failure is answered with a line starting with ERR.
 *
 * Board settings (Arduino IDE or arduino-cli): XIAO_ESP32S3, PSRAM = OPI PSRAM,
 * USB CDC On Boot = Enabled (default).
 */
#include "esp_camera.h"

// XIAO ESP32S3 Sense camera pins
#define PWDN_GPIO_NUM -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 10
#define SIOD_GPIO_NUM 40
#define SIOC_GPIO_NUM 39
#define Y9_GPIO_NUM 48
#define Y8_GPIO_NUM 11
#define Y7_GPIO_NUM 12
#define Y6_GPIO_NUM 14
#define Y5_GPIO_NUM 16
#define Y4_GPIO_NUM 18
#define Y3_GPIO_NUM 17
#define Y2_GPIO_NUM 15
#define VSYNC_GPIO_NUM 38
#define HREF_GPIO_NUM 47
#define PCLK_GPIO_NUM 13

#define LED_PIN 21  // user LED, active LOW

struct SizeName {
  const char *name;
  framesize_t size;
};
static const SizeName SIZES[] = {
  {"QVGA", FRAMESIZE_QVGA}, {"VGA", FRAMESIZE_VGA}, {"SVGA", FRAMESIZE_SVGA}, {"XGA", FRAMESIZE_XGA},
  {"HD", FRAMESIZE_HD}, {"SXGA", FRAMESIZE_SXGA}, {"UXGA", FRAMESIZE_UXGA},
};

static bool camera_ok = false;
static esp_err_t camera_err = ESP_OK;
static const char *size_name = "XGA";
static int quality = 12;
static String line;

static bool init_camera() {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  // Buffers are allocated for the largest size so SIZE can switch up to UXGA later
  config.frame_size = FRAMESIZE_UXGA;
  config.jpeg_quality = quality;
  config.fb_count = 2;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.grab_mode = CAMERA_GRAB_LATEST;  // always hand out the newest frame

  camera_err = esp_camera_init(&config);
  if (camera_err != ESP_OK) return false;
  esp_camera_sensor_get()->set_framesize(esp_camera_sensor_get(), FRAMESIZE_XGA);
  return true;
}

static const char *sensor_name() {
  switch (esp_camera_sensor_get()->id.PID) {
    case OV2640_PID: return "OV2640";
    case OV3660_PID: return "OV3660";
    case OV5640_PID: return "OV5640";
    default: return "unknown";
  }
}

static void reply_camera_error() {
  Serial.printf("ERR camera_init 0x%x (camera module not attached? PSRAM not enabled?)\n", camera_err);
}

static void snap() {
  if (!camera_ok) return reply_camera_error();
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("ERR capture");
    return;
  }
  digitalWrite(LED_PIN, LOW);
  Serial.printf("JPEG %u\n", (unsigned)fb->len);
  Serial.write(fb->buf, fb->len);
  Serial.flush();
  esp_camera_fb_return(fb);
  digitalWrite(LED_PIN, HIGH);
}

static void handle(String cmd) {
  cmd.trim();
  if (cmd == "PING") {
    Serial.println("PONG");
  } else if (cmd == "SNAP") {
    snap();
  } else if (cmd == "INFO") {
    if (!camera_ok) return reply_camera_error();
    Serial.printf("INFO sensor=%s size=%s quality=%d psram=%u\n", sensor_name(), size_name, quality,
                  (unsigned)ESP.getPsramSize());
  } else if (cmd.startsWith("SIZE ")) {
    if (!camera_ok) return reply_camera_error();
    String name = cmd.substring(5);
    for (const SizeName &s : SIZES) {
      if (name.equalsIgnoreCase(s.name)) {
        esp_camera_sensor_get()->set_framesize(esp_camera_sensor_get(), s.size);
        size_name = s.name;
        // Drop frames captured at the old size
        for (int i = 0; i < 2; i++) {
          camera_fb_t *fb = esp_camera_fb_get();
          if (fb) esp_camera_fb_return(fb);
        }
        Serial.println("OK");
        return;
      }
    }
    Serial.println("ERR unknown size");
  } else if (cmd.startsWith("QUALITY ")) {
    if (!camera_ok) return reply_camera_error();
    quality = constrain(cmd.substring(8).toInt(), 4, 63);
    esp_camera_sensor_get()->set_quality(esp_camera_sensor_get(), quality);
    Serial.println("OK");
  } else if (cmd.length()) {
    Serial.println("ERR unknown command");
  }
}

void setup() {
  Serial.setTxBufferSize(16384);
  Serial.begin(115200);  // USB CDC: the baud rate is ignored, it runs at full USB speed
  Serial.setTxTimeoutMs(2000);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);

  camera_ok = psramFound() && init_camera();
  if (!psramFound()) camera_err = ESP_ERR_NO_MEM;
  if (camera_ok) {
    Serial.printf("READY sensor=%s size=%s\n", sensor_name(), size_name);
  } else {
    reply_camera_error();
  }
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      handle(line);
      line = "";
    } else if (line.length() < 64) {
      line += c;
    }
  }
  if (!camera_ok) {  // slow blink = camera problem
    digitalWrite(LED_PIN, (millis() / 500) % 2);
  }
  delay(2);
}
