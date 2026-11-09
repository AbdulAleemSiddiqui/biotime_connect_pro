# -*- coding: utf-8 -*-
from odoo import fields, models


class BiotimeSyncLog(models.Model):
    _name = 'biotime.sync.log'
    _description = 'BioTime Sync Log'
    _order = 'create_date desc, id desc'

    server_id = fields.Many2one('biotime.server', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='server_id.company_id', store=True)
    operation = fields.Selection([
        ('pull', 'Pull transactions'), ('process', 'Transfer to attendance'),
        ('devices', 'Sync devices'), ('employees', 'Pull employees'),
        ('push', 'Push master data'), ('job', 'Job')], required=True)
    status = fields.Selection([('success', 'Success'), ('warning', 'Warning'), ('failed', 'Failed')],
                              required=True)
    message = fields.Text()
    created = fields.Integer('New')
    skipped = fields.Integer('Skipped')
    errors = fields.Integer('Errors')
    duration = fields.Float('Duration (s)')
