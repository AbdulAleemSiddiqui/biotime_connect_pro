# -*- coding: utf-8 -*-
import logging
import re
import time
from datetime import datetime, time as dtime, timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .biotime_api import BioTimeClient, BioTimeError

_logger = logging.getLogger(__name__)

TS_FMT = '%Y-%m-%d %H:%M:%S'


def _tz_get(self):
    return [(tz, tz) for tz in pytz.all_timezones]


def parse_ts(value):
    """BioTime timestamp string -> naive datetime (no tz conversion)."""
    if not value:
        return False
    return datetime.strptime(str(value).replace('T', ' ')[:19], TS_FMT)


class BiotimeServer(models.Model):
    _name = 'biotime.server'
    _description = 'BioTime Server'
    _inherit = ['mail.thread']
    _order = 'name'

    # --- connection -----------------------------------------------------
    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company)
    is_cloud = fields.Boolean('BioTime Cloud', help="Connect to ZKTeco BioTime Cloud (HTTPS, no port).")
    protocol = fields.Selection([('http', 'HTTP'), ('https', 'HTTPS')], default='http', required=True)
    host = fields.Char('Host / IP / URL', required=True)
    port = fields.Integer(default=8000)
    auth_type = fields.Selection([
        ('token', 'Token (BioTime 9.x)'),
        ('jwt', 'JWT (BioTime 8.x / Cloud)'),
        ('basic', 'HTTP Basic')], default='jwt', required=True)
    username = fields.Char(required=True)
    password = fields.Char(groups='biotime_connect_pro.group_biotime_manager')
    verify_ssl = fields.Boolean('Verify SSL certificate', default=True)
    timeout = fields.Integer('Timeout (s)', default=30)
    timezone = fields.Selection(_tz_get, string='Device Timezone', required=True,
                                default=lambda s: s.env.user.tz or 'UTC',
                                help="Timezone the devices use for punch times.")

    # --- sync behaviour ---------------------------------------------------
    pull_mode = fields.Selection([
        ('all', '1 request for all transactions'),
        ('per_device', '1 request per device')], default='all', required=True)
    page_size = fields.Integer(default=200)
    start_time = fields.Datetime('First sync from', help="Never import punches older than this.")
    overlap_minutes = fields.Integer('Re-pull overlap (min)', default=10,
                                     help="Safety window re-read on every sync; duplicates are ignored.")
    auto_sync = fields.Boolean('Automatic sync', default=True)
    sync_interval = fields.Integer('Sync every (min)', default=15)
    last_sync_time = fields.Datetime(readonly=True)
    last_device_sync = fields.Datetime(readonly=True)
    employee_push_mode = fields.Selection([
        ('all', 'All employees'),
        ('assigned', 'Only employees assigned to this server')], default='assigned', required=True)
    mapping_field = fields.Selection([
        ('auto', 'Auto (BioTime code, Badge, Identification)'),
        ('biotime_code', 'BioTime code'),
        ('barcode', 'Badge ID'),
        ('identification_id', 'Identification No.')], default='auto', required=True)
    auto_create_employee = fields.Boolean('Auto-create unknown employees')
    code_prefix = fields.Char('Code prefix for pushed records', default='OD')
    root_department_id = fields.Integer('BioTime root department ID', default=1)
    default_area_id = fields.Integer('BioTime default area ID', default=1)

    # --- attendance rules -------------------------------------------------
    pairing_mode = fields.Selection([
        ('explicit', 'Explicit IN / OUT punch states'),
        ('alternating', 'Alternating (toggle IN/OUT)'),
        ('first_last', 'First & Last punch of the day (state-agnostic)')],
        default='explicit', required=True,
        help="First & Last mirrors the BioTime 'First & Last' report: "
             "the earliest punch of the day is the check-in, the latest is the check-out, "
             "whatever the device labels say.")
    punch_state_in = fields.Char('Punch state = IN', default='0')
    punch_state_out = fields.Char('Punch state = OUT', default='1')
    min_punch_gap = fields.Integer('Ignore double punches within (min)', default=1)
    max_shift_hours = fields.Float('Max shift length (h)', default=16.0)
    default_shift_hours = fields.Float('Auto-close missing check-out after (h)', default=8.0)
    late_tolerance = fields.Integer('Late grace (min)', default=5)
    early_out_tolerance = fields.Integer('Early-out grace (min)', default=5)
    break_minutes = fields.Integer('Break to deduct (min)', default=60)
    break_after_hours = fields.Float('Deduct break after (h)', default=6.0)
    overtime_threshold = fields.Float('Overtime after (h)', default=8.0)

    # --- job queue ----------------------------------------------------------
    retry_delays = fields.Char('Retry delays (min)', default='1,5,10,30')
    max_retries = fields.Integer('Max transaction retries', default=5)

    # --- real-time / device health -----------------------------------------
    webhook_token = fields.Char('Webhook token', copy=False,
                                help="Set on any active server to require this token "
                                     "(header 'X-Biotime-Token') on /biotime/webhook/push calls.")
    ping_devices = fields.Boolean('TCP-ping devices (LAN only)',
                                  help="Ping each terminal directly from the Odoo server. "
                                       "Only meaningful when Odoo runs on the device network "
                                       "(on-premise or VPN); on cloud hosting every device "
                                       "would wrongly show offline.")
    last_device_ping = fields.Datetime(readonly=True)

    # --- relations / counters ---------------------------------------------------
    device_ids = fields.One2many('biotime.device', 'server_id')
    device_count = fields.Integer(compute='_compute_counts')
    online_count = fields.Integer(compute='_compute_counts')
    pending_count = fields.Integer(compute='_compute_counts')
    error_count = fields.Integer(compute='_compute_counts')
    transaction_count = fields.Integer(compute='_compute_counts')
    job_failed_count = fields.Integer(compute='_compute_counts')

    @api.depends('device_ids.status')
    def _compute_counts(self):
        Tx = self.env['biotime.transaction']
        Job = self.env['biotime.job']
        for srv in self:
            srv.device_count = len(srv.device_ids)
            srv.online_count = len(srv.device_ids.filtered(lambda d: d.status == 'online'))
            srv.transaction_count = Tx.search_count([('server_id', '=', srv.id)])
            srv.pending_count = Tx.search_count([('server_id', '=', srv.id), ('state', '=', 'pending')])
            srv.error_count = Tx.search_count([('server_id', '=', srv.id), ('state', '=', 'error')])
            srv.job_failed_count = Job.search_count([('server_id', '=', srv.id), ('state', '=', 'failed')])

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _base_url(self):
        self.ensure_one()
        host = (self.host or '').strip().rstrip('/')
        proto = 'https' if self.is_cloud else self.protocol
        url = host if host.startswith(('http://', 'https://')) else '%s://%s' % (proto, host)
        if self.port and not self.is_cloud and not re.search(r':\d+$', url):
            url += ':%s' % self.port
        return url

    def _client(self):
        self.ensure_one()
        return BioTimeClient(self)

    def _tz(self):
        return pytz.timezone(self.timezone or 'UTC')

    def _to_utc(self, naive_local):
        """Device-local naive datetime -> naive UTC."""
        local = self._tz().localize(naive_local, is_dst=False)
        return local.astimezone(pytz.utc).replace(tzinfo=None)

    def _to_local_str(self, naive_utc):
        return pytz.utc.localize(naive_utc).astimezone(self._tz()).strftime(TS_FMT)

    def _retry_delays(self):
        out = []
        for part in (self.retry_delays or '').split(','):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
        return out or [1, 5, 10, 30]

    def _notify(self, message, kind='success'):
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _('BioTime'), 'message': message, 'type': kind, 'sticky': False}}

    def _log(self, operation, status, message='', created=0, skipped=0, errors=0, duration=0.0):
        self.env['biotime.sync.log'].sudo().create({
            'server_id': self.id, 'operation': operation, 'status': status,
            'message': message, 'created': created, 'skipped': skipped,
            'errors': errors, 'duration': duration})

    def _open(self, model, name, domain):
        return {'type': 'ir.actions.act_window', 'name': name, 'res_model': model,
                'view_mode': 'list,form', 'domain': domain,
                'context': {'default_server_id': self.id}}

    # ------------------------------------------------------------------
    # buttons
    # ------------------------------------------------------------------
    def action_test_connection(self):
        self.ensure_one()
        try:
            client = self._client()
            data = client.request('GET', '/iclock/api/terminals/', params={'page': 1, 'page_size': 1})
            count = data.get('count', '?') if isinstance(data, dict) else len(data)
        except BioTimeError as exc:
            raise UserError(_("Connection failed:\n%s") % exc)
        return self._notify(_("Connection successful. %s device(s) found.") % count)

    def action_sync_devices(self):
        self.ensure_one()
        return self._notify(self._sync_devices())

    def action_sync_employees(self):
        self.ensure_one()
        return self._notify(self._sync_employees())

    def action_ping_devices(self):
        self.ensure_one()
        Device = self.env['biotime.device']
        online = 0
        for dev in self.device_ids:
            ok, latency, _port = dev._tcp_ping()
            vals = {'last_ping': fields.Datetime.now(),
                    'latency_ms': latency if ok else 0.0}
            # In non-LAN mode the API heartbeat stays the source of truth for status.
            if self.ping_devices:
                vals['status'] = 'online' if ok else 'offline'
            dev.write(vals)
            online += bool(ok)
        self.last_device_ping = fields.Datetime.now()
        self._log('devices', 'success', _("Ping: %s/%s devices reachable.") % (online, len(self.device_ids)))
        return self._notify(_("Ping complete: %s/%s device(s) reachable from this server.") % (online, len(self.device_ids)))

    @api.model
    def _cron_ping_devices(self):
        for srv in self.search([('ping_devices', '=', True)]):
            srv.action_ping_devices()

    # ------------------------------------------------------------------
    # dashboard
    # ------------------------------------------------------------------
    @api.model
    def _dashboard_tz(self):
        return pytz.timezone(self.env.user.tz or 'Asia/Riyadh')

    @api.model
    def get_dashboard_data(self):
        """Everything the OWL dashboard renders: KPIs, alerts, recent attendance,
        top late arrivals. 'Today' is evaluated in the user's timezone."""
        tz = self._dashboard_tz()
        srv = self.search([], limit=1)
        now_local = datetime.now(tz)
        day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        win_from = day_start.astimezone(pytz.utc).replace(tzinfo=None)
        win_to = (day_start + timedelta(days=1)).astimezone(pytz.utc).replace(tzinfo=None)

        Employee = self.env['hr.employee'].sudo()
        Att = self.env['hr.attendance'].sudo()
        Tx = self.env['biotime.transaction'].sudo()
        Device = self.env['biotime.device'].sudo()
        Log = self.env['biotime.sync.log'].sudo()
        Job = self.env['biotime.job'].sudo()

        total_employees = Employee.search_count([('active', '=', True)])
        today_atts = Att.search([('check_in', '>=', win_from), ('check_in', '<', win_to)])
        present = len(set(today_atts.mapped('employee_id').ids))
        late_today = len(today_atts.filtered(lambda a: a.bt_status == 'late'))
        absent = max(total_employees - present, 0)
        rate = round(present * 100.0 / total_employees, 1) if total_employees else 0.0
        worked_total = sum(today_atts.mapped('worked_hours'))
        worked_avg = round(worked_total / present, 1) if present else 0.0

        synced = Tx.search_count([('state', '=', 'processed')])
        queue = Tx.search_count([('state', 'in', ('pending', 'error'))])
        sync_pct = round(synced * 100.0 / (synced + queue)) if (synced + queue) else 100

        if srv and srv.last_sync_time:
            last_sync = pytz.utc.localize(srv.last_sync_time).astimezone(tz).strftime('%d/%m/%Y %H:%M')
        else:
            last_sync = now_local.strftime('%d/%m/%Y %H:%M')
        last_log = Log.search([('operation', 'in', ('pull', 'job'))], order='id desc', limit=1)
        last_status = ((last_log.status if last_log else 'success') or 'success').upper()

        recent = []
        for att in Att.search([], order='check_in desc, id desc', limit=10):
            cin = pytz.utc.localize(att.check_in).astimezone(tz)
            cout = pytz.utc.localize(att.check_out).astimezone(tz) if att.check_out else False
            wh = att.worked_hours or 0.0
            if not att.check_out:
                st, label = 'incomplete', 'Incomplete'
            elif att.bt_status == 'missing_checkout':
                st, label = 'missing', 'Missing Check-out'
            elif att.bt_status == 'excused':
                st, label = 'excused', 'Excused'
            elif att.bt_status == 'late':
                st, label = 'late', 'Late'
            else:
                st, label = 'present', 'Present'
            recent.append({
                'employee': att.employee_id.name or '',
                'date': cin.strftime('%b %d'),
                'check_in': cin.strftime('%H:%M'),
                'check_out': cout.strftime('%H:%M') if cout else '-',
                'hours': '%02d:%02d' % (int(wh), int(round((wh - int(wh)) * 60))),
                'status': st, 'status_label': label,
            })

        week_ago = fields.Datetime.now() - timedelta(days=7)
        stats = {}
        for att in Att.search([('check_in', '>=', week_ago), ('bt_status', '=', 'late')]):
            emp = att.employee_id
            s = stats.setdefault(emp.id, {
                'employee': emp.name or '', 'initial': (emp.name or 'E')[0].upper(),
                'department': emp.department_id.name or '', 'count': 0, 'minutes': 0})
            s['count'] += 1
            s['minutes'] += att.bt_late_minutes or 0
        top_late = sorted(stats.values(), key=lambda s: (s['count'], s['minutes']), reverse=True)[:5]

        alerts = []
        for d in Device.search([('status', '=', 'offline')]):
            alerts.append({'id': 'device-%s' % d.id, 'severity': 'critical',
                           'title': _('Device offline: %s') % d.name,
                           'description': _('SN %s (%s) is reported offline by the BioTime server.') % (
                               d.sn, d.ip_address or '?'),
                           'action': 'view_devices'})
        if queue:
            alerts.append({'id': 'queue', 'severity': 'warning',
                           'title': _('%s punches awaiting pairing') % queue,
                           'description': _('Staged punches are waiting to be transferred to attendance.'),
                           'action': 'retry_sync'})
        failed_jobs = Job.search_count([('state', '=', 'failed')])
        if failed_jobs:
            alerts.append({'id': 'jobs', 'severity': 'critical',
                           'title': _('%s sync job(s) failed') % failed_jobs,
                           'description': _('Background pulls failed after all retries.'),
                           'action': 'view_jobs'})

        # 7-day trend: present employees + late arrivals per day
        trend = []
        for i in range(6, -1, -1):
            d0 = day_start - timedelta(days=i)
            f = d0.astimezone(pytz.utc).replace(tzinfo=None)
            t = (d0 + timedelta(days=1)).astimezone(pytz.utc).replace(tzinfo=None)
            atts = Att.search([('check_in', '>=', f), ('check_in', '<', t)])
            trend.append({'label': d0.strftime('%a'), 'day': d0.strftime('%d %b'),
                          'present': len(set(atts.mapped('employee_id').ids)),
                          'late': len(atts.filtered(lambda a: a.bt_status == 'late'))})

        # payroll window: 20th -> 20th, review deadline the 21st (client rule)
        if now_local.day >= 20:
            p_start = now_local.replace(day=20, hour=0, minute=0, second=0, microsecond=0)
            deadline = (p_start + timedelta(days=32)).replace(day=21, hour=23, minute=59)
        else:
            prev_end = now_local.replace(day=1) - timedelta(days=1)
            p_start = prev_end.replace(day=20, hour=0, minute=0, second=0, microsecond=0)
            deadline = now_local.replace(day=21, hour=23, minute=59)
        days_left = max((deadline.date() - now_local.date()).days, 0)
        period_atts = Att.search([
            ('check_in', '>=', p_start.astimezone(pytz.utc).replace(tzinfo=None)),
            ('bt_status', '=', 'late')])
        payroll = {'start': p_start.strftime('%d %b'), 'deadline': deadline.strftime('%d %b %Y'),
                   'days_left': days_left, 'late_count': len(period_atts),
                   'late_minutes': sum(period_atts.mapped('bt_late_minutes'))}

        # department breakdown for today
        dept_map = {}
        for emp in Employee.search([('active', '=', True)]):
            dname = emp.department_id.name or 'No department'
            dept_map.setdefault(dname, {'name': dname, 'employees': 0, 'present': 0, 'late': 0})
            dept_map[dname]['employees'] += 1
        for emp in Employee.browse(set(today_atts.mapped('employee_id').ids)):
            dname = emp.department_id.name or 'No department'
            if dname in dept_map:
                dept_map[dname]['present'] += 1
        for att in today_atts.filtered(lambda a: a.bt_status == 'late'):
            dname = att.employee_id.department_id.name or 'No department'
            if dname in dept_map:
                dept_map[dname]['late'] += 1
        departments = sorted(dept_map.values(), key=lambda d: -d['employees'])[:6]

        return {
            'header': {'last_sync': last_sync, 'last_status': last_status},
            'kpis': {'employees': total_employees, 'present': present,
                     'late': late_today, 'absent': absent},
            'metrics': {'rate': rate, 'worked_hours': round(worked_total, 1),
                        'worked_avg': worked_avg, 'synced': synced,
                        'queue': queue, 'sync_pct': sync_pct},
            'recent': recent, 'top_late': top_late, 'alerts': alerts,
            'trend': trend, 'payroll': payroll, 'departments': departments,
            'links': {
                'today_domain': [('check_in', '>=', win_from), ('check_in', '<', win_to)],
                'late_domain': [('check_in', '>=', win_from), ('check_in', '<', win_to),
                                ('bt_status', '=', 'late')],
                'absent_domain': ['!', '&', ('attendance_ids.check_in', '>=', win_from),
                                  ('attendance_ids.check_in', '<', win_to)],
            },
        }

    @api.model
    def action_dashboard_sync(self):
        """Queue a background pull + transfer job from the dashboard."""
        srv = self.search([], limit=1)
        if not srv:
            raise UserError(_("No BioTime server configured yet."))
        self.env['biotime.job'].sudo().enqueue(srv, 'pull')
        return _("Sync queued — new punches will flow in within a minute.")

    @api.model
    def action_dashboard_sync_employees(self):
        srv = self.search([], limit=1)
        if not srv:
            raise UserError(_("No BioTime server configured yet."))
        self.env['biotime.job'].sudo().enqueue(srv, 'employees')
        return _("Employee sync queued in the background.")

    def action_push_all(self):
        self.ensure_one()
        return self._notify(self._push_all())

    def action_process_pending(self):
        self.ensure_one()
        return self._notify(self._process_pending())

    def action_retry_all_errors(self):
        """One-click recovery: retry every failed/skipped punch and reprocess.

        Covers the usual ops case: employees were created/mapped after their
        punches had already errored."""
        self.ensure_one()
        Tx = self.env['biotime.transaction'].sudo()
        txs = Tx.search([('server_id', '=', self.id),
                         ('state', 'in', ('error', 'skipped'))])
        count = len(txs)
        if count:
            txs.action_retry()
        msg = _("%s failed punch(es) retried. ") % count
        msg += self._process_pending()
        return self._notify(msg)

    # ------------------------------------------------------------------
    # employee mapping review
    # ------------------------------------------------------------------
    def _name_candidates(self, name):
        """Odoo employees whose name matches the (possibly truncated) BioTime name:
        exact, word-prefix, or squashed (spaces removed)."""
        Employee = self.env['hr.employee'].sudo()
        low = ' '.join((name or '').lower().split())
        if not low:
            return Employee
        matches = Employee
        for emp in Employee.search(['|', ('company_id', '=', False),
                                    ('company_id', '=', self.company_id.id)]):
            en = ' '.join((emp.name or '').lower().split())
            if en == low or en.startswith(low) or en.replace(' ', '') == low.replace(' ', ''):
                matches |= emp
        return matches

    def _load_mapping_review(self):
        """Sync the review table from the BioTime employee list + current mappings."""
        self.ensure_one()
        client = self._client()
        Review = self.env['biotime.mapping.review'].sudo()
        Employee = self.env['hr.employee'].sudo()
        staged = {r['emp_code']: r['emp_code_count']
                  for r in self.env['biotime.transaction'].sudo().read_group(
                      [('server_id', '=', self.id)], ['emp_code'], ['emp_code'])}
        for rows in client.paginate('/personnel/api/employees/', {}, self.page_size):
            for row in rows:
                code = (row.get('emp_code') or '').strip()
                if not code:
                    continue
                name = ' '.join(filter(None, [row.get('first_name'), row.get('last_name')])).strip() or code
                dept = row.get('department')
                dept_name = dept.get('dept_name') if isinstance(dept, dict) else ''
                rec = Review.search([('server_id', '=', self.id),
                                     ('emp_code', '=', code)], limit=1)
                vals = {'bt_name': name, 'department_name': dept_name,
                        'punches': staged.get(code, 0)}
                mapped_emp = Employee.search([('biotime_code', '=', code)], limit=1)
                if mapped_emp:
                    vals.update({'employee_id': mapped_emp.id, 'status': 'mapped'})
                elif rec.employee_id and rec.employee_id.biotime_code == code:
                    vals['status'] = 'mapped'
                else:
                    candidates = self._name_candidates(name)
                    if len(candidates) == 1:
                        vals.update({'employee_id': candidates.id, 'status': 'suggested',
                                     'candidate_names': candidates.name})
                    elif len(candidates) > 1:
                        vals.update({'status': 'conflict',
                                     'candidate_names': ' | '.join(
                                         candidates.mapped('name')[:8])})
                    elif rec.status not in ('ignored',):
                        vals['status'] = 'unmapped'
                if rec:
                    rec.write(vals)
                else:
                    Review.create(dict(vals, server_id=self.id, emp_code=code))
        self._log('employees', 'success', _('Employee mapping review refreshed.'))

    def action_open_mapping_review(self):
        self.ensure_one()
        self._load_mapping_review()
        return self._open('biotime.mapping.review', _('Employee Mapping Review'),
                          [('server_id', '=', self.id)])

    def action_enqueue_pull(self):
        self.ensure_one()
        self.env['biotime.job'].enqueue(self, 'pull')
        return self._notify(_("Pull queued. It will run in the background within a minute."))

    def action_view_devices(self):
        return self._open('biotime.device', _('Devices'), [('server_id', '=', self.id)])

    def action_view_transactions(self):
        return self._open('biotime.transaction', _('Transactions'), [('server_id', '=', self.id)])

    def action_view_pending(self):
        return self._open('biotime.transaction', _('Pending / Errors'),
                          [('server_id', '=', self.id), ('state', 'in', ('pending', 'error'))])

    def action_view_jobs(self):
        return self._open('biotime.job', _('Jobs'), [('server_id', '=', self.id)])

    # ------------------------------------------------------------------
    # employees lookup
    # ------------------------------------------------------------------
    def _find_employee(self, code):
        self.ensure_one()
        code = (code or '').strip()
        if not code:
            return self.env['hr.employee']
        Employee = self.env['hr.employee']
        order = {
            'auto': ['biotime_code', 'barcode', 'identification_id',
                     'employee_code', 'l10n_sa_employee_code'],
        }.get(self.mapping_field, [self.mapping_field])
        company = ['|', ('company_id', '=', False), ('company_id', '=', self.company_id.id)]
        for fname in order:
            if fname not in Employee._fields:
                continue  # optional field (e.g. identification_id) not installed here
            dom = [(fname, '=', code)] + company
            if fname == 'biotime_code':
                dom += ['|', ('biotime_server_id', '=', False), ('biotime_server_id', '=', self.id)]
            emp = Employee.search(dom, limit=1)
            if emp:
                return emp
        return Employee

    # ------------------------------------------------------------------
    # devices
    # ------------------------------------------------------------------
    def _sync_devices(self):
        self.ensure_one()
        t0 = time.time()
        Device = self.env['biotime.device']
        created = updated = 0
        client = self._client()
        for rows in client.paginate('/iclock/api/terminals/', {}, self.page_size):
            for row in rows:
                sn = str(row.get('sn') or '').strip()
                if not sn:
                    continue
                vals = {
                    'server_id': self.id, 'sn': sn,
                    'name': row.get('alias') or row.get('terminal_name') or sn,
                    'remote_id': str(row.get('id') or ''),
                    'ip_address': row.get('ip_address'),
                    'firmware': row.get('fw_ver'),
                    'status': 'online' if str(row.get('state')) in ('1', 'True', 'true') else 'offline',
                    'user_count': row.get('user_count') or 0,
                    'fp_count': row.get('fp_count') or 0,
                    'face_count': row.get('face_count') or 0,
                    'palm_count': row.get('palm_count') or 0,
                    'last_activity': parse_ts(row.get('last_activity')) and
                    self._to_utc(parse_ts(row.get('last_activity'))),
                }
                dev = Device.search([('server_id', '=', self.id), ('sn', '=', sn)], limit=1)
                if dev:
                    dev.write(vals)
                    updated += 1
                else:
                    Device.create(vals)
                    created += 1
        self.last_device_sync = fields.Datetime.now()
        msg = _("Devices: %s new, %s updated.") % (created, updated)
        self._log('devices', 'success', msg, created=created, skipped=updated,
                  duration=time.time() - t0)
        return msg

    # ------------------------------------------------------------------
    # employees pull
    # ------------------------------------------------------------------
    def _sync_employees(self):
        self.ensure_one()
        t0 = time.time()
        client = self._client()
        created = updated = 0
        for rows in client.paginate('/personnel/api/employees/', {}, self.page_size):
            for row in rows:
                code = str(row.get('emp_code') or '').strip()
                if not code:
                    continue
                emp = self._find_employee(code)
                if emp:
                    emp.write({'biotime_code': emp.biotime_code or code,
                               'biotime_remote_id': str(row.get('id') or ''),
                               'biotime_server_id': emp.biotime_server_id.id or self.id})
                    updated += 1
                elif self.auto_create_employee:
                    name = ' '.join(filter(None, [row.get('first_name'), row.get('last_name')])) or code
                    vals = {'name': name, 'biotime_code': code, 'biotime_server_id': self.id,
                            'biotime_remote_id': str(row.get('id') or ''),
                            'company_id': self.company_id.id}
                    dept = row.get('department')
                    if isinstance(dept, dict) and dept.get('dept_name'):
                        Dept = self.env['hr.department']
                        d = Dept.search([('name', '=', dept['dept_name']),
                                         ('company_id', 'in', (False, self.company_id.id))], limit=1)
                        vals['department_id'] = (d or Dept.create({
                            'name': dept['dept_name'], 'company_id': self.company_id.id})).id
                    self.env['hr.employee'].create(vals)
                    created += 1
        msg = _("Employees: %s created, %s linked/updated.") % (created, updated)
        self._log('employees', 'success', msg, created=created, skipped=updated,
                  duration=time.time() - t0)
        return msg

    # ------------------------------------------------------------------
    # push master data (Odoo -> BioTime)
    # ------------------------------------------------------------------
    def _upsert(self, client, path, lookup, payload):
        found = client.request('GET', path, params=lookup)
        rows = found.get('data', []) if isinstance(found, dict) else found
        if rows:
            rid = rows[0]['id']
            client.request('PUT', '%s%s/' % (path, rid), json=payload)
            return rid
        return client.request('POST', path, json=payload)['id']

    def _push_all(self):
        self.ensure_one()
        t0 = time.time()
        client = self._client()
        Map = self.env['biotime.remote.map']
        pre = self.code_prefix or 'OD'
        n_dept = n_area = n_emp = 0
        # departments (parents first)
        for dept in self.env['hr.department'].search(
                [('company_id', 'in', (False, self.company_id.id))], order='parent_path'):
            parent = Map._get(self, dept.parent_id) if dept.parent_id else self.root_department_id
            rid = self._upsert(client, '/personnel/api/departments/',
                               {'dept_code': '%s%s' % (pre, dept.id)},
                               {'dept_code': '%s%s' % (pre, dept.id), 'dept_name': dept.name,
                                'parent': parent or self.root_department_id})
            Map._set(self, dept, rid)
            n_dept += 1
        # work locations -> areas
        for loc in self.env['hr.work.location'].search(
                [('company_id', 'in', (False, self.company_id.id))]):
            rid = self._upsert(client, '/personnel/api/areas/',
                               {'area_code': '%s%s' % (pre, loc.id)},
                               {'area_code': '%s%s' % (pre, loc.id), 'area_name': loc.name})
            Map._set(self, loc, rid)
            n_area += 1
        # employees
        dom = [('company_id', 'in', (False, self.company_id.id))]
        if self.employee_push_mode == 'assigned':
            dom.append(('biotime_server_id', '=', self.id))
        for emp in self.env['hr.employee'].search(dom):
            code = emp.biotime_code or emp.barcode or str(emp.id)
            parts = (emp.name or code).split(' ', 1)
            payload = {
                'emp_code': code, 'first_name': parts[0],
                'last_name': parts[1] if len(parts) > 1 else '',
                'department': Map._get(self, emp.department_id) or self.root_department_id,
                'area': [Map._get(self, emp.work_location_id) or self.default_area_id],
            }
            rid = self._upsert(client, '/personnel/api/employees/', {'emp_code': code}, payload)
            emp.write({'biotime_code': code, 'biotime_remote_id': str(rid),
                       'biotime_server_id': emp.biotime_server_id.id or self.id})
            n_emp += 1
        msg = _("Pushed %s departments, %s areas, %s employees.") % (n_dept, n_area, n_emp)
        self._log('push', 'success', msg, created=n_dept + n_area + n_emp, duration=time.time() - t0)
        return msg

    # ------------------------------------------------------------------
    # transactions pull
    # ------------------------------------------------------------------
    def _default_start(self):
        self.ensure_one()
        if self.last_sync_time:
            return self.last_sync_time - timedelta(minutes=self.overlap_minutes or 0)
        return self.start_time or (fields.Datetime.now() - timedelta(days=1))

    def _pull_transactions(self, date_from=None, date_to=None, device=None, employee=None):
        """Import raw punches. Idempotent (deduplicated on BioTime id).
        Returns dict(created=recordset, seen=int)."""
        self.ensure_one()
        t0 = time.time()
        Tx = self.env['biotime.transaction']
        client = self._client()
        incremental = not date_from and not date_to and not device and not employee
        params = {'start_time': self._to_local_str(date_from or self._default_start())}
        if date_to:
            params['end_time'] = self._to_local_str(date_to)
        if employee:
            if not employee.biotime_code:
                raise UserError(_("Employee %s has no BioTime code.") % employee.name)
            params['emp_code'] = employee.biotime_code
        if device:
            queries = [{'terminal_sn': device.sn}]
        elif self.pull_mode == 'per_device' and self.device_ids:
            queries = [{'terminal_sn': d.sn} for d in self.device_ids]
        else:
            queries = [{}]
        created = Tx
        seen = 0
        newest = False
        for extra in queries:
            for rows in client.paginate('/iclock/api/transactions/', dict(params, **extra), self.page_size):
                ids = [str(r['id']) for r in rows if r.get('id') is not None]
                existing = set(Tx.search([('server_id', '=', self.id),
                                          ('biotime_id', 'in', ids)]).mapped('biotime_id'))
                vals_list = []
                for r in rows:
                    seen += 1
                    bid = str(r.get('id'))
                    ptime = parse_ts(r.get('punch_time'))
                    if not ptime or bid in existing:
                        continue
                    existing.add(bid)
                    utc = self._to_utc(ptime)
                    newest = max(newest, utc) if newest else utc
                    dev = self.device_ids.filtered(lambda d: d.sn == r.get('terminal_sn'))[:1]
                    vals_list.append({
                        'server_id': self.id, 'biotime_id': bid,
                        'emp_code': str(r.get('emp_code') or ''),
                        'emp_name': ' '.join(filter(None, [r.get('first_name'), r.get('last_name')])),
                        'punch_time': utc,
                        'punch_time_local': ptime,
                        'punch_state': str(r.get('punch_state') if r.get('punch_state') is not None else ''),
                        'punch_state_display': r.get('punch_state_display'),
                        'verify_type': r.get('verify_type_display') or str(r.get('verify_type') or ''),
                        'device_sn': r.get('terminal_sn'),
                        'device_id': dev.id or False,
                        'terminal_alias': r.get('terminal_alias'),
                        'area_alias': r.get('area_alias'),
                    })
                if vals_list:
                    created |= Tx.create(vals_list)
        if incremental and newest:
            self.last_sync_time = newest
        self._log('pull', 'success', _("%s punches read, %s new.") % (seen, len(created)),
                  created=len(created), skipped=seen - len(created), duration=time.time() - t0)
        return {'created': created, 'seen': seen}

    def _process_first_last(self, txs):
        """First & Last pairing: one attendance per employee per local calendar day.

        Mirrors the BioTime 'First & Last' transaction report: earliest punch of the
        day = check-in, latest punch = check-out, regardless of device punch states.
        The whole day is recomputed from all staged punches on each pass, so a punch
        that arrives late still lands correctly (self-healing)."""
        res = {'ok': 0, 'skip': 0, 'err': 0}
        Tx = self.env['biotime.transaction']
        Att = self.env['hr.attendance']
        for tx in txs:
            try:
                with self.env.cr.savepoint():
                    emp = tx.employee_id or tx._match_employee()
                    if not emp:
                        raise UserError(_("No Odoo employee matches BioTime code '%s'.") % tx.emp_code)
                    tx.employee_id = emp
            except Exception as exc:  # noqa: BLE001 - unmatched employee stays retryable
                msg = exc.args[0] if isinstance(exc, UserError) and exc.args else str(exc)
                tx.write({'state': 'error', 'error': msg, 'retry_count': tx.retry_count + 1})
                res['err'] += 1

        days = {}
        for tx in txs.filtered(lambda t: t.employee_id):
            emp = tx.employee_id
            tz = pytz.timezone(emp.tz) if emp.tz else self._tz()
            local_day = pytz.utc.localize(tx.punch_time).astimezone(tz).date()
            days.setdefault((emp.id, local_day), emp)

        for (emp_id, day), emp in sorted(days.items()):
            try:
                with self.env.cr.savepoint():
                    tz = pytz.timezone(emp.tz) if emp.tz else self._tz()
                    day_start = tz.localize(datetime.combine(day, dtime.min)).astimezone(pytz.utc).replace(tzinfo=None)
                    day_end = tz.localize(datetime.combine(day, dtime.max)).astimezone(pytz.utc).replace(tzinfo=None)
                    day_txs = Tx.search([
                        ('server_id', '=', self.id), ('employee_id', '=', emp.id),
                        ('punch_time', '>=', day_start), ('punch_time', '<=', day_end),
                    ], order='punch_time, id').filtered(lambda t: t.state != 'skipped')
                    if not day_txs:
                        res['skip'] += 1
                        continue
                    first, last = day_txs[0], day_txs[-1]
                    # close attendances left open on earlier days (constraint safety)
                    for prev in Att.search([('employee_id', '=', emp.id), ('check_out', '=', False),
                                            ('check_in', '<', day_start)]):
                        prev.write({'check_out': min(
                            prev.check_in + timedelta(hours=self.default_shift_hours or 8),
                            day_start - timedelta(seconds=1)), 'bt_missing_checkout': True})
                    # a same-day open attendance is reused so punches stay linked
                    att = Att.search([
                        ('employee_id', '=', emp.id), ('bt_server_id', '=', self.id),
                        ('check_in', '>=', day_start), ('check_in', '<=', day_end)], limit=1)
                    vals = {'check_in': first.punch_time}
                    if last.id != first.id and \
                            last.punch_time - first.punch_time > timedelta(minutes=self.min_punch_gap or 0):
                        vals['check_out'] = last.punch_time
                    else:
                        vals['check_out'] = False  # single punch: leave open
                    if att:
                        att.write(vals)
                    else:
                        att = Att.create(dict(vals, employee_id=emp.id, bt_server_id=self.id))
                    day_txs.write({'state': 'processed', 'attendance_id': att.id, 'error': False})
                    res['ok'] += len(day_txs)
            except Exception as exc:  # noqa: BLE001 - one bad day must not stop the rest
                _logger.warning("First&Last pairing failed for employee %s on %s: %s",
                                emp.name, day, exc)
                res['err'] += 1
        return res

    def _process_pending(self):
        self.ensure_one()
        Tx = self.env['biotime.transaction'].sudo()
        todo = Tx.search([
            ('server_id', '=', self.id), '|', ('state', '=', 'pending'),
            '&', ('state', '=', 'error'), ('retry_count', '<', self.max_retries)],
            order='punch_time, id', limit=5000)
        res = todo._process()
        msg = _("Attendance: %(ok)s created/updated, %(skip)s skipped, %(err)s errors.") % res
        self._log('process', 'success' if not res['err'] else 'warning', msg,
                  created=res['ok'], skipped=res['skip'], errors=res['err'])
        return msg

    def _run_pull(self, params=None):
        """Pull then transfer; used by jobs and the wizard."""
        self.ensure_one()
        params = params or {}
        res = self._pull_transactions(
            date_from=parse_ts(params.get('date_from')) or None,
            date_to=parse_ts(params.get('date_to')) or None,
            device=self.env['biotime.device'].browse(params.get('device_id') or 0).exists() or None,
            employee=self.env['hr.employee'].browse(params.get('employee_id') or 0).exists() or None)
        msg = _("%s punches read, %s new. ") % (res['seen'], len(res['created']))
        if params.get('transfer', True):
            msg += self._process_pending()
        return msg

    # ------------------------------------------------------------------
    # cron
    # ------------------------------------------------------------------
    @api.model
    def _cron_schedule(self):
        now = fields.Datetime.now()
        Job = self.env['biotime.job']
        for srv in self.search([('auto_sync', '=', True)]):
            due = not srv.last_sync_time or \
                now - srv.last_sync_time >= timedelta(minutes=srv.sync_interval or 15)
            last_job = Job.search([('server_id', '=', srv.id), ('job_type', '=', 'pull'),
                                   ('state', '=', 'done')], order='date_done desc', limit=1)
            if last_job and last_job.date_done and \
                    now - last_job.date_done < timedelta(minutes=srv.sync_interval or 15):
                due = False
            if due:
                Job.enqueue(srv, 'pull')
            if not srv.last_device_sync or now - srv.last_device_sync >= timedelta(days=1):
                Job.enqueue(srv, 'devices')
