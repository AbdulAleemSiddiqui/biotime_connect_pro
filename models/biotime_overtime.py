# -*- coding: utf-8 -*-
"""Overtime sheet: a standalone claim, approved by manager + HR.

Deliberately NOT connected to payroll — the client settles overtime manually.
Hours suggested from the synced attendance (bt_overtime_hours) are a starting
point the employee can edit before submitting."""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .biotime_late_reason import WORKFLOW_STATES


class BiotimeOvertimeSheet(models.Model):
    _name = 'biotime.overtime.sheet'
    _description = 'Overtime Sheet'
    _inherit = ['mail.thread']
    _order = 'date desc, id desc'

    employee_id = fields.Many2one('hr.employee', required=True, index=True,
                                  default=lambda s: s.env.user.employee_id)
    company_id = fields.Many2one(related='employee_id.company_id', store=True)
    date = fields.Date('Overtime Date', required=True, default=fields.Date.context_today)
    hours_claimed = fields.Float('Hours Claimed', required=True, tracking=True)
    hours_synced = fields.Float('Hours per Attendance Sync', readonly=True,
                                help="Overtime computed from synced punches for this date.")
    reason = fields.Char('Reason / Task', required=True)
    state = fields.Selection(WORKFLOW_STATES, default='submitted', index=True, tracking=True)
    manager_id = fields.Many2one('hr.employee', 'Approved by Manager', readonly=True)
    manager_date = fields.Datetime(readonly=True)
    hr_id = fields.Many2one('hr.employee', 'Reviewed by HR', readonly=True)
    hr_date = fields.Datetime(readonly=True)
    note = fields.Char('Reviewer Note')

    def action_suggest_from_sync(self):
        """Prefill hours_synced from the employee's attendance overtime for the day."""
        for rec in self:
            day_start = fields.Datetime.to_datetime(rec.date)
            atts = self.env['hr.attendance'].sudo().search([
                ('employee_id', '=', rec.employee_id.id),
                ('check_in', '>=', day_start), ('check_in', '<', day_start.replace(hour=23, minute=59, second=59)),
            ])
            rec.hours_synced = sum(atts.mapped('bt_overtime_hours'))

    def _check_reviewer(self):
        for rec in self:
            emp = self.env.user.employee_id
            is_mgr = bool(emp) and rec.employee_id.department_id.manager_id == emp
            if not (self.env.user.has_group('hr_attendance.group_hr_attendance_officer') or is_mgr):
                raise UserError(_("Only the employee's department manager or HR can review this."))

    def action_manager_approve(self):
        self._check_reviewer()
        for rec in self.filtered(lambda r: r.state == 'submitted'):
            rec.write({'state': 'manager_approved', 'manager_id': self.env.user.employee_id.id,
                       'manager_date': fields.Datetime.now()})
        self.filtered(lambda r: r.state == 'manager_approved' and
                      self.env.user.has_group('hr_attendance.group_hr_attendance_officer')).action_hr_approve()

    def action_hr_approve(self):
        if not self.env.user.has_group('hr_attendance.group_hr_attendance_officer'):
            raise UserError(_("Only HR officers can give the final approval."))
        for rec in self.filtered(lambda r: r.state in ('submitted', 'manager_approved')):
            rec.write({'state': 'approved', 'hr_id': self.env.user.employee_id.id,
                       'hr_date': fields.Datetime.now()})

    def action_reject(self, note=False):
        self._check_reviewer()
        for rec in self.filtered(lambda r: r.state in ('submitted', 'manager_approved')):
            rec.write({'state': 'rejected', 'note': note or _('Rejected.')})
