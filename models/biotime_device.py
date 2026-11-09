# -*- coding: utf-8 -*-
import logging
import socket
import time as time_mod

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class BiotimeDevice(models.Model):
    _name = 'biotime.device'
    _description = 'BioTime Device (Terminal)'
    _order = 'server_id, name'

    name = fields.Char(required=True)
    server_id = fields.Many2one('biotime.server', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='server_id.company_id', store=True)
    sn = fields.Char('Serial No.', required=True, index=True)
    remote_id = fields.Char('BioTime ID')
    ip_address = fields.Char()
    firmware = fields.Char()
    status = fields.Selection([('online', 'Online'), ('offline', 'Offline')], default='offline')
    user_count = fields.Integer('Users')
    fp_count = fields.Integer('Fingerprints')
    face_count = fields.Integer('Faces')
    palm_count = fields.Integer('Palms')
    last_activity = fields.Datetime()
    last_ping = fields.Datetime('Last Ping', readonly=True)
    latency_ms = fields.Float('Ping Latency (ms)', readonly=True)

    _sn_unique = models.Constraint('UNIQUE(server_id, sn)', 'This device already exists on the server.')

    def _tcp_ping(self):
        """Direct TCP ping to the terminal (ZK devices listen on 4370, web on 80).

        Returns (reachable, latency_ms, port). Only meaningful when the Odoo server
        can reach the device network (on-premise / VPN)."""
        self.ensure_one()
        if not self.ip_address:
            return False, 0.0, 0
        for port in (4370, 80):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.5)
            t0 = time_mod.time()
            try:
                reachable = sock.connect_ex((self.ip_address, port)) == 0
            except OSError:
                reachable = False
            finally:
                sock.close()
            if reachable:
                return True, round((time_mod.time() - t0) * 1000, 1), port
        return False, 0.0, 0

    def action_ping_terminal(self):
        self.ensure_one()
        if not self.ip_address:
            raise UserError(_("No IP address known for device %s. Sync devices first.") % self.name)
        ok, latency, port = self._tcp_ping()
        vals = {'last_ping': fields.Datetime.now(),
                'latency_ms': latency if ok else 0.0}
        # Only let the ping decide online/offline in LAN mode; otherwise the API
        # heartbeat (Sync Devices) stays the source of truth.
        if self.server_id.ping_devices:
            vals['status'] = 'online' if ok else 'offline'
        self.write(vals)
        if ok:
            return self.server_id._notify(_("%s is reachable on port %s (%s ms).") % (self.name, port, latency))
        if not self.server_id.ping_devices:
            return self.server_id._notify(_(
                "%s did not answer from this network (ports 4370/80). "
                "Status still follows the BioTime API; enable 'TCP-ping devices' on the "
                "server to let pings decide.") % self.name, kind='warning')
        return self.server_id._notify(_("%s did not answer on ports 4370/80.") % self.name, kind='warning')
