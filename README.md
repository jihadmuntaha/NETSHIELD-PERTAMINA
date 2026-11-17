# NetShield NOC — Dokumentasi Sistem & Panduan Operasional

## 1. Gambaran Umum Sistem

**NetShield NOC** adalah aplikasi pemantauan kesehatan jaringan (*Network Operations Center*) terpusat berbasis web yang dibangun menggunakan **Python FastAPI**. Sistem ini dirancang untuk memantau status keterjangkauan (*availability*) dan telemetri perangkat keras jaringan di seluruh unit operasional **PT Pertamina Patra Niaga Regional Jawa Bagian Tengah** (Kantor Region, Fuel Terminal, Integrated Terminal, dan DPPU) secara *real-time*, serta mengotomatiskan eskalasi tiket insiden dan konfirmasi perbaikan dua arah melalui **WhatsApp Gateway**.

---

## 2. Arsitektur & Fitur Utama

### A. Command Center Portal Hub & Segmentasi Wilayah

* **Portal Terpadu:** Gerbang navigasi utama yang memisahkan akses antara *Dashboard Pemantauan*, *Manajemen Perangkat*, *Log Insiden*, dan *NOC Toolkit*.
* **Segmentasi Wilayah Operasional:** Pengelompokan perangkat berdasarkan unit kerja (misal: Kantor Region Semarang, FT Pengapon, FT Tegal, IT Cilacap, dll.) untuk mempermudah pemetaan lokasi gangguan secara spesifik.

### B. Two-Tier Inspection Architecture (Efisiensi Probing)

* **Tier 1 — Lightweight Availability Check (Interval 5 Detik):** Mesin *background worker* asinkron (`asyncio`) melakukan pengujian konektivitas dasar menggunakan **TCP Socket Probe** dan **ICMP Ping** secara non-blocking.
* **Tier 2 — Deep Hardware Telemetry SNMP v2c (Interval 30 Detik):** Hanya dieksekusi jika status simpul pada Tier 1 terkonfirmasi `ONLINE`. Mengambil metrik kritis perangkat keras (*CPU 1-minute load*, *RAM usage*, *System Uptime*, dan *System Description*). Jika perangkat `DOWN`, query SNMP otomatis di-bypass guna mencegah *event loop bottleneck*.

### C. Automated Incident Lifecycle & Two-Way WhatsApp ACK

* **Penerbitan Tiket Otomatis:** Menerbitkan tiket insiden ke basis data saat perangkat melewati batas toleransi *timeout* serta mengirimkan notifikasi darurat ke nomor teknisi piket melalui **Fonnte WhatsApp Gateway**.
* **Interactive Inbound Webhook (`ACK`):** Teknisi di lapangan dapat merespons alert langsung dari WhatsApp dengan format:
```text
ACK <ID_TIKET> <CATATAN_TINDAKAN>

```


Status tiket di dashboard otomatis berubah dari `OPEN` menjadi `ACKNOWLEDGED` lengkap dengan identitas pengirim dan waktu respons, serta kembali tertutup otomatis (*auto-recovery*) saat simpul pulih.

### D. NOC Toolkit (Web-Based Diagnostic)

* Utilitas diagnostik jaringan berbasis web yang memungkinkan administrator melakukan uji konektivitas *on-demand* (ICMP Ping / port check) langsung dari antarmuka aplikasi tanpa perlu mengakses terminal server.

---

## 3. Panduan Instalasi & Konfigurasi (Deployment)

### Prasyarat Sistem

* Python 3.10 atau versi lebih baru
* Akses jaringan lokal/VPN menuju segmen IP perangkat yang dipantau (port ICMP, TCP target, dan UDP 161 untuk SNMP)
* Akun aktif dan *Device Token* dari **Fonnte WhatsApp Gateway**

### Langkah Instalasi

1. **Clone Repositori & Masuk ke Direktori Proyek**
```bash
git clone https://github.com/jihadmuntaha/NETSHIELD-PERTAMINA
cd netshield-noc

```


2. **Buat & Aktifkan Virtual Environment**
```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

```


3. **Instal Dependensi**
```bash
pip install -r requirements.txt

```


4. **Konfigurasi Environment Variables (`.env`)**
Buat file `.env` pada direktori utama proyek dan sesuaikan parameter berikut:
```env
APP_SECRET_KEY=ganti_dengan_random_secret_key_yang_kuat
DATABASE_URL=sqlite:///./netshield_noc.db

# Konfigurasi Interval Probing (Detik)
PING_INTERVAL_SECONDS=5
SNMP_INTERVAL_SECONDS=30
DOWN_THRESHOLD_COUNT=3

# Integrasi Fonnte WhatsApp Gateway
FONNTE_API_URL=https://api.fonnte.com/send
FONNTE_TOKEN=masukkan_token_fonnte_disini
ALERT_RECIPIENTS=62812xxxxxxxx,62857xxxxxxxx

# Pengaturan Keamanan Sesi & Proxy
HTTPS_ONLY=false

```


5. **Menjalankan Aplikasi**
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000

```


Akses aplikasi melalui peramban di alamat `http://localhost:8000` (atau IP server lokal).

---

## 4. Konfigurasi Webhook Dua Arah (Inbound ACK)

Agar balasan pesan `ACK` dari teknisi dapat diterima oleh server lokal:

1. Pastikan endpoint `/api/webhook/whatsapp` dapat dijangkau oleh server Fonnte (menggunakan *public IP static*, *reverse proxy* Nginx, atau *secure tunnel* seperti Ngrok saat pengujian).
2. Masukkan URL lengkap endpoint (contoh: `https://<domain-anda>/api/webhook/whatsapp`) pada kolom **Webhook URL** di dashboard pengaturan perangkat Fonnte.
3. Pastikan header *reverse-proxy* (`X-Forwarded-Proto`) diteruskan dengan benar agar *session middleware* tetap menjaga kompatibilitas sesi lokal maupun eksternal.

---

## 5. Panduan Operasional Singkat (User Guide)

* **Menambahkan Perangkat Baru:** Masuk ke menu **Devices** $\rightarrow$ klik **Add Device** $\rightarrow$ isi Nama Host, Alamat IP, Pilih Wilayah Operasional, serta centang opsi **Enable SNMP v2c** (isi *Community String*, default: `public`) khusus untuk perangkat *managed*.
* **Menangani Alert Insiden:** Saat menerima pesan WhatsApp gangguan dengan nomor tiket (misal `#104`), balas pesan tersebut dengan mengetik:
`ACK 104 Sedang pengecekan jalur FO di lokasi`
* **Ekspor Laporan Gangguan:** Buka menu **Incident Logs**, pilih rentang tanggal atau status tiket, lalu klik **Export CSV** untuk mengunduh rekapitulasi audit gangguan jaringan.

---

Kalau struktur dokumentasi *handover* di atas sudah pas, kita bisa lanjut merapikan **tabel logbook 90 hari versi sinkron** (memasukkan detail *NOC Toolkit*, segmentasi wilayah, push GitHub, dan *housekeeping* IT), atau kamu ingin menyesuaikan isi dokumen ini dulu?
