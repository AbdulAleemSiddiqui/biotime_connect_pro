# -*- coding: utf-8 -*-
from odoo import api, fields, models


class BiotimeRemoteMap(models.Model):
    """Odoo record <-> BioTime remote id (departments, areas)."""
    _name = 'biotime.remote.map'
    _description = 'BioTime Remote ID Map'

    server_id = fields.Many2one('biotime.server', required=True, ondelete='cascade', index=True)
    res_model = fields.Char(required=True)
    res_id = fields.Integer(required=True)
    remote_id = fields.Integer(required=True)

    @api.model
    def _get(self, server, record):
        rec = self.sudo().search([('server_id', '=', server.id), ('res_model', '=', record._name),
                                  ('res_id', '=', record.id)], limit=1)
        return rec.remote_id or 0

    @api.model
    def _set(self, server, record, remote_id):
        rec = self.sudo().search([('server_id', '=', server.id), ('res_model', '=', record._name),
                                  ('res_id', '=', record.id)], limit=1)
        if rec:
            rec.remote_id = remote_id
        else:
            self.sudo().create({'server_id': server.id, 'res_model': record._name,
                                'res_id': record.id, 'remote_id': remote_id})
