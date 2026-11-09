# -*- coding: utf-8 -*-
"""Real-time punch ingest for ZKTeco BioTime push/webhook events.

Configure the webhook in BioTime (or an HTTP listener bridge) to POST JSON to
/biotime/webhook/push. Accepted bodies: a single punch object, a list of
punches, or {'data': [...]}. Punches are deduplicated on the BioTime ID,
staged in biotime.transaction, and paired into hr.attendance immediately.

If any active server has a Webhook Token set, callers must send it in the
'X-Biotime-Token' header; otherwise the first active server is used.
"""
import logging
from datetime import datetime

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

TS_FMT = '%Y-%m-%d %H:%M:%S'


class BiotimeWebhook(http.Controller):

    @http.route('/biotime/webhook/push', type='json', auth='public',
                methods=['POST'], csrf=False)
    def push(self, **kwargs):
        data = request.dispatcher.jsonrequest
        if isinstance(data, dict) and isinstance(data.get('data'), list):
            punches = data['data']
        elif isinstance(data, list):
            punches = data
        elif isinstance(data, dict):
            punches = [data]
        else:
            return {'status': 'error', 'code': 400, 'message': 'Empty payload'}

        Server = request.env['biotime.server'].sudo()
        servers = Server.search([('active', '=', True)])
        secured = servers.filtered('webhook_token')
        if secured:
            token = request.httprequest.headers.get('X-Biotime-Token') or ''
            srv = secured.filtered(lambda s: s.webhook_token == token)[:1]
            if not srv:
                return {'status': 'error', 'code': 401, 'message': 'Invalid webhook token.'}
        else:
            srv = servers[:1]
        if not srv:
            return {'status': 'error', 'code': 409, 'message': 'No active BioTime server configured.'}

        Tx = request.env['biotime.transaction'].sudo()
        created = duplicates = invalid = 0
        for punch in punches:
            try:
                emp_code = str(punch.get('emp_code') or punch.get('pin') or '').strip()
                raw = punch.get('punch_time') or punch.get('time')
                if not emp_code or not raw:
                    invalid += 1
                    continue
                dt = datetime.strptime(str(raw).replace('T', ' ')[:19], TS_FMT)
                bid = str(punch.get('id') or '')
                if bid and Tx.search([('server_id', '=', srv.id), ('biotime_id', '=', bid)], limit=1):
                    duplicates += 1
                    continue
                state = punch.get('punch_state')
                Tx.create({
                    'server_id': srv.id,
                    'biotime_id': bid or 'WH-%s-%s' % (emp_code, raw),
                    'emp_code': emp_code,
                    'punch_time': srv._to_utc(dt),
                    'punch_time_local': dt,
                    'punch_state': str(state) if state is not None else '',
                    'punch_state_display': punch.get('punch_state_display'),
                    'verify_type': str(punch.get('verify_type') or ''),
                    'device_sn': punch.get('terminal_sn'),
                    'terminal_alias': punch.get('terminal_alias'),
                    'area_alias': punch.get('area_alias'),
                })
                created += 1
            except Exception as exc:  # noqa: BLE001 - one bad punch must not fail the batch
                _logger.warning("Webhook punch rejected (emp %s): %s",
                                punch.get('emp_code'), exc)
                invalid += 1
        if created:
            srv._process_pending()
        return {'status': 'ok', 'created': created, 'duplicates': duplicates, 'invalid': invalid}
