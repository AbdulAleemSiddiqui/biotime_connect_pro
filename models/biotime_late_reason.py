# -*- coding: utf-8 -*-
"""Late-arrival reasoning: employee submits → department manager → HR.

An approved reason never alters the attendance record itself; payroll stays
manual by design. It is purely a recorded, auditable exchange."""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

WORKFLOW_STATES = [
    ('submitted', 'Submitted'),
    ('manager_approved', 'Manager Approved'),
    ('approved', 'Approved (HR)'),
    ('rejected', 'Rejected'),
]


class BiotimeLateReason(models.Model):
    _name = 'biotime.late.reason'
    _description = 'Late Arrival Reason'
    _inherit = ['mail.thread']
    _order = 'date desc, id desc'

    attendance_id = fields.Many2one('hr.attendance', required=True, ondelete='cascade', index=True)
    employee_id = fields.Many2one('hr.employee', related='attendance_id.employee_id',
                                  store=True, index=True)
    company_id = fields.Many2one(related='employee_id.company_id', store=True)
    date = fields.Datetime('Late Check-in', related='attendance_id.check_in', store=True)
    late_minutes = fields.Integer(related='attendance_id.bt_late_minutes', store=True)
    category = fields.Selection([
        ('forgot', 'Forgot / overslept'), ('traffic', 'Traffic'),
        ('medical', 'Medical / family'), ('official', 'Official duty'),
        ('other', 'Other')], default='other', required=True)
    reason = fields.Text('Reason', required=True)
    state = fields.Selection(WORKFLOW_STATES, default='submitted', index=True, tracking=True)
    manager_id = fields.Many2one('hr.employee', 'Approved by Manager', readonly=True)
    manager_date = fields.Datetime(readonly=True)
    hr_id = fields.Many2one('hr.employee', 'Reviewed by HR', readonly=True)
    hr_date = fields.Datetime(readonly=True)
    note = fields.Char('Reviewer Note')

    _attendance_unique = models.Constraint(
        'UNIQUE(attendance_id)', 'A reason was already submitted for this late arrival.')

    # ------------------------------------------------------------------
    def _is_manager_of(self):
        self.ensure_one()
        emp = self.env.user.employee_id
        return bool(emp) and self.employee_id.department_id.manager_id == emp

    def _is_hr(self):
        return self.env.user.has_group('hr_attendance.group_hr_attendance_officer')

    def _check_reviewer(self):
        for rec in self:
            if not (rec._is_hr() or rec._is_manager_of()):
                raise UserError(_("Only the employee's department manager or HR can review this."))

    def action_manager_approve(self):
        self._check_reviewer()
        for rec in self.filtered(lambda r: r.state == 'submitted'):
            rec.write({'state': 'manager_approved', 'manager_id': self.env.user.employee_id.id,
                       'manager_date': fields.Datetime.now()})
        self.filtered(lambda r: r.state == 'manager_approved' and r._is_hr()).action_hr_approve()

    def action_hr_approve(self):
        if not self._is_hr():
            raise UserError(_("Only HR officers can give the final approval."))
        for rec in self.filtered(lambda r: r.state in ('submitted', 'manager_approved')):
            rec.write({'state': 'approved', 'hr_id': self.env.user.employee_id.id,
                       'hr_date': fields.Datetime.now()})

    def action_reject(self, note=False):
        self._check_reviewer()
        for rec in self.filtered(lambda r: r.state in ('submitted', 'manager_approved')):
            rec.write({'state': 'rejected', 'note': note or _('Rejected.')})

    @api.model
    def _cron_payroll_reminder(self):
        """Weekly nudge every Thursday (client rule), plus daily escalation
        in the last 2 days before the payroll deadline."""
        today = fields.Date.context_today(self)
        days_left = (today.replace(day=21) - today).days
        is_thursday = today.weekday() == 3
        if not is_thursday and not 0 <= days_left <= 2:
            return
        pending = self.search([('state', 'in', ('submitted', 'manager_approved'))])
        if not pending:
            return
        officer_group = self.env.ref('hr_attendance.group_hr_attendance_officer')
        model_id = self.env['ir.model']._get_id('biotime.late.reason')
        act_type = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        Activity = self.env['mail.activity'].sudo()
        for rec in pending:
            user = rec.employee_id.department_id.manager_id.user_id or \
                self.env['res.users'].search([('groups_id', 'in', officer_group.id)], limit=1)
            if not user or Activity.search_count([
                    ('res_model', '=', 'biotime.late.reason'), ('res_id', '=', rec.id),
                    ('user_id', '=', user.id)]):
                continue
            Activity.create({
                'res_model_id': model_id, 'res_id': rec.id,
                'activity_type_id': act_type.id if act_type else False,
                'user_id': user.id, 'date_deadline': today,
                'summary': _('Payroll review: %s day(s) left, %s reason(s) pending.') % (
                    days_left, len(pending)),
            })
