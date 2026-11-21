# 🎬 AutoClipper

Aplikasi lokal untuk mengubah video YouTube menjadi klip vertikal (9:16) siap upload ke **Instagram Reels**, **TikTok**, dan **YouTube Shorts** — lengkap dengan **subtitle otomatis**.

## Fitur

- ✂️ **Auto clip** — link YouTube di-paste, AI memilih momen paling menarik lalu memotongnya jadi klip
- 📝 **2 gaya klip**:
  - **Rangkuman** (disarankan) — beberapa bagian terpenting dari sepanjang video digabung jadi 1 klip padat dengan jump cut
  - **Momen tunggal** — 1 potongan kontinyu di momen terbaik
- 🤖 **2 mode pemilihan momen**:
  - **AI (gratis)** — transkrip dianalisis GLM Flash via API Z.ai (model Flash gratis, cukup daftar API key)
  - **Heuristik lokal** — tanpa API key, milih berdasar kepadatan bicara (jalan offline)
- 💡 **Preview video per momen** — lihat & putar tiap momen dulu sebelum memilih
- 📝 **Subtitle otomatis** — hasil transkripsi Whisper (lokal, gratis), 2 style dengan preview:
  - **Karaoke** ✨ — highlight kata per kata ala TikTok
  - **Klasik** — putih bold outline hitam
- 📐 **3 rasio** — 9:16 vertikal (rekomendasi), 1:1 kotak, 16:9 landscape — 720p/1080p
- 🖼️ **2 layout** — **Full + Blur** (gambar utuh, sisi diisi blur, tidak terpotong — rekomendasi) atau Crop tengah
- ⏱️ **Durasi via tombol** — 15 / 30 / 60 detik
- 🏷️ **Caption + hashtag per platform** — pilih Reels/TikTok/Shorts, tiap platform dapat caption sendiri di hasil

## Instalasi

> Butuh **Python 3.10+** dan **ffmpeg** yang sudah ada di PATH.

```bash
cd auto-clipper
pip install -r requirements.txt
```

## Menjalankan

```bash
python run.py
```

Server jalan di [http://127.0.0.1:8765](http://127.0.0.1:8765) dan browser terbuka otomatis.

## Cara Pakai

1. **Tempel link YouTube** → klik **Periksa** (muncul preview judul & thumbnail)
2. **Atur klip**: platform, rasio, gaya klip, layout, resolusi, durasi, subtitle + style
3. Klik **🔍 Cari Momen Terbaik** — unduh → transkripsi → analisis AI → preview momen
4. **Tonton preview tiap momen**, centang yang mau dijadikan klip
5. Klik **✂️ Buat Klip Terpilih** — render (tanpa unduh ulang, memakai cache analisis)
6. Klip jadi! Preview langsung, tombol **Download** per klip, caption per platform bisa dicopy sekali klik

## Kecepatan

- Transkripsi memakai **batched Whisper + VAD** (±4× lebih cepat dari mode biasa); untuk video musik otomatis fallback ke mode klasik
- Beberapa klip dirender **paralel** (4 worker, thread ffmpeg dibagi rata)
- Cache analisis dipakai ulang saat render ulang dengan setting berbeda — tidak perlu unduh/transkrip ulang

## Pengaturan (⚙️ di pojok kanan atas)

| Setting | Keterangan |
|---|---|
| **API key Z.ai** | Daftar gratis di [z.ai](https://z.ai) → API Keys → buat key. Untuk momen terpilih AI. Kosong = heuristik lokal. |
| **Model AI** | Default `glm-4.5-flash` (gratis). Bisa diisi model Flash lain. |
| **Model Whisper** | `tiny` paling cepat; `base` cepat (default); **`small` lebih akurat untuk Bahasa Indonesia**; `medium` paling akurat tapi lambat. |

API key tersimpan di `auto-clipper/config.json` (lokal, tidak dikirim ke mana pun selain Z.ai saat analisis).

## Cara Kerja

```
Link YouTube
   │
   ▼
[1] Unduh video (yt-dlp)              0–30%
[2] Transkripsi (Whisper batched)     30–55%  → kata + timestamp
[3] Analisis momen (GLM Flash /       55–65%  → momen/rangkuman terbaik
    heuristik lokal) + preview video
[4] Render per klip (ffmpeg paralel)  65–100% → layout → burn subtitle → H.264
```

Catatan: model Whisper diunduh sekali saat pertama kali dipakai (~75 MB untuk `base`).

## Troubleshooting

- **"ffmpeg gagal"** — pastikan `ffmpeg` bisa dipanggil dari terminal (`ffmpeg -version`)
- **Unduhan gagal / video tidak ditemukan** — cek link valid, bukan video privat/age-restricted
- **Subtitle tidak muncul** — pastikan video punya audio bicara (bukan musik saja)
- **Analisis AI gagal** — cek API key di ⚙️ Pengaturan; aplikasi otomatis fallback ke heuristik lokal
- **"Video ini hampir tidak memiliki dialog"** — video musik/footage tanpa narasi; coba mode **Momen tunggal**

## Batasan

- Video maksimal 3 jam (makin panjang makin lama transkripsi)
- Hanya mendukung video publik YouTube
- Mode rangkuman butuh audio bicara — video musik murni tidak bisa dirangkum
