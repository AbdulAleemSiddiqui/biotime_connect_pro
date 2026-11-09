# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError


class BiotimePullWizard(models.TransientModel):
    _name = 'biotime.pull.wizard'
    _description = 'Pull BioTime Transactions (Advanced)'

    server_id = fields.Many2one('biotime.server', required=True)
    date_from = fields.Datetime('From', required=True,
                                default=lambda s: fields.Datetime.subtract(fields.Datetime.now(), days=1))
    date_to = fields.Datetime('To', default=fields.Datetime.now)
    device_id = fields.Many2one('biotime.device', domain="[('server_id', '=', server_id)]")
    employee_id = fields.Many2one('hr.employee', domain="[('biotime_code', '!=', False)]")
    transfer = fields.Boolean('Transfer to attendance after pull', default=True)
    background = fields.Boolean('Run in background', default=False,
                                help="Queue the pull instead of waiting for it (recommended for long periods).")

    def action_pull(self):
        self.ensure_one()
        if self.date_to and self.date_to < self.date_from:
            raise UserError(_("'To' must be after 'From'."))
        params = {
            'date_from': self.date_from.strftime('%Y-%m-%d %H:%M:%S'),
            'date_to': self.date_to and self.date_to.strftime('%Y-%m-%d %H:%M:%S'),
            'device_id': self.device_id.id, 'employee_id': self.employee_id.id,
            'transfer': self.transfer,
        }
        # wizard dates are UTC; the server expects device-local, handled by _to_local_str
        if self.background:
            self.env['biotime.job'].enqueue(self.server_id, 'pull', params)
            return self.server_id._notify(_("Pull queued in the background."))
        # synchronous path
        res = self.server_id._pull_transactions(
            date_from=self.date_from, date_to=self.date_to or None,
            device=self.device_id or None, employee=self.employee_id or None)
        msg = _("%s punches read, %s new. ") % (res['seen'], len(res['created']))
        if self.transfer:
            msg += self.server_id._process_pending()
        return self.server_id._notify(msg)
