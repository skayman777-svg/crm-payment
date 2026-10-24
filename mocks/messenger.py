"""Учебный сервис уведомлений (вместо почты и Telegram): складывает сообщения в список."""
from __future__ import annotations

import itertools
import logging
import threading

from flask import Flask, jsonify, request

from ._ui import short

log = logging.getLogger('msg')


def create_app():
    app = Flask('msg-mock')
    app.json.ensure_ascii = False
    state = {'messages': [], 'fail': 0, 'seq': itertools.count(1)}
    lock = threading.Lock()

    @app.post('/send')
    def send():
        body = request.get_json(silent=True) or {}
        with lock:
            if state['fail'] > 0:
                state['fail'] -= 1
                log.warning('POST /send → 503 (имитация сбоя)')
                return jsonify(error='temporarily unavailable'), 503
            if not all(body.get(k) for k in ('channel', 'to', 'text')):
                return jsonify(error='channel, to, text are required'), 422
            msg = {'id': next(state['seq']), **{k: body[k] for k in ('channel', 'to', 'text')}}
            state['messages'].append(msg)
        log.info('%s → %s: «%s»', body['channel'], body['to'], short(body['text'].splitlines()[0], 90))
        return jsonify(id=msg['id']), 201

    @app.get('/messages')
    def messages():
        return jsonify(state['messages'])

    @app.post('/_sim/fail')
    def sim_fail():
        with lock:
            state['fail'] = int((request.get_json(silent=True) or {}).get('count', 0))
        return jsonify(ok=True)

    app.msg_state = state
    return app
