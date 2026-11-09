# -*- coding: utf-8 -*-
"""Employee-facing pages: submit late reasons and overtime claims.

Routes run under the logged-in user; records are created with sudo only after
checking the user is linked to the employee in question."""
import logging
from datetime import timedelta

from odoo import _, fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class BioTimePortal(http.Controller):

    def _my_employee(self):
        emp = request.env.user.employee_id
        return emp if emp else False

    def _prepare_values(self):
        emp = self._my_employee()
        Late = request.env['biotime.late.reason'].sudo()
        Ot = request.env['biotime.overtime.sheet'].sudo()
        Att = request.env['hr.attendance'].sudo()
        values = {'employee': emp}
        if not emp:
            return values
        atts = Att.search([('employee_id', '=', emp.id), ('bt_status', '=', 'late')])
        ot_atts = Att.search([
            ('employee_id', '=', emp.id), ('bt_overtime_hours', '>', 0),
            ('check_in', '>=', fields.Datetime.now() - timedelta(days=30))])
        values.update({
            'pending_lates': atts.filtered(
                lambda a: not Late.search([('attendance_id', '=', a.id)], limit=1)),
            'my_reasons': Late.search([('employee_id', '=', emp.id)], limit=20),
            'my_overtime': Ot.search([('employee_id', '=', emp.id)], limit=20),
            'my_ot_days': [{'date': a.check_in, 'hours': a.bt_overtime_hours}
                           for a in ot_atts],
        })
        return values

    @http.route('/my/biotime', type='http', auth='user', website=True)
    def portal_biotime(self, **post):
        values = self._prepare_values()
        error = False
        emp = values.get('employee')
        if emp and post:
            try:
                if post.get('form') == 'late':
                    att = request.env['hr.attendance'].sudo().browse(
                        int(post.get('attendance_id') or 0)).exists()
                    if not att or att.employee_id != emp or att.bt_status != 'late':
                        error = _('That late arrival is not open for a reason.')
                    else:
                        Late = request.env['biotime.late.reason'].sudo()
                        if Late.search([('attendance_id', '=', att.id)], limit=1):
                            error = _('A reason already exists for this late arrival.')
                        else:
                            Late.create({
                                'attendance_id': att.id,
                                'category': post.get('category') or 'other',
                                'reason': (post.get('reason') or '').strip(),
                            })
                            return request.redirect('/my/biotime?submitted=late')
                elif post.get('form') == 'overtime':
                    hours = float(post.get('hours') or 0)
                    date = post.get('date')
                    if not date or hours <= 0 or not (post.get('reason') or '').strip():
                        error = _('Date, hours and reason are required.')
                    else:
                        Ot = request.env['biotime.overtime.sheet'].sudo()
                        Ot.create({
                            'employee_id': emp.id, 'date': date, 'hours_claimed': hours,
                            'reason': post.get('reason').strip(),
                        })
                        return request.redirect('/my/biotime?submitted=overtime')
            except ValueError:
                error = _('Invalid input.')
        values['error'] = error
        values['submitted'] = post.get('submitted') in ('late', 'overtime')
        values['today'] = fields.Date.context_today(request.env.user)
        values['categories'] = request.env['biotime.late.reason']._fields['category'].selection
        return request.render('biotime_connect_pro.portal_my_biotime', values)
