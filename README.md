## ⚙️ Panduan Konfigurasi & Menjalankan Sistem

Bagian ini menjelaskan konfigurasi environment, instalasi dependensi, migrasi database, dan langkah menjalankan Pertamina NetShield NOC secara terintegrasi.

---

### 1. Prasyarat Sistem
* Python 3.10+
* Prometheus Server (port default: `9090`)
* Akun Fonnte & Device WhatsApp terhubung
* Ngrok (untuk tunneling webhook Fonnte ke local server)

---

### 2. Konfigurasi Lingkungan (`.env`)

Buat berkas `.env` di direktori *root* proyek dan lengkapi variabel berikut:

```env
# Server Configuration
APP_NAME="Pertamina NetShield NOC"
APP_ENV="development"
PORT=8000
DEBUG=True

# Database Configuration (SQLite)
DATABASE_URL="sqlite:///./netshield.db"

# Prometheus Integration
PROMETHEUS_URL="http://localhost:9090"
POLLER_INTERVAL_SECONDS=5
LATENCY_THRESHOLD_MS=200.0

# Fonnte WhatsApp Gateway
FONNTE_API_TOKEN="your_fonnte_api_token_here"
FONNTE_SEND_URL="[https://api.fonnte.com/send](https://api.fonnte.com/send)"
NOC_WHATSAPP_TARGET="08xxxxxxxxxx" # Nomor PIC atau Target Group WhatsApp
