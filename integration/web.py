"""HTTP-слой: три адреса. Вся логика в service.py."""
from __future__ import annotations

import hmac

from flask import Flask, jsonify, request

from .service import Service


def create_app(svc: Service) -> Flask:
    app = Flask('integration')
    app.json.ensure_ascii = False
    app.config['MAX_CONTENT_LENGTH'] = 64 * 1024         # заявки и вебхуки маленькие: большое тело сразу отклоняем (413)

    @app.post('/leads')
    def leads():
        got = request.headers.get('Authorization', '')
        if not hmac.compare_digest(got.encode(), f'Bearer {svc.cfg.lead_token}'.encode()):
            return jsonify(error='unauthorized'), 401
        request.get_data()                                   # слишком большое тело даёт 413 до разбора
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error='JSON object expected'), 400
        code, body = svc.accept_lead(data)
        return jsonify(body), code

    @app.post('/webhooks/pay')
    def webhook():
        code, body = svc.handle_webhook(request.get_data(), request.headers.get('X-Demo-Signature'))
        resp = jsonify(body)
        resp.status_code = code
        if code == 503:
            resp.headers['Retry-After'] = '30'
        return resp

    @app.get('/health')
    def health():
        return jsonify(status='ok')                       # цифры по заказам наружу не отдаём

    app.svc = svc
    return app
