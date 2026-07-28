# Render / Heroku-style process types.
#
# `bot`     — the Telegram bot itself. Binds $PORT with a tiny /health
#             endpoint (render_service.py) so Render's Web Service health
#             check passes, then runs the bot via long-polling (or webhook
#             mode if RUN_MODE=webhook — see config/settings.py).
# `webhook` — OPTIONAL second Web Service. Only needed if you use a payment
#             gateway that requires a public HTTPS callback URL (CryptoBot,
#             bKash, Nagad, Cryptomus, NOWPayments, ZiniPay). Deploy as its
#             own Render Web Service with its own $PORT if you need it.
bot: python render_service.py
webhook: python webhook_server.py
