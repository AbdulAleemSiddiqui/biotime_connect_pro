# -*- coding: utf-8 -*-
from datetime import timedelta

import pytz

from odoo import api, fields, models


class HrAttendance(models.Model):
    _inherit = 'hr.attendance'

    bt_server_id = fields.Many2one('biotime.server', 'BioTime Server', readonly=True, index=True)
    bt_transaction_ids = fields.One2many('biotime.transaction', 'attendance_id', 'BioTime Punches')
    bt_missing_checkout = fields.Boolean('Missing Check-out', readonly=True,
                                         help="Closed automatically because the employee never checked out.")
    bt_status = fields.Selection([
        ('on_time', 'On Time'), ('late', 'Late In'), ('excused', 'Excused'),
        ('missing_checkout', 'Missing Check-out'), ('no_schedule', 'No Schedule')],
        string='BioTime Status', compute='_compute_bt_metrics', store=True)
    late_reason_ids = fields.One2many('biotime.late.reason', 'attendance_id', 'Late Reasons')
    bt_reason_approved = fields.Boolean('Late Reason Approved', compute='_compute_bt_reason_approved', store=True)
    bt_late_minutes = fields.Integer('Late (min)', compute='_compute_bt_metrics', store=True)
    bt_early_out_minutes = fields.Integer('Early Out (min)', compute='_compute_bt_metrics', store=True)
    bt_net_hours = fields.Float('Net Hours', compute='_compute_bt_metrics', store=True,
                                help="Worked hours minus the configured break.")
    bt_overtime_hours = fields.Float('Overtime (h)', compute='_compute_bt_metrics', store=True)

    # ------------------------------------------------------------------
    def _bt_shift(self):
        """Scheduled (start, stop) in naive UTC for this attendance's day, or None.
        Re-evaluated automatically when the employee's working schedule changes."""
        self.ensure_one()
        emp = self.employee_id
        cal = emp.resource_calendar_id or emp.company_id.resource_calendar_id
        if not cal or not emp.resource_id:
            return None
        tz = pytz.timezone(emp.tz or cal.tz or 'UTC')
        ci = pytz.utc.localize(self.check_in).astimezone(tz)
        day0 = ci.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            res = cal._work_intervals_batch(
                day0 - timedelta(days=1), day0 + timedelta(days=2),
                resources=emp.resource_id, tz=tz, compute_leaves=False)
            intervals = [(s, e) for s, e, _r in res[emp.resource_id.id]]
        except Exception:  # noqa: BLE001 - schedule API differences must not block attendance
            return None
        if not intervals:
            return None
        nearest = min(intervals, key=lambda i: abs((i[0] - ci).total_seconds()))
        if abs((nearest[0] - ci).total_seconds()) > 12 * 3600:
            return None
        day = nearest[0].astimezone(tz).date()
        same_day = [i for i in intervals if i[0].astimezone(tz).date() == day]  # lunch splits intervals
        to_utc = lambda d: d.astimezone(pytz.utc).replace(tzinfo=None)
        return to_utc(min(i[0] for i in same_day)), to_utc(max(i[1] for i in same_day))

    @api.depends('late_reason_ids.state')
    def _compute_bt_reason_approved(self):
        for att in self:
            att.bt_reason_approved = any(r.state == 'approved' for r in att.late_reason_ids)

    @api.depends('check_in', 'check_out', 'employee_id', 'employee_id.resource_calendar_id',
                 'bt_server_id', 'bt_missing_checkout', 'worked_hours', 'bt_reason_approved',
                 'bt_server_id.late_tolerance', 'bt_server_id.early_out_tolerance',
                 'bt_server_id.break_minutes', 'bt_server_id.break_after_hours',
                 'bt_server_id.overtime_threshold')
    def _compute_bt_metrics(self):
        for att in self:
            vals = {'bt_status': False, 'bt_late_minutes': 0, 'bt_early_out_minutes': 0,
                    'bt_net_hours': 0.0, 'bt_overtime_hours': 0.0}
            srv = att.bt_server_id
            if srv and att.check_in:
                shift = att._bt_shift()
                status = 'no_schedule'
                if shift:
                    start, stop = shift
                    late = (att.check_in - start).total_seconds() / 60.0 - (srv.late_tolerance or 0)
                    vals['bt_late_minutes'] = max(0, int(round(late)))
                    status = 'late' if vals['bt_late_minutes'] else 'on_time'
                    if att.bt_reason_approved:
                        status = 'excused'
                    if att.check_out:
                        early = (stop - att.check_out).total_seconds() / 60.0 - (srv.early_out_tolerance or 0)
                        vals['bt_early_out_minutes'] = max(0, int(round(early)))
                if att.bt_missing_checkout:
                    status = 'missing_checkout'
                vals['bt_status'] = status
                if att.check_out:
                    worked = att.worked_hours or 0.0
                    if worked >= (srv.break_after_hours or 0):
                        worked -= (srv.break_minutes or 0) / 60.0
                    vals['bt_net_hours'] = max(0.0, worked)
                    vals['bt_overtime_hours'] = max(0.0, vals['bt_net_hours'] - (srv.overtime_threshold or 0))
            att.update(vals)

    @api.model
    def _cron_close_stale(self):
        """Close BioTime attendances left open longer than the max shift length.

        Without this, an employee who never punches again stays checked in forever."""
        now = fields.Datetime.now()
        for srv in self.env['biotime.server'].search([]):
            if not srv.max_shift_hours:
                continue
            cutoff = now - timedelta(hours=srv.max_shift_hours)
            stale = self.search([('bt_server_id', '=', srv.id), ('check_out', '=', False),
                                 ('check_in', '<', cutoff)])
            for att in stale:
                att.write({'check_out': att.check_in + timedelta(hours=srv.default_shift_hours),
                           'bt_missing_checkout': True})
