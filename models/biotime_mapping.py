# -*- coding: utf-8 -*-
"""HR-facing employee mapping screen.

One row per BioTime employee. HR picks the Odoo employee (or creates one)
and clicks Apply — the link is stored permanently on the employee record.
No SQL, no scripts: this screen is the supported way to map people."""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class BiotimeMappingReview(models.Model):
    _name = 'biotime.mapping.review'
    _description = 'Employee Mapping Review'
    _order = 'status, bt_name'
    _rec_name = 'bt_name'

    server_id = fields.Many2one('biotime.server', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='server_id.company_id', store=True)
    emp_code = fields.Char('BioTime Code', required=True)
    bt_name = fields.Char('Name in BioTime')
    department_name = fields.Char('Department (BioTime)')
    punches = fields.Integer('Recent Punches',
                             help="How many of this employee's punches are staged.")
    employee_id = fields.Many2one('hr.employee', 'Odoo Employee',
                                  domain="[('company_id', 'in', (False, company_id))]",
                                  help="Pick the Odoo employee, then click Apply.")
    candidate_names = fields.Text('Possible Matches', readonly=True)
    status = fields.Selection([
        ('mapped', 'Mapped'), ('suggested', 'Suggestion Ready'),
        ('conflict', 'Multiple Matches'), ('unmapped', 'No Match'),
        ('ignored', 'Ignored')], default='unmapped', index=True)

    _code_unique = models.Constraint(
        'UNIQUE(server_id, emp_code)', 'This BioTime code already has a review row.')

    # ------------------------------------------------------------------
    def action_apply(self):
        for rec in self:
            if not rec.employee_id:
                raise UserError(_("Pick an Odoo employee first."))
            if rec.employee_id.biotime_code and rec.employee_id.biotime_code != rec.emp_code:
                raise UserError(_("%s already carries BioTime code '%s'.") % (
                    rec.employee_id.name, rec.employee_id.biotime_code))
            other = self.env['hr.employee'].sudo().search([
                ('biotime_code', '=', rec.emp_code),
                ('id', '!=', rec.employee_id.id)], limit=1)
            if other:
                raise UserError(_("Code '%s' is already assigned to %s.") % (
                    rec.emp_code, other.name))
            rec.employee_id.sudo().write({
                'biotime_code': rec.emp_code, 'biotime_server_id': rec.server_id.id})
            rec.status = 'mapped'

    def action_create_employee(self):
        for rec in self.filtered(lambda r: not r.employee_id):
            emp = self.env['hr.employee'].sudo().create({
                'name': rec.bt_name or rec.emp_code,
                'biotime_code': rec.emp_code,
                'biotime_server_id': rec.server_id.id,
                'company_id': rec.server_id.company_id.id,
            })
            rec.employee_id = emp
            rec.status = 'mapped'

    def action_ignore(self):
        self.filtered(lambda r: r.status != 'mapped').write({'status': 'ignored'})

    def action_reload(self):
        for srv in self.mapped('server_id'):
            srv._load_mapping_review()
