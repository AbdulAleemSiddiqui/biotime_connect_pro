# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPairing(TransactionCase):
    """Pairing engine behaviors, verified against plain Odoo with no BioTime server."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = cls.env['biotime.server'].create({
            'name': 'Test server', 'host': 'test.local', 'auth_type': 'basic',
            'username': 'u', 'password': 'p', 'timezone': 'UTC',
            'auto_create_employee': False,
        })
        cls.emp = cls.env['hr.employee'].create({'name': 'Test Worker', 'biotime_code': '77'})

    def _tx(self, when, state='0', code='77'):
        return self.env['biotime.transaction'].create({
            'server_id': self.srv.id, 'biotime_id': 'T%s-%s' % (when, state),
            'emp_code': code, 'punch_time': when, 'punch_time_local': when,
            'punch_state': state,
        })

    def _all_tx(self):
        return self.env['biotime.transaction'].search([])

    def _atts(self):
        return self.env['hr.attendance'].search([('employee_id', '=', self.emp.id)])

    def test_explicit_pairing(self):
        t0 = fields.Datetime.now()
        self._tx(t0, '0')
        self._tx(t0 + timedelta(hours=8), '1')
        self._all_tx()._process()
        atts = self._atts()
        self.assertEqual(len(atts), 1)
        self.assertTrue(atts.check_out)

    def test_orphan_checkout_is_skipped(self):
        """A check-out with no open check-in must be skipped, never error/retried forever."""
        tx = self._tx(fields.Datetime.now(), '1')
        tx._process()
        self.assertEqual(tx.state, 'skipped')
        self.assertIn('Orphan', tx.error or '')
        self.assertFalse(self._atts())

    def test_double_punch_skipped(self):
        t0 = fields.Datetime.now()
        self._tx(t0, '0')
        tx = self._tx(t0 + timedelta(seconds=30), '1')
        self._all_tx()._process()
        self.assertEqual(tx.state, 'skipped')
        self.assertIn('Double punch', tx.error or '')
        self.assertFalse(self._atts().check_out)

    def test_alternating_toggle(self):
        """Alternating mode ignores wrong device labels: 2nd punch closes even if state=0."""
        self.srv.pairing_mode = 'alternating'
        t0 = fields.Datetime.now()
        self._tx(t0, '0')                          # opens
        self._tx(t0 + timedelta(hours=9), '0')     # closes, despite state=0
        self._tx(t0 + timedelta(hours=10), '0')    # opens the next shift
        self._all_tx()._process()
        atts = self._atts().sorted('check_in')
        self.assertEqual(len(atts), 2)
        self.assertTrue(atts[0].check_out)
        self.assertFalse(atts[1].check_out)

    def test_employee_timezone_correction(self):
        """Punch re-interpreted in the employee's timezone: 09:00 Riyadh = 06:00 UTC."""
        self.emp.tz = 'Asia/Riyadh'
        t0 = fields.Datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        tx = self._tx(t0, '0')
        tx._process()
        self.assertEqual(tx.punch_time.hour, 6)
        self.assertEqual(self._atts().check_in.hour, 6)

    def test_stale_sweep(self):
        """The daily sweep closes attendances open longer than max_shift_hours."""
        t0 = fields.Datetime.now() - timedelta(days=2)
        self._tx(t0, '0')
        self._all_tx()._process()
        self.assertFalse(self._atts().check_out)
        self.env['hr.attendance']._cron_close_stale()
        att = self._atts()
        self.assertTrue(att.check_out)
        self.assertTrue(att.bt_missing_checkout)

    def test_first_last_pairing(self):
        """First & Last: messy labels and lunch punches still give one 08:00-17:00 record."""
        self.srv.pairing_mode = 'first_last'
        t0 = fields.Datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
        self._tx(t0, '1')                          # first punch, mislabeled OUT
        self._tx(t0 + timedelta(hours=2), '0')     # lunch out
        self._tx(t0 + timedelta(hours=3), '0')     # lunch back
        self._tx(t0 + timedelta(hours=9), '0')     # last punch, mislabeled IN
        self._all_tx()._process()
        atts = self._atts()
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts.check_in.hour, 8)
        self.assertEqual(atts.check_out.hour, 17)
        self.assertEqual(set(self._all_tx().mapped('state')), {'processed'})

    def test_first_last_late_arrival_self_heals(self):
        """A punch that arrives after the day was already paired re-shapes the whole day."""
        self.srv.pairing_mode = 'first_last'
        t0 = fields.Datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
        self._tx(t0 + timedelta(hours=1), '0')     # first batch: 09:00 only
        self._all_tx()._process()
        att = self._atts()
        self.assertEqual(att.check_in.hour, 9)
        # earlier 08:00 punch arrives in a later batch
        self._tx(t0, '0')
        self._all_tx()._process()
        att = self._atts()
        self.assertEqual(len(att), 1)
        self.assertEqual(att.check_in.hour, 8)

    def test_cron_respects_max_retries(self):
        """_cron_process must use the server's max_retries, not a hardcoded value."""
        tx = self._tx(fields.Datetime.now(), '0')
        tx.write({'state': 'error', 'retry_count': 0, 'error': 'simulated'})
        self.srv.max_retries = 0
        self.env['biotime.transaction']._cron_process()
        self.assertEqual(tx.state, 'error')  # not retried at max_retries=0
        self.srv.max_retries = 5
        self.env['biotime.transaction']._cron_process()
        self.assertEqual(tx.state, 'processed')  # retried, paired cleanly
