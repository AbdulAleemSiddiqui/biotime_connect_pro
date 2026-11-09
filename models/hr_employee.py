# -*- coding: utf-8 -*-
from odoo import fields, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    biotime_code = fields.Char('BioTime Code', copy=False, index=True,
                               help="Employee code (emp_code) in BioTime.")
    biotime_server_id = fields.Many2one('biotime.server', 'BioTime Server', copy=False)
    biotime_remote_id = fields.Char('BioTime ID', copy=False, readonly=True)
