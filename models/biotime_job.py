# -*- coding: utf-8 -*-
"""Built-in job queue: no OCA queue_job dependency.

Jobs are executed one at a time by a cron (so the BioTime server is never hit
by parallel pulls). Failures retry after the server's retry delays
(default 1, 5, 10, 30 minutes) before the job is marked Failed.
"""
import json
import logging
from datetime import timedelta

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class BiotimeJob(models.Model):
    _name = 'biotime.job'
    _description = 'BioTime Background Job'
    _order = 'next_run, id desc'

    name = fields.Char(required=True)
    server_id = fields.Many2one('biotime.server', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='server_id.company_id', store=True)
    job_type = fields.Selection([
        ('pull', 'Pull + transfer'), ('devices', 'Sync devices'),
        ('employees', 'Pull employees'), ('push', 'Push master data')], required=True)
    params = fields.Text()
    state = fields.Selection([('pending', 'Pending'), ('started', 'Started'),
                              ('done', 'Done'), ('failed', 'Failed')], default='pending', index=True)
    attempts = fields.Integer(readonly=True)
    next_run = fields.Datetime(default=fields.Datetime.now, index=True)
    date_done = fields.Datetime(readonly=True)
    result = fields.Char(readonly=True)
    error = fields.Text(readonly=True)

    @api.model
    def enqueue(self, server, job_type, params=None):
        if not params:
            dup = self.search([('server_id', '=', server.id), ('job_type', '=', job_type),
                               ('state', 'in', ('pending', 'started'))], limit=1)
            if dup:
                return dup
        label = dict(self._fields['job_type'].selection)[job_type]
        return self.create({'name': '%s - %s' % (server.name, label), 'server_id': server.id,
                            'job_type': job_type, 'params': json.dumps(params or {})})

    def action_requeue(self):
        self.write({'state': 'pending', 'next_run': fields.Datetime.now(), 'attempts': 0, 'error': False})

    def _dispatch(self):
        self.ensure_one()
        srv = self.server_id
        params = json.loads(self.params or '{}')
        if self.job_type == 'pull':
            return srv._run_pull(params)
        if self.job_type == 'devices':
            return srv._sync_devices()
        if self.job_type == 'employees':
            return srv._sync_employees()
        return srv._push_all()

    def _run(self):
        for job in self:
            job.write({'state': 'started', 'attempts': job.attempts + 1})
            try:
                with self.env.cr.savepoint():
                    result = job._dispatch()
                job.write({'state': 'done', 'date_done': fields.Datetime.now(),
                           'result': (result or '')[:250], 'error': False})
            except Exception as exc:  # noqa: BLE001 - any failure must be retried/recorded
                _logger.warning("BioTime job %s failed: %s", job.id, exc)
                delays = job.server_id._retry_delays()
                if job.attempts <= len(delays):
                    job.write({'state': 'pending', 'error': str(exc),
                               'next_run': fields.Datetime.now() + timedelta(minutes=delays[job.attempts - 1])})
                    status = 'warning'
                else:
                    job.write({'state': 'failed', 'error': str(exc)})
                    status = 'failed'
                job.server_id._log('job', status, _("%(job)s attempt %(n)s: %(err)s") % {
                    'job': job.name, 'n': job.attempts, 'err': exc}, errors=1)

    @api.model
    def _cron_run_jobs(self):
        jobs = self.search([('state', '=', 'pending'),
                            ('next_run', '<=', fields.Datetime.now())], order='next_run, id', limit=20)
        jobs._run()
