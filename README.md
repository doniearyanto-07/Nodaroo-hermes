# nodaroo-hermes

Saklar induk buat server Nodaroo (Pterodactyl, egg python generic): `app.py` pasang Node.js,
9Router, dan Hermes Agent ke folder server (tanpa root), lalu jalankan dan jaga keduanya.

- 9Router: dashboard di port server (`SERVER_PORT`), password dari `secrets.env`.
- Hermes: gateway Telegram, model OpenAI-compatible (default DeepSeek via TokenFlow).
  Mau pindah lewat 9Router: set `MODEL_BASE_URL=http://127.0.0.1:<port>/v1` + `MODEL_API_KEY` key 9Router.

Kunci rahasia hanya di `secrets.env` (lihat `secrets.env.example`), tidak pernah di repo.
