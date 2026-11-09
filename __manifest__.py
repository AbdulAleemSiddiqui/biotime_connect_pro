# -*- coding: utf-8 -*-
{
    'name': 'BioTime Connect Pro | ZKTeco BioTime Attendance Suite',
    'summary': 'Two-way ZKTeco BioTime (8.x / 9.x / Cloud) to Odoo attendance sync with '
               'smart pairing, late/overtime rules, job queue, dashboard and device monitoring.',
    'description': """
BioTime Connect Pro
===================
All-in-one ZKTeco BioTime integration for Odoo HR Attendance.
See static/description/index.html for the full feature list.
    """,
    'version': '19.0.1.4.3',
    'category': 'Human Resources/Attendances',
    'author': 'Your Company',
    'website': 'https://example.com',
    'license': 'OPL-1',
    'depends': ['hr', 'hr_attendance', 'mail', 'portal'],
    'external_dependencies': {'python': ['requests', 'pytz']},
    'assets': {
        'web.assets_backend': [
            'biotime_connect_pro/static/src/dashboard/*',
        ],
    },
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'wizard/biotime_pull_wizard_views.xml',
        'views/biotime_server_views.xml',
        'views/biotime_transaction_views.xml',
        'views/biotime_misc_views.xml',
        'views/hr_views.xml',
        'views/dashboard_views.xml',
        'views/menus.xml',
        'views/biotime_review_views.xml',
        'views/biotime_mapping_views.xml',
        'views/biotime_portal_templates.xml',
        'data/ir_cron.xml',
    ],
    'application': True,
    'installable': True,
    'price': 199.00,
    'currency': 'USD',
}
