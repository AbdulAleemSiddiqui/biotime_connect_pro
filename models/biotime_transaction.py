# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class BiotimeTransaction(models.Model):
    _name = 'biotime.transaction'
    _description = 'BioTime Raw Punch'
    _order = 'punch_time desc, id desc'
    _rec_name = 'emp_code'

    server_id = fields.Many2one('biotime.server', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='server_id.company_id', store=True)
    biotime_id = fields.Char('BioTime ID', required=True, index=True)
    emp_code = fields.Char('Employee Code', index=True)
    emp_name = fields.Char('Name in BioTime')
    employee_id = fields.Many2one('hr.employee', index=True)
    punch_time = fields.Datetime(required=True, index=True)
    punch_time_local = fields.Datetime('Raw Device Time',
                                       help="Punch time exactly as the device reported it.")
    punch_state = fields.Char('Raw Punch State')
    punch_state_display = fields.Char('Punch State')
    verify_type = fields.Char('Verify Type')
    device_id = fields.Many2one('biotime.device', ondelete='set null')
    device_sn = fields.Char('Device SN')
    terminal_alias = fields.Char('Terminal')
    area_alias = fields.Char('Area')
    state = fields.Selection([
        ('pending', 'Pending'), ('processed', 'Processed'),
        ('error', 'Error'), ('skipped', 'Skipped')], default='pending', index=True)
    attendance_id = fields.Many2one('hr.attendance', ondelete='set null')
    error = fields.Text()
    retry_count = fields.Integer()

    _biotime_unique = models.Constraint(
        'UNIQUE(server_id, biotime_id)', 'This punch was already imported.')

    # ------------------------------------------------------------------
    # buttons
    # ------------------------------------------------------------------
    def action_process(self):
        res = self.sudo().filtered(lambda t: t.state in ('pending', 'error'))._process()
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('BioTime'), 'type': 'success' if not res['err'] else 'warning',
            'message': _("%(ok)s transferred, %(skip)s skipped, %(err)s errors.") % res}}

    def action_skip(self):
        self.write({'state': 'skipped', 'error': _('Skipped manually.')})

    def action_retry(self):
        self.filtered(lambda t: t.state in ('error', 'skipped')).write(
            {'state': 'pending', 'error': False, 'retry_count': 0})

    # ------------------------------------------------------------------
    # processing engine
    # ------------------------------------------------------------------
    def _process(self):
        """Turn punches into hr.attendance, strictly chronological per employee.

        Servers in 'First & Last' mode are routed to day-level pairing instead."""
        res = {'ok': 0, 'skip': 0, 'err': 0}
        for srv in self.mapped('server_id'):
            part = self.filtered(lambda t: t.server_id == srv)
            if srv.pairing_mode == 'first_last':
                day_res = srv._process_first_last(part)
                for key in res:
                    res[key] += day_res[key]
                continue
            for tx in part.sorted(lambda t: (t.punch_time, t.id)):
                try:
                    with self.env.cr.savepoint():
                        outcome = tx._process_one()
                    res[outcome] += 1
                except Exception as exc:  # noqa: BLE001 - record per-punch failure, keep going
                    msg = exc.args[0] if isinstance(exc, UserError) and exc.args else str(exc)
                    tx.write({'state': 'error', 'error': msg, 'retry_count': tx.retry_count + 1})
                    res['err'] += 1
        return res

    def _match_employee(self):
        self.ensure_one()
        return self.server_id._find_employee(self.emp_code)

    def _employee_tz_time(self, emp):
        """Re-interpret the raw device time in the employee's own timezone (returns naive UTC)."""
        try:
            local = pytz.timezone(emp.tz).localize(self.punch_time_local, is_dst=False)
        except Exception:  # noqa: BLE001 - unknown timezone must not block the punch
            return False
        utc = local.astimezone(pytz.utc).replace(tzinfo=None)
        self.write({'punch_time': utc})
        return utc

    def _process_one(self):
        self.ensure_one()
        srv = self.server_id
        emp = self.employee_id or self._match_employee()
        if not emp:
            raise UserError(_("No Odoo employee matches BioTime code '%s'.") % self.emp_code)
        self.employee_id = emp
        # employee in a different timezone than the server: re-derive the UTC punch time
        punch_time = self.punch_time
        if self.punch_time_local and emp.tz and emp.tz != srv.timezone:
            punch_time = self._employee_tz_time(emp) or punch_time
        Att = self.env['hr.attendance']
        open_att = Att.search([('employee_id', '=', emp.id), ('check_out', '=', False)],
                              order='check_in desc', limit=1)

        # stale open attendance (forgot to check out): close it, flag it, start fresh
        if open_att and srv.max_shift_hours and \
                punch_time - open_att.check_in > timedelta(hours=srv.max_shift_hours):
            close_at = min(open_att.check_in + timedelta(hours=srv.default_shift_hours),
                           punch_time - timedelta(seconds=1))
            if close_at > open_att.check_in:
                open_att.write({'check_out': close_at, 'bt_missing_checkout': True})
                open_att = Att

        if srv.pairing_mode == 'alternating':
            direction = 'out' if open_att else 'in'
        elif self.punch_state == (srv.punch_state_in or '0'):
            direction = 'in'
        elif self.punch_state == (srv.punch_state_out or '1'):
            direction = 'out'
        else:
            return self._skip(_("Punch state '%s' is not mapped to IN or OUT.") % self.punch_state)

        gap = timedelta(minutes=srv.min_punch_gap or 0)
        if direction == 'in':
            if open_att:
                return self._skip(_("Already checked in at %s.") % open_att.check_in)
            try:
                with self.env.cr.savepoint():
                    att = Att.create({'employee_id': emp.id, 'check_in': punch_time,
                                      'bt_server_id': srv.id})
            except Exception:  # noqa: BLE001 - conflicting open attendance (out-of-order punch)
                return self._skip(_(
                    "Conflicting open attendance for this employee "
                    "(out-of-order or duplicate punch)."))
        else:
            if not open_att:
                return self._skip(_(
                    "Orphan check-out: no open check-in to match. "
                    "The matching check-in was probably lost or not synced."))
            if punch_time <= open_att.check_in + gap:
                return self._skip(_("Double punch ignored."))
            open_att.write({'check_out': punch_time})
            att = open_att
        self.write({'state': 'processed', 'attendance_id': att.id, 'error': False})
        return 'ok'

    def _skip(self, reason):
        self.write({'state': 'skipped', 'error': reason})
        return 'skip'

    @api.model
    def _cron_process(self):
        """Respect each server's max_retries for error transactions."""
        todo = self.env['biotime.transaction']
        for srv in self.env['biotime.server'].search([]):
            todo |= self.search([
                ('server_id', '=', srv.id),
                '|', ('state', '=', 'pending'),
                '&', ('state', '=', 'error'), ('retry_count', '<', srv.max_retries)],
                order='punch_time, id', limit=5000)
        todo._process()
